//! Boxes (`run._boxes`, `_box`, `_fill`, TG.RUN.009, .025): a `where` on
//! index tables cut where any table changes, each box run with its tables
//! constant; a box of one value is a mover fill, or the value moved in.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use ktpu_machine::Refusal;
use serde_json::json;
use sha2::{Digest as _, Sha256};

use super::rows::{prune, rebuild};
use super::{item, Bufs, Runner};
use crate::exec::View;
use crate::expr::{self, Expr, E};
use crate::fold::{self, Ty};
use crate::lower::{hex, Access, Plan, Slot, Table};
use crate::program::Call;
use crate::uop::Id;

/// Most boxes a padded or selected loop nest is cut into.
const MAX_BOXES: usize = 64;

/// A table's values as an array over its ranges (`_table_axes`): ranges
/// outermost first, extents, f16 values row-major.
pub(super) struct Axes {
    pub rs: Vec<Id>,
    pub ext: Vec<i64>,
    pub vals: Vec<f64>,
}

impl Axes {
    pub fn strides(&self) -> Vec<i64> {
        let mut out = vec![1; self.ext.len()];
        for k in (0..self.ext.len().saturating_sub(1)).rev() {
            out[k] = out[k + 1] * self.ext[k + 1];
        }
        out
    }

    /// Positions `p` along axis `k` where the table changes from `p` to
    /// `p + 1` anywhere over the other axes.
    fn steps(&self, k: usize) -> Vec<i64> {
        let st = self.strides();
        let mut out = Vec::new();
        for p in 0..self.ext[k] - 1 {
            let changes = (0..self.vals.len() as i64).any(|flat| {
                (flat / st[k]) % self.ext[k] == p
                    && self.vals[(flat + st[k]) as usize] - self.vals[flat as usize] != 0.0
            });
            if changes {
                out.push(p);
            }
        }
        out
    }

    /// The values inside `box` (`(start, end)` an axis), row-major.
    pub fn slice(&self, bounds: &[(i64, i64)]) -> Vec<usize> {
        let st = self.strides();
        let mut idx = vec![0usize];
        for (k, (s, e)) in bounds.iter().enumerate() {
            let step = st[k];
            idx = idx
                .iter()
                .flat_map(|i| (*s..*e).map(move |p| i + (p * step) as usize))
                .collect();
        }
        idx
    }
}

impl Runner<'_> {
    pub(super) fn table_axes(&self, a: &Access) -> Axes {
        let mut coef = a.coef.clone();
        coef.sort_by_key(|(_, c)| -c);
        let rs: Vec<Id> = coef.iter().map(|(r, _)| *r).collect();
        let ext = rs.iter().map(|r| self.extent(*r)).collect();
        let vals = a
            .table
            .as_ref()
            .unwrap()
            .bits
            .iter()
            .map(|b| fold::f16_value(*b))
            .collect();
        Axes { rs, ext, vals }
    }

    /// `_boxes`: false when nothing cuts, a bound moves with a reduced
    /// range, the cuts are more than `MAX_BOXES`, or a box's output is not a
    /// window of whole words.
    pub(super) fn boxes(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        if crate::mutant::on("tg.run/no_boxes") {
            return Ok(false);
        }
        let kept = self.live(&plan.out_ranges);
        let mut cuts: HashMap<Id, BTreeSet<i64>> = HashMap::new();
        let mut must = false;
        let mut sel: Vec<usize> = Vec::new();
        for (i, gate) in self.selectors(plan) {
            let ax = self.table_axes(&plan.accesses[i]);
            let live: Vec<usize> = (0..ax.rs.len()).filter(|k| ax.ext[*k] > 1).collect();
            if live.iter().any(|k| !kept.contains(&ax.rs[*k])) {
                if gate {
                    return Ok(false);
                }
                continue;
            }
            must |= gate;
            sel.push(i);
            for k in live {
                let set = cuts
                    .entry(ax.rs[k])
                    .or_insert_with(|| BTreeSet::from([0, ax.ext[k]]));
                set.extend(ax.steps(k).into_iter().map(|p| p + 1));
            }
        }
        if cuts.is_empty() {
            if !(must && !kept.is_empty()) {
                return Ok(false);
            }
            cuts.insert(kept[0], BTreeSet::from([0, self.extent(kept[0])]));
        }
        let axes: Vec<Id> = kept
            .iter()
            .copied()
            .filter(|r| cuts.contains_key(r))
            .collect();
        let spans: Vec<Vec<(i64, i64)>> = axes
            .iter()
            .map(|r| {
                let s: Vec<i64> = cuts[r].iter().copied().collect();
                s.windows(2).map(|w| (w[0], w[1])).collect()
            })
            .collect();
        let count: usize = spans.iter().map(Vec::len).product();
        if count > MAX_BOXES {
            return Ok(false);
        }
        let it = item(&plan.out_dtype);
        let mut boxes = Vec::new();
        let mut pick = vec![0usize; spans.len()];
        for _ in 0..count {
            let bx: Vec<(Id, (i64, i64))> = axes
                .iter()
                .zip(&pick)
                .enumerate()
                .map(|(k, (r, p))| (*r, spans[k][*p]))
                .collect();
            let Some((sub, sub_bufs)) = self.cut(plan, &bx, bufs, &sel) else {
                return Ok(false);
            };
            let doff = sub_bufs[&sub.out_slot].offset;
            if doff.rem_euclid(self.hw.word) != 0
                || self.walk(&sub, &self.out_coef(&sub), it).is_none()
            {
                return Ok(false);
            }
            boxes.push((sub, sub_bufs));
            for d in (0..pick.len()).rev() {
                pick[d] += 1;
                if pick[d] < spans[d].len() {
                    break;
                }
                pick[d] = 0;
            }
        }
        for (sub, sub_bufs) in boxes {
            match sub.expr.as_deref() {
                Some(Expr::Const(v)) => self.fill(&sub, &sub_bufs, *v)?,
                _ => self.vector(&sub, &sub_bufs)?,
            }
        }
        Ok(true)
    }

    /// `_box`: `plan` over `bx` (`(range, (start, end))`): ranges narrowed,
    /// reads moved, the output a window of its buffer; None when a selector
    /// is not constant over it.
    pub(super) fn cut(
        &mut self,
        plan: &Plan,
        bx: &[(Id, (i64, i64))],
        bufs: &Bufs,
        sel: &[usize],
    ) -> Option<(Plan, Bufs)> {
        let bound = |r: Id| bx.iter().find(|(x, _)| *x == r).map(|(_, b)| *b);
        let mut new: Vec<(Id, Id)> = Vec::new();
        for (r, (s, e)) in bx {
            let n = self.resized(*r, e - s);
            new.push((*r, n));
        }
        let renamed = |r: Id| new.iter().find(|(x, _)| *x == r).map_or(r, |(_, n)| *n);
        let mut consts: HashMap<usize, f64> = HashMap::new();
        let mut accesses = Vec::new();
        for (i, a) in plan.accesses.iter().enumerate() {
            let mut off = a.off
                + a.coef
                    .iter()
                    .filter_map(|(r, c)| bound(*r).map(|(s, _)| c * s))
                    .sum::<i64>();
            let mut coef: Vec<(Id, i64)> = a.coef.iter().map(|(r, c)| (renamed(*r), *c)).collect();
            let mut table = a.table.clone();
            if a.slot == Slot::Table && a.coef.iter().any(|(r, _)| bound(*r).is_some()) {
                let ax = self.table_axes(a);
                let bounds: Vec<(i64, i64)> = ax
                    .rs
                    .iter()
                    .zip(&ax.ext)
                    .map(|(r, e)| bound(*r).unwrap_or((0, *e)))
                    .collect();
                let at = ax.slice(&bounds);
                let bits: Vec<u16> = at
                    .iter()
                    .map(|i| a.table.as_ref().unwrap().bits[*i])
                    .collect();
                let keep: Vec<Id> = ax.rs.iter().map(|r| renamed(*r)).collect();
                coef = keep.iter().copied().zip(self.mult(&keep)).collect();
                off = 0;
                if sel.contains(&i) {
                    let first = at.first().map(|j| ax.vals[*j])?;
                    if !at.iter().all(|j| ax.vals[*j] == first) {
                        return None;
                    }
                    consts.insert(i, first);
                }
                table = Some(Table {
                    shape: vec![bits.len()],
                    bits,
                });
            }
            accesses.push(Access {
                off,
                coef,
                table,
                ..a.clone()
            });
        }
        let mut reds: Vec<(E, Vec<Id>)> = Vec::new();
        let e = rebuild(
            plan.expr.as_ref().unwrap(),
            &mut |x| match x.as_ref() {
                Expr::Load(i) => Some(match consts.get(i) {
                    Some(v) => expr::constant(*v),
                    None => x.clone(),
                }),
                _ => None,
            },
            &mut |x, built| match (x.as_ref(), built.as_ref()) {
                (Expr::Op(n, _), Expr::Op(_, args)) if n == "where" => match args[0].as_ref() {
                    Expr::Const(v) if *v != 0.0 => args[1].clone(),
                    Expr::Const(_) => args[2].clone(),
                    _ => built.clone(),
                },
                (Expr::Reduce(..), _) => {
                    let rs = super::rows::reductions_of(plan, x);
                    reds.push((built.clone(), rs.iter().map(|r| renamed(*r)).collect()));
                    built
                }
                _ => built,
            },
        );
        let (e, accesses, reductions) = if matches!(e.as_ref(), Expr::Const(_)) {
            (e, Vec::new(), reds)
        } else {
            let (e, acc, remap) = prune(&e, &accesses);
            let r = remap
                .iter()
                .map(|(k, v)| {
                    let rs = &reds
                        .iter()
                        .find(|(q, _)| std::rc::Rc::ptr_eq(q, v))
                        .unwrap()
                        .1;
                    (k.clone(), rs.clone())
                })
                .collect();
            (e, acc, r)
        };
        let mult = self.out_coef(plan);
        let m = |r: Id| super::coef_of(&mult, r);
        let out_ranges: Vec<Id> = plan.out_ranges.iter().map(|r| renamed(*r)).collect();
        let first: i64 = bx.iter().map(|(r, (s, _))| m(*r) * s).sum();
        let it = item(&plan.out_dtype);
        let (st, off) = self.base(bufs, &plan.out_slot);
        let span = 1 + plan
            .out_ranges
            .iter()
            .map(|r| (self.extent(renamed(*r)) - 1) * m(*r))
            .sum::<i64>();
        let view = View {
            base: st,
            offset: off + first * it,
            nbytes: span * it,
        };
        let live: Vec<(Id, Id)> = plan
            .out_ranges
            .iter()
            .map(|r| (*r, renamed(*r)))
            .filter(|(_, n)| self.extent(*n) > 1)
            .collect();
        let news: Vec<Id> = live.iter().map(|(_, n)| *n).collect();
        let dense = self.dense(&news);
        let is_dense = live
            .iter()
            .all(|(r, n)| m(*r) == super::coef_of(&dense, *n));
        let sub = Plan {
            out_shape: out_ranges.iter().map(|r| self.extent(*r)).collect(),
            out_ranges,
            expr: Some(e),
            accesses,
            reductions,
            reduce_ranges: plan.reduce_ranges.iter().map(|r| renamed(*r)).collect(),
            out_strides: if is_dense {
                None
            } else {
                Some(plan.out_ranges.iter().map(|r| m(*r)).collect())
            },
            ..plan.clone()
        };
        let mut b2 = bufs.clone();
        b2.insert(plan.out_slot.clone(), view);
        Some((sub, b2))
    }

    /// `_fill`: the mover's fill over the output window where it is whole
    /// words, else the value moved in from a temporary.
    fn fill(&mut self, plan: &Plan, bufs: &Bufs, value: f64) -> Result<(), Refusal> {
        let it = item(&plan.out_dtype);
        let (dst, doff) = self.base(bufs, &plan.out_slot);
        let dlevels = self.walk(plan, &self.out_coef(plan), it);
        let word = self.hw.word;
        let Some(dlevels) = dlevels.filter(|_| doff.rem_euclid(word) == 0 && it != 8) else {
            let live = self.live(&plan.out_ranges);
            let n = self.size(&live);
            let t = self.x.storage(self.hw.f16_bytes(n));
            self.x.store_mut(&t).dtype = Some("float16".into());
            let mut h = Sha256::new();
            let bits = fold::f16_bits(value);
            for _ in 0..n {
                h.update(bits.to_le_bytes());
            }
            let at = self.x.addr(&t, 0);
            self.x.write(at, 2 * n, hex(&h.finalize()));
            let got = self
                .half_target(plan, bufs)
                .and_then(|(tplan, tbufs, fin)| {
                    self.moved(&tplan, &tbufs, &t, &self.dense(&live))?;
                    self.finish_half(fin);
                    Ok(())
                });
            self.x.free(&t);
            return got;
        };
        let bits: i64 = match Ty::of(&plan.out_dtype) {
            Some(Ty::F16) => fold::f16_bits(value) as i64,
            Some(Ty::F32) => (value as f32).to_bits() as i64,
            _ => (value as i64) & ((1i64 << (8 * it)) - 1),
        };
        self.window_ok(&dst, plan)?;
        let call = Call {
            gen: "convert.fill_kernel".into(),
            args: json!({
                "dst_bytes": self.x.store(&dst).nbytes,
                "dlevels": dlevels,
                "dat": doff,
                "value": bits,
                "ew": 8 * it,
            }),
        };
        let binds = BTreeMap::from([("dst".to_string(), self.x.addr(&dst, 0))]);
        self.x
            .launch(call, "move", json!({"plan": plan.name}), binds);
        self.wrote_flat(&dst, plan);
        Ok(())
    }
}
