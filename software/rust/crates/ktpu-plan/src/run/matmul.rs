//! Contractions on the clusters (`run._matmul`, `_product`, `_product_run`,
//! `forms.mx7`, `kernels.matmul.plan`, TG.RUN.019-.020, .028-.029): each
//! side laid out as [batch.., rows, K] where it is not, quantised once into
//! its MXFP7 packing, the product and its epilogue one kernel; a padded
//! product moved out of a temporary or kept in its padded-rows form.

use std::collections::BTreeMap;

use ktpu_kernels::gen::matmul::{self, Group, Knobs};
use ktpu_machine::{Machine, Refusal, PAIRING};
use serde_json::{json, Value};

use super::pending::form_error;
use super::{bound, costs, expr_arg, is_float, item, np_name, Bound, Bufs, Operand, Runner, Step};
use crate::exec::{rows_key, View};
use crate::hw::Hw;
use crate::lower::{Access, Plan, Slot};
use crate::program::{Addr, Call};
use crate::uop::{Arg, Id};

/// One contraction's tiling (`ktpu_kernels::gen::matmul::plan`) and the
/// knobs it was planned under.
#[derive(Clone, Debug, PartialEq)]
pub struct Mm {
    pub bt: i64,
    pub m: i64,
    pub n: i64,
    pub k: i64,
    pub mp: i64,
    pub kp: i64,
    pub tm: i64,
    pub tn: i64,
    pub nk: i64,
    pub shared: bool,
    pub quantum: i64,
    /// whether the call named the quantum (one band otherwise)
    pub named: bool,
    pub knobs: Knobs,
}

impl Mm {
    /// The generators' plan on `m` (its default pairing) under `knobs`.
    #[allow(clippy::too_many_arguments)]
    pub fn plan(
        m: &Machine,
        knobs: &Knobs,
        bt: i64,
        rows: i64,
        n: i64,
        k: i64,
        shared: bool,
        quantum: Option<i64>,
    ) -> Result<Mm, Refusal> {
        let hw = Hw::of(m);
        let (named, quantum) = (quantum.is_some(), quantum.unwrap_or(hw.band));
        if n % hw.kblock != 0 {
            return Err(Refusal::new(
                "TG.RUN.019",
                format!(
                    "N = {n}: the product's columns are whole blocks of {}",
                    hw.kblock
                ),
            ));
        }
        let p = matmul::plan(bt, rows, n, k, shared, quantum, PAIRING, m, knobs)?;
        Ok(Mm {
            bt,
            m: rows,
            n,
            k,
            mp: p.mp,
            kp: p.kp,
            tm: p.tm,
            tn: p.tn,
            nk: p.nk,
            shared,
            quantum,
            named,
            knobs: *knobs,
        })
    }

    /// Whether its kernel takes A as f16 on `m` (`matmul::takes_f16`).
    pub fn takes_f16(&self, m: &Machine) -> bool {
        matmul::takes_f16(self.k, self.nk, &self.knobs, m)
    }

    /// The generator call that made it, its plan knobs recorded.
    pub fn made_by(&self) -> Value {
        let mut args = json!({
            "bt": self.bt, "m": self.m, "n": self.n, "k": self.k, "shared": self.shared,
        });
        if self.named {
            args["quantum"] = json!(self.quantum);
        }
        for (k, v) in self.knobs.args(Group::Plan) {
            args[k] = v;
        }
        json!({"made_by": {"gen": "matmul.plan", "args": args}})
    }

    /// The `matmul.mm_kernel` call of epilogue `e` over `extras` into a
    /// full [bt * mp, `pitch`] output, its kernel knobs recorded.
    pub fn kernel_call(
        &self,
        e: Value,
        extras: &[Operand],
        pitch: i64,
        (clusters, cores): (i64, i64),
    ) -> Call {
        let mut args = json!({
            "mm": self.made_by(),
            "e": e,
            "extras": extras.iter().map(Operand::json).collect::<Vec<_>>(),
            "out": Operand { pitch, rows: self.bt * self.mp, ..Operand::new("full") }.json(),
            "clusters": clusters,
            "cores": cores,
        });
        for (k, v) in self.knobs.args(Group::Kernel) {
            args[k] = v;
        }
        Call {
            gen: "matmul.mm_kernel".into(),
            args,
        }
    }

    /// Python's `vars(mm)`.
    pub fn vars(&self) -> Value {
        json!({
            "bt": self.bt, "m": self.m, "n": self.n, "k": self.k, "mp": self.mp,
            "kp": self.kp, "tm": self.tm, "tn": self.tn, "nk": self.nk,
            "shared": self.shared, "quantum": self.quantum,
        })
    }
}

/// A kernel call's `made` argument and its binds.
type MadeBinds = (Value, BTreeMap<String, Addr>);

/// `coef` without the ranges of extent one.
fn live_coef(r: &Runner, coef: &[(Id, i64)]) -> Vec<(Id, i64)> {
    coef.iter()
        .copied()
        .filter(|(x, _)| r.extent(*x) > 1)
        .collect()
}

fn same_map(a: &[(Id, i64)], b: &[(Id, i64)]) -> bool {
    a.len() == b.len() && a.iter().all(|x| b.contains(x))
}

impl Runner<'_> {
    /// `_batched`: refuse a batched operand whose batches are not one
    /// row-major stack.
    fn batched(&self, acc: &Access, batch: &[Id], step: i64, what: &str) -> Result<(), Refusal> {
        let mut want = step;
        for r in batch.iter().rev() {
            if self.extent(*r) > 1 && acc.c(*r) != want {
                return Err(Refusal::new(
                    "TG.RUN.028",
                    format!("{what}'s batches are not one row-major stack of [rows, K] blocks"),
                ));
            }
            want *= self.extent(*r);
        }
        Ok(())
    }

    /// `window.ranges`: new ranges of `extents`, ids no range of `plan` has,
    /// of the kind of its output's.
    pub(super) fn fresh_ranges(&mut self, plan: &Plan, extents: &[i64]) -> Vec<Id> {
        let id = |me: &Self, r: Id| match me.g.arg(r) {
            Arg::Range { id, .. } => *id,
            _ => 0,
        };
        let mut seen: Vec<i64> = plan
            .accesses
            .iter()
            .flat_map(|a| a.coef.iter().map(|(r, _)| id(self, *r)))
            .collect();
        seen.extend(
            plan.out_ranges
                .iter()
                .chain(&plan.reduce_ranges)
                .map(|r| id(self, *r)),
        );
        let top = 1 + seen.into_iter().max().unwrap_or(0);
        let kind = match self.g.arg(plan.out_ranges[0]) {
            Arg::Range { axis, .. } => axis.clone(),
            _ => unreachable!("an output range"),
        };
        extents
            .iter()
            .enumerate()
            .map(|(i, n)| self.g.range(*n, top + i as i64, &kind))
            .collect()
    }

    /// [`packed`](Self::packed) of `acc`'s buffer, or of the pending buffer
    /// at its slot: packed by its measured recipe inside the quant kernel,
    /// else made by its own relayouts first.
    #[allow(clippy::too_many_arguments)]
    fn packed_read(
        &mut self,
        bufs: &Bufs,
        acc: &Access,
        src: &Operand,
        dims: (i64, i64, i64, i64),
        side: &str,
        groups: (i64, i64, i64),
    ) -> Result<Addr, Refusal> {
        let Some(&id) = self.pending_slots.get(&acc.slot) else {
            return self.packed(&bufs[&acc.slot], &acc.dtype, src, dims, side, groups);
        };
        if let Some(at) = self.prepared_packing(id, src, dims, side, groups)? {
            return Ok(at);
        }
        let at = self.made(id)?;
        let view = View {
            nbytes: self.x.store(&at.buf).nbytes,
            base: at.buf,
            offset: 0,
        };
        self.packed(&view, "half", src, dims, side, groups)
    }

    /// `forms._prepared_quant` kept on the pending buffer's holder: its
    /// packing by the measured recipe's steps inside the quant kernel; None
    /// when no recipe is admitted or its steps do not plan straight into
    /// the destination.
    fn prepared_packing(
        &mut self,
        id: usize,
        src: &Operand,
        (rows, cols, rp, cp): (i64, i64, i64, i64),
        side: &str,
        (g, nk, batches): (i64, i64, i64),
    ) -> Result<Option<Addr>, Refusal> {
        if !self.knobs.forms.prepared_quant {
            return Ok(None);
        }
        let steps = self.pending[id].steps.clone();
        let Some(recipe) = costs::prepared_quant(
            &steps,
            (src.pitch, src.offset),
            (rows, cols, rp, cp),
            side,
            (g, nk, batches),
            &self.m.name,
        ) else {
            return Ok(None);
        };
        let level = |v: &Value| -> Vec<(i64, i64)> {
            v.as_array()
                .unwrap()
                .iter()
                .map(|l| (l[0].as_i64().unwrap(), l[1].as_i64().unwrap()))
                .collect()
        };
        let measured: Vec<Step> = recipe["steps"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
            .map(|(i, v)| Step {
                src: if i == 0 { steps[0].src.clone() } else { None },
                n: v["n"].as_i64().unwrap(),
                walk: level(&v["walk"]),
                at: v["at"].as_i64().unwrap(),
                dwalk: level(&v["dwalk"]),
                dat: v["dat"].as_i64().unwrap(),
                m: v["m"].as_i64().unwrap(),
            })
            .collect();
        let Some(plans) = self.plans_of(&measured)? else {
            return Ok(None);
        };
        if !ktpu_kernels::gen::prepared::direct_destination(&plans) {
            return Ok(None);
        }
        // The tables go up with the kernel's making, whether or not the
        // holder keeps this packing already.
        let mut binds = BTreeMap::from([(
            "p_x".to_string(),
            steps[0].src.clone().expect("a first step reads a source"),
        )]);
        self.step_binds(&plans, "p_", &mut binds);
        let key = json!([
            "prepared_mx7",
            side,
            src.json(),
            rows,
            cols,
            rp,
            cp,
            g,
            nk,
            batches
        ]);
        let holder = self.holder(id);
        if !self.x.store(&holder).has(&key) {
            let q = &recipe["quant"];
            let folded = json!({
                "kind": "full",
                "dtype": q["src"].get("dtype").cloned().unwrap_or(json!("f16")),
                "offset": q["src"]["offset"],
                "pitch": q["src"]["pitch"],
                "rows": 0,
            });
            let made: Vec<Value> = measured.iter().map(|s| s.made_by()).collect();
            let call = Call {
                gen: "matmul.quant_kernel".into(),
                args: json!({
                    "src": folded, "rows": rows, "cols": q["cols"], "rp": rp, "cp": cp,
                    "side": side, "g": g, "nk": nk, "batches": batches,
                    "prepared": made,
                    "pad_strategy": recipe.get("pad_strategy").cloned().unwrap_or(json!("native")),
                }),
            };
            let f = self.x.form(&holder, &key, batches * rp * cp);
            binds.insert("q".into(), f);
            self.x.launch(call, "quant", json!({}), binds);
            self.x.made(&holder, &key);
        }
        Ok(Some(Addr {
            buf: holder,
            form: key,
            offset: 0,
        }))
    }

    /// `forms.packed` (`forms.mx7`): `batches` [rows, cols] blocks of the
    /// buffer quantised into [batches * rp, cp] MXFP7, kept while unwritten.
    #[allow(clippy::too_many_arguments)]
    pub fn packed(
        &mut self,
        view: &View,
        dtype: &str,
        src: &Operand,
        (rows, cols, rp, cp): (i64, i64, i64, i64),
        side: &str,
        (g, nk, batches): (i64, i64, i64),
    ) -> Result<Addr, Refusal> {
        let (st, off) = (view.base.clone(), view.offset);
        let it = item(dtype);
        let src = Operand {
            offset: src.offset + off.div_euclid(it),
            ..src.clone()
        };
        let key = json!(["mx7", side, src.json(), rows, cols, rp, cp, g, nk, batches]);
        let at = Addr {
            buf: st.clone(),
            form: key.clone(),
            offset: 0,
        };
        if self.x.store(&st).has(&key) {
            return Ok(at);
        }
        let nbytes = self.x.store(&st).nbytes;
        let n = nbytes / it;
        let whole = View {
            base: st.clone(),
            offset: 0,
            nbytes,
        };
        let mut base = self.f16_of(&whole, dtype, n)?;
        let f = self.x.form(&st, &key, batches * rp * cp);
        let (mut qsrc, mut qcols) = (src.clone(), cols);
        let mut tmp = None;
        let h = self.hw.halves;
        if cols % h != 0 || src.pitch % h != 0 || src.offset % h != 0 {
            let (r, dp) = (batches * rows, self.hw.whole_halves(cols));
            let t = self.x.storage(2 * r * dp);
            let tat = self.x.addr(&t, 0);
            tmp = Some(t);
            let walk = [(r, src.pitch), (cols, 1)];
            let got = self.relayout(
                base,
                n,
                &walk,
                src.offset,
                &[(r, dp), (cols, 1)],
                r * dp,
                tat.clone(),
                0,
                false,
            );
            if let Err(e) = got {
                self.x.free(tmp.as_ref().unwrap());
                return Err(e);
            }
            base = tat;
            qsrc = Operand {
                pitch: dp,
                rows: r,
                ..Operand::new("full")
            };
            qcols = dp;
        }
        let call = Call {
            gen: "matmul.quant_kernel".into(),
            args: json!({
                "src": qsrc.json(), "rows": rows, "cols": qcols, "rp": rp, "cp": cp,
                "side": side, "g": g, "nk": nk, "batches": batches,
            }),
        };
        let binds = BTreeMap::from([("x".to_string(), base), ("q".to_string(), f)]);
        self.x.launch(call, "quant", json!({}), binds);
        if let Some(t) = tmp {
            self.x.free(&t);
        }
        self.x.made(&st, &key);
        Ok(at)
    }

    /// `_matmul`: each side not [.., rows, K] row-major laid out first.
    pub(super) fn matmul(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let (m, n, ks) = plan.mnk.clone().expect("a matmul plan's M, N, K");
        let (Some(a), Some(b)) = (plan.a.clone(), plan.b.clone()) else {
            return Err(Refusal::new(
                "TG.RUN.019",
                "a matmul plan without its A and B reads",
            ));
        };
        let batch: Vec<Id> = plan
            .out_ranges
            .iter()
            .copied()
            .filter(|r| plan.batch.contains(r))
            .collect();
        let order: Vec<Id> = batch.iter().copied().chain([m, n]).collect();
        let oc = live_coef(self, &self.out_coef(plan));
        if !same_map(&oc, &live_coef(self, &self.dense(&order))) {
            return self.reordered(plan, bufs, &order);
        }
        let mut bufs = bufs.clone();
        let mut tmps = Vec::new();
        let (mut a, mut b) = (a, b);
        let got = (|| -> Result<(), Refusal> {
            for side in ["a", "b"] {
                let (acc, row) = if side == "a" { (&a, m) } else { (&b, n) };
                let whole = side == "a" || batch.iter().any(|r| acc.c(*r) != 0);
                let mut order: Vec<Id> = if whole { batch.clone() } else { vec![] };
                order.push(row);
                order.extend(&ks);
                let dense = self.dense(&order);
                if same_map(&live_coef(self, &acc.coef), &live_coef(self, &dense)) {
                    continue;
                }
                let key = self.key(&format!("mm{side}"));
                let acc = acc.clone();
                if let Some(id) = self.quant_pending(plan, &acc, &order, &bufs, side)? {
                    // Made inside the packing's kernel.
                    tmps.push(self.holder(id));
                    self.pending_slots.insert(key.clone(), id);
                } else {
                    self.laid_side(plan, &acc, &order, &mut bufs, &mut tmps, &key, side)?;
                }
                let new = Access {
                    slot: key,
                    dtype: "half".into(),
                    coef: dense,
                    off: 0,
                    table: None,
                    gate: false,
                };
                if side == "a" {
                    a = new;
                } else {
                    b = new;
                }
            }
            let p = Plan {
                a: Some(a.clone()),
                b: Some(b.clone()),
                ..plan.clone()
            };
            self.product(&p, &bufs)
        })();
        for t in tmps {
            self.free_tmp(&t);
        }
        got
    }

    /// `_matmul`'s side laid out: `acc` as f16 dense over `order` into a
    /// temporary at `key`.
    #[allow(clippy::too_many_arguments)]
    fn laid_side(
        &mut self,
        plan: &Plan,
        acc: &Access,
        order: &[Id],
        bufs: &mut Bufs,
        tmps: &mut Vec<String>,
        key: &Slot,
        side: &str,
    ) -> Result<(), Refusal> {
        let t = self.x.storage(self.hw.f16_bytes(self.size(order)));
        tmps.push(t.clone());
        let nbytes = self.x.store(&t).nbytes;
        bufs.insert(
            key.clone(),
            View {
                base: t,
                offset: 0,
                nbytes,
            },
        );
        let sub = Plan {
            name: format!("{}_{side}", plan.name),
            ..self.laid_plan(plan, key.clone(), acc, order, None)
        };
        self.vector(&sub, bufs)
    }

    /// `_reordered`: a product whose output is not dense [batch.., m, n]
    /// into an f16 temporary so, then moved into the output.
    fn reordered(&mut self, plan: &Plan, bufs: &Bufs, order: &[Id]) -> Result<(), Refusal> {
        let key = self.key("mmout");
        let nbytes = self.hw.f16_bytes(self.size(order));
        let t = self.x.storage(nbytes);
        self.x.store_mut(&t).dtype = Some("float16".into());
        let sub = Plan {
            out_slot: key.clone(),
            out_dtype: "half".into(),
            out_ranges: order.to_vec(),
            out_shape: order.iter().map(|r| self.extent(*r)).collect(),
            out_strides: None,
            ..plan.clone()
        };
        let mut b2 = bufs.clone();
        b2.insert(
            key,
            View {
                base: t.clone(),
                offset: 0,
                nbytes,
            },
        );
        let got = self.matmul(&sub, &b2).and_then(|_| {
            let (tplan, tbufs, fin) = self.half_target(plan, bufs)?;
            self.moved(&tplan, &tbufs, &t, &self.dense(order))?;
            self.finish_half(fin);
            Ok(())
        });
        self.x.free(&t);
        got
    }

    /// `_packing_plan`: the product's tiling, one decision shared by a
    /// deferred read's admission and the product.
    fn packing_plan(&self, plan: &Plan) -> Result<Mm, Refusal> {
        let (m, n, ks) = plan.mnk.clone().unwrap();
        let b = plan.b.as_ref().unwrap();
        let batch: Vec<Id> = plan
            .out_ranges
            .iter()
            .copied()
            .filter(|r| plan.batch.contains(r))
            .collect();
        let shared = !batch.iter().any(|r| b.c(*r) != 0);
        let rows = plan.out_ranges[..plan.out_ranges.len() - 1].to_vec();
        let stat = plan.accesses.iter().any(|acc| {
            self.classify(acc, &rows, &[n])
                .is_some_and(|o| o.kind == "row")
        });
        let q = if stat { self.stat_rows() } else { self.hw.band };
        let np = self.hw.whole_blocks(self.extent(n));
        let (bt, em, k) = (self.size(&batch), self.extent(m), self.size(&ks));
        Mm::plan(self.m, &self.knobs.matmul, bt, em, np, k, shared, Some(q))
    }

    /// `_pending`: `acc` as f16 dense over `order` for a packing alone to
    /// read, a pending buffer of one relayout step from the buffer's f16
    /// values, where a measured recipe (`costs.prepared_quant`) takes it;
    /// None for a table, a gated read, or no recipe.
    fn quant_pending(
        &mut self,
        plan: &Plan,
        acc: &Access,
        order: &[Id],
        bufs: &Bufs,
        side: &str,
    ) -> Result<Option<usize>, Refusal> {
        if !(self.knobs.forms.prepared && self.knobs.forms.prepared_quant)
            || acc.slot == Slot::Table
            || acc.gate
            || !is_float(&acc.dtype)
        {
            return Ok(None);
        }
        let live = self.live(order);
        let (st, off) = self.base(bufs, &acc.slot);
        let first = acc.off + off.div_euclid(item(&acc.dtype));
        if first < 0 {
            return Ok(None);
        }
        let dense = self.dense(&live);
        let walk: Vec<(i64, i64)> = live.iter().map(|r| (self.extent(*r), acc.c(*r))).collect();
        let dwalk: Vec<(i64, i64)> = live
            .iter()
            .map(|r| (self.extent(*r), super::coef_of(&dense, *r)))
            .collect();
        let (src, total, walk, first, dwalk) =
            match self.f16_walk(&st, &acc.dtype, &walk, first, &dwalk) {
                Ok(got) => got,
                Err(e) if form_error(&e) => return Ok(None),
                Err(e) => return Err(e),
            };
        let n = self.size(&live);
        let steps = vec![Step {
            src: Some(src),
            n: total,
            walk,
            at: first,
            dwalk,
            dat: 0,
            m: n,
        }];
        let mm = self.packing_plan(plan)?;
        let (rows, rp, group, batches) = if side == "a" {
            (mm.m, mm.mp, mm.tm / self.hw.band, mm.bt)
        } else {
            let en = self.extent(plan.mnk.as_ref().unwrap().1);
            let batches = if mm.shared { 1 } else { mm.bt };
            (en, mm.n, mm.tn / self.hw.band, batches)
        };
        let recipe = costs::prepared_quant(
            &steps,
            (mm.k, 0),
            (rows, mm.k, rp, mm.kp),
            side,
            (group, mm.nk, batches),
            &self.m.name,
        );
        if recipe.is_none() {
            return Ok(None);
        }
        Ok(Some(self.pend(2 * n, steps)))
    }

    /// `_product`: both sides packed, the epilogue's operands bound.
    pub(super) fn product(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let (m, n, _) = plan.mnk.clone().unwrap();
        let (a, b) = (plan.a.clone().unwrap(), plan.b.clone().unwrap());
        let (em, en) = (self.extent(m), self.extent(n));
        let batch: Vec<Id> = plan
            .out_ranges
            .iter()
            .copied()
            .filter(|r| plan.batch.contains(r))
            .collect();
        let bt = self.size(&batch);
        let (pa, pb) = (a.c(m), b.c(n));
        self.batched(&a, &batch, em * pa, "A")?;
        let shared = !batch.iter().any(|r| b.c(*r) != 0);
        if !shared {
            self.batched(&b, &batch, en * pb, "B")?;
        }
        let np = self.hw.whole_blocks(en);
        let rows = plan.out_ranges[..plan.out_ranges.len() - 1].to_vec();
        let hw = self.hw;
        let mut mm = self.packing_plan(plan)?;
        let k = mm.k;
        let src_a = Operand {
            pitch: pa,
            offset: a.off,
            ..Operand::new("full")
        };
        // `forms.f16_rows`: a pending A is made first.
        let f16 = if mm.takes_f16(self.m) {
            let (view, dtype) = match self.pending_slots.get(&a.slot) {
                Some(&id) => {
                    let at = self.made(id)?;
                    let view = View {
                        nbytes: self.x.store(&at.buf).nbytes,
                        base: at.buf,
                        offset: 0,
                    };
                    (view, "half".to_string())
                }
                None => (bufs[&a.slot].clone(), a.dtype.clone()),
            };
            self.f16_rows(&view, &dtype, &src_a, k)?
        } else {
            None
        };
        mm.knobs.activation_fp16 = f16.is_some();
        let qa = match f16 {
            Some(at) => at,
            None => self.packed_read(
                bufs,
                &a,
                &src_a,
                (em, k, mm.mp, mm.kp),
                "a",
                (mm.tm / hw.band, mm.nk, bt),
            )?,
        };
        let qb = self.packed_read(
            bufs,
            &b,
            &Operand {
                pitch: pb,
                offset: b.off,
                ..Operand::new("full")
            },
            (en, k, en.max(mm.n), mm.kp),
            "b",
            (mm.tn / hw.band, mm.nk, if shared { 1 } else { bt }),
        )?;
        let mut binds = bound(BTreeMap::from([
            ("a".to_string(), qa),
            ("b".to_string(), qb),
        ]));
        let mut extras = Vec::new();
        let mut lays = Vec::new();
        let got = self
            .epilogue(
                plan,
                bufs,
                &mm,
                (&batch, m, n, np, &rows),
                &mut binds,
                &mut extras,
                &mut lays,
            )
            .and_then(|bufs| {
                self.product_run(plan, &bufs, &mm, binds, &extras, (&batch, m, n, np))
            });
        for t in lays {
            self.free_tmp(&t);
        }
        got
    }

    /// `_product`'s epilogue operands: each read as a row form the kernel
    /// takes, laid out first where it is not one.
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    fn epilogue(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        mm: &Mm,
        (batch, m, n, np, rows): (&[Id], Id, Id, i64, &[Id]),
        binds: &mut BTreeMap<String, Bound>,
        extras: &mut Vec<Operand>,
        lays: &mut Vec<String>,
    ) -> Result<Bufs, Refusal> {
        let mut bufs = bufs.clone();
        let (em, en) = (self.extent(m), self.extent(n));
        let bt = self.size(batch);
        let rpad = mm.mp != em;
        let out_n: i64 = plan.out_shape.iter().product();
        for (i, acc0) in plan.accesses.iter().enumerate() {
            let i = i + 1;
            let mut acc = acc0.clone();
            let mut o = self.classify(&acc, rows, &[n]);
            if o.is_none() {
                let t = self.x.storage(self.hw.f16_bytes(out_n));
                lays.push(t.clone());
                let key = self.key("epi");
                let nbytes = self.x.store(&t).nbytes;
                if acc.slot == Slot::Table {
                    let vals = self.table_over(&acc, &plan.out_ranges);
                    self.write_u16(&t, &vals);
                    self.x.wrote(&t, &crate::exec::flat(), false);
                    bufs.insert(
                        key.clone(),
                        View {
                            base: t,
                            offset: 0,
                            nbytes,
                        },
                    );
                } else {
                    bufs.insert(
                        key.clone(),
                        View {
                            base: t,
                            offset: 0,
                            nbytes,
                        },
                    );
                    let lp = self.laid_plan(plan, key.clone(), &acc, &plan.out_ranges, None);
                    self.vector(&lp, &bufs)?;
                }
                acc = Access {
                    slot: key,
                    dtype: "half".into(),
                    coef: self.dense(&plan.out_ranges),
                    off: 0,
                    table: None,
                    gate: false,
                };
                o = self.classify(&acc, rows, &[n]);
            }
            let Some(mut o) = o else {
                return Err(Refusal::new(
                    "TG.RUN.019",
                    format!(
                        "an epilogue operand read as no row form: strides {:?}",
                        acc.coef
                    ),
                ));
            };
            let start = match bufs.get(&acc.slot) {
                Some(v) => v.offset.div_euclid(item(&acc.dtype)),
                None => 0,
            };
            let off_word = o.kind == "row" && !self.takes(Some(&o), np, start);
            if (rpad || off_word) && (o.kind == "full" || o.kind == "row") {
                let cols: Vec<Id> = if o.kind == "full" { vec![n] } else { vec![] };
                let width = if cols.is_empty() { 1 } else { np };
                let t = self.pad_rows(plan, &bufs, &acc, (batch, m, &cols), mm.mp, width)?;
                lays.push(t.clone());
                let nrows = bt * mm.mp;
                let o2 = if cols.is_empty() {
                    Operand {
                        rows: nrows,
                        ..Operand::new("row")
                    }
                } else {
                    Operand {
                        pitch: np,
                        rows: nrows,
                        ..Operand::new("full")
                    }
                };
                binds.insert(format!("in{i}"), Bound::At(self.x.addr(&t, 0)));
                extras.push(o2);
                continue;
            }
            let h = self.hw.halves;
            if o.kind == "col"
                && (o.offset % h != 0 || (np != en && !crate::mutant::on("memory/unsafe-bounds")))
            {
                if self.knobs.forms.prepared && acc.slot != Slot::Table {
                    // Made inside the product's kernel, or at its launch.
                    let (st, off) = self.base(&bufs, &acc.slot);
                    let it = item(&acc.dtype);
                    let nbytes = self.x.store(&st).nbytes;
                    let whole = View {
                        base: st,
                        offset: 0,
                        nbytes,
                    };
                    let at = self.f16_of(&whole, &acc.dtype, nbytes / it)?;
                    let step = Step {
                        src: Some(at),
                        n: nbytes / it,
                        walk: vec![(en, 1)],
                        at: o.offset + off.div_euclid(it),
                        dwalk: vec![(en, 1)],
                        dat: 0,
                        m: np,
                    };
                    let id = self.pend(2 * np, vec![step]);
                    lays.push(self.holder(id));
                    binds.insert(format!("in{i}"), Bound::Pend(id));
                    extras.push(Operand::new("col"));
                    continue;
                }
                let t = self.x.storage(2 * np);
                lays.push(t.clone());
                let at = self.x.addr(&t, 0);
                if acc.slot == Slot::Table {
                    let bits = &acc.table.as_ref().unwrap().bits;
                    let mut laid = vec![0u16; np as usize];
                    laid[..en as usize]
                        .copy_from_slice(&bits[o.offset as usize..(o.offset + en) as usize]);
                    self.write_u16(&t, &laid);
                } else if !self.gather_to(
                    &acc,
                    &bufs,
                    &[(en, 1)],
                    o.offset,
                    &[(en, 1)],
                    0,
                    np,
                    at.clone(),
                    false,
                )? {
                    return Err(Refusal::new(
                        "TG.RUN.019",
                        format!("an epilogue row of {en} with no f16 form"),
                    ));
                }
                binds.insert(format!("in{i}"), Bound::At(at));
                extras.push(Operand::new("col"));
                continue;
            }
            let (mut at, o2) = self.addr_of(&acc, &o, &bufs)?;
            o = o2;
            if o.kind == "full" && (o.pitch < np || o.offset % h != 0 || o.pitch % h != 0) {
                let nrows = bt * em;
                let t = self.x.storage(2 * nrows * np);
                lays.push(t.clone());
                let tat = self.x.addr(&t, 0);
                if (2 * en) % self.hw.word == 0 && o.offset % h == 0 && o.pitch % h == 0 {
                    let src = Addr {
                        offset: at.offset + 2 * o.offset,
                        ..at.clone()
                    };
                    self.repitch(src, o.pitch, tat.clone(), np, nrows, en)?;
                } else if acc.slot == Slot::Table {
                    let bits = &acc.table.as_ref().unwrap().bits;
                    let mut laid = vec![0u16; (nrows * np) as usize];
                    for r in 0..nrows {
                        let from = (o.offset + r * o.pitch) as usize;
                        let to = (r * np) as usize;
                        laid[to..to + en as usize].copy_from_slice(&bits[from..from + en as usize]);
                    }
                    self.write_u16(&t, &laid);
                } else {
                    let walk = [(nrows, o.pitch), (en, 1)];
                    let dwalk = [(nrows, np), (en, 1)];
                    if !self.gather_to(
                        &acc,
                        &bufs,
                        &walk,
                        o.offset,
                        &dwalk,
                        0,
                        nrows * np,
                        tat.clone(),
                        false,
                    )? {
                        return Err(Refusal::new(
                            "TG.RUN.019",
                            format!("an epilogue operand of rows of {en} with no f16 form"),
                        ));
                    }
                }
                at = tat;
                o = Operand {
                    pitch: np,
                    rows: nrows,
                    ..Operand::new("full")
                };
            }
            binds.insert(format!("in{i}"), Bound::At(at));
            extras.push(o);
        }
        Ok(bufs)
    }

    /// `_pad_rows`: `acc` over [batch.., m, cols..] as f16, each batch's
    /// rows padded to `mp`, rows `width` apart.
    fn pad_rows(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        acc: &Access,
        (batch, m, cols): (&[Id], Id, &[Id]),
        mp: i64,
        width: i64,
    ) -> Result<String, Refusal> {
        let order: Vec<Id> = batch
            .iter()
            .copied()
            .chain([m])
            .chain(cols.iter().copied())
            .collect();
        let mut strides = vec![width];
        if !cols.is_empty() {
            strides.push(1);
        }
        let mut step = mp * width;
        for r in batch.iter().rev() {
            strides.insert(0, step);
            step *= self.extent(*r);
        }
        let t = self.x.storage(self.hw.f16_bytes(step));
        self.x.wrote(&t, &crate::exec::flat(), false);
        self.x.store_mut(&t).dtype = Some("float16".into());
        if acc.slot == Slot::Table {
            let vals = self.table_over(acc, &order);
            let ext: Vec<i64> = order.iter().map(|r| self.extent(*r)).collect();
            let mut laid = vec![0u16; step as usize];
            let mut idx = vec![0i64; order.len()];
            for v in vals {
                let at: i64 = idx.iter().zip(&strides).map(|(i, s)| i * s).sum();
                laid[at as usize] = v;
                for d in (0..order.len()).rev() {
                    idx[d] += 1;
                    if idx[d] < ext[d] {
                        break;
                    }
                    idx[d] = 0;
                }
            }
            self.write_u16(&t, &laid);
            return Ok(t);
        }
        let key = self.key("pad_rows");
        let mut b2 = bufs.clone();
        let nbytes = self.x.store(&t).nbytes;
        b2.insert(
            key.clone(),
            View {
                base: t.clone(),
                offset: 0,
                nbytes,
            },
        );
        let lp = self.laid_plan(plan, key, acc, &order, Some(strides));
        match self.vector(&lp, &b2) {
            Ok(()) => Ok(t),
            Err(e) => {
                self.x.free(&t);
                Err(e)
            }
        }
    }

    /// `_product_run`: the kernel, into the output, its padded-rows form,
    /// or an [bt, mp, Np] temporary whose window is moved out.
    #[allow(clippy::type_complexity)]
    fn product_run(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        mm: &Mm,
        binds: BTreeMap<String, Bound>,
        extras: &[Operand],
        (batch, m, n, np): (&[Id], Id, Id, i64),
    ) -> Result<(), Refusal> {
        let (em, en) = (self.extent(m), self.extent(n));
        let bt = self.size(batch);
        let mp = mm.mp;
        let padded = np != en || mp != em;
        let (dst, doff) = self.base(bufs, &plan.out_slot);
        let whole = doff == 0 && plan.out_strides.is_none();
        let narrow = (2 * en) % self.hw.word != 0 || plan.out_dtype != "half";
        let in_form = np != en && narrow && whole && is_float(&plan.out_dtype);
        let pkey = rows_key(en, np);
        let mut tmp = None;
        let mut key = None;
        let out_at = if padded {
            if in_form && mp == em {
                self.x.form(&dst, &pkey, 2 * bt * em * np)
            } else {
                let t = self.x.storage(2 * bt * mp * np);
                let at = self.x.addr(&t, 0);
                tmp = Some(t);
                at
            }
        } else {
            let (at, k) = self.out(plan, bufs)?;
            key = Some(k);
            at
        };
        let mut call = mm.kernel_call(
            expr_arg(plan.expr.as_ref().unwrap()),
            extras,
            np,
            (self.clusters(), self.cores()),
        );
        let binds = match self.prepared_mm(mm, &call.args, &binds)? {
            Some((made, mut plain)) => {
                call.args["made"] = made;
                plain.insert("out".into(), out_at);
                plain
            }
            None => {
                let mut binds = self.made_all(&binds)?;
                binds.insert("out".into(), out_at);
                binds
            }
        };
        self.x.launch(
            call,
            "matmul",
            json!({"plan": plan.name, "mm": mm.made_by()}),
            binds,
        );
        if let Some(k) = key {
            self.wrote(plan, bufs, &k);
            return Ok(());
        }
        let Some(tmp) = tmp else {
            self.x.wrote(&dst, &pkey, false);
            self.x.store_mut(&dst).dtype = Some(np_name(&plan.out_dtype));
            return Ok(());
        };
        let got = self.product_out(plan, bufs, &tmp, (batch, m, n, np, mp), in_form);
        self.x.free(&tmp);
        got
    }

    /// `_product_run`'s `prepared_mm` branch: the `made` argument and the
    /// binds (`p{i}_*` for `in{i}`) when a measured recipe wins.
    fn prepared_mm(
        &mut self,
        mm: &Mm,
        args: &Value,
        binds: &BTreeMap<String, Bound>,
    ) -> Result<Option<MadeBinds>, Refusal> {
        let pending: BTreeMap<usize, usize> = binds
            .iter()
            .filter_map(|(k, b)| match b {
                Bound::Pend(id) => Some((k[2..].parse().unwrap(), *id)),
                Bound::At(_) => None,
            })
            .collect();
        let steps: serde_json::Map<String, Value> = pending
            .iter()
            .map(|(i, id)| (i.to_string(), costs::steps_recipe(&self.pending[*id].steps)))
            .collect();
        let recipe = json!({
            "mm": mm.vars(),
            "e": args["e"],
            "extras": args["extras"],
            "out": args["out"],
            "steps": steps,
        });
        // The kernel's depth as it resolves it: a ring past the staging room
        // is the DRAM product (`call::mm_resolved`).
        let resolved = ktpu_kernels::call::mm_resolved(args, self.m)?;
        if pending.is_empty()
            || resolved["knobs"]["ring_depth"] != 0
            || !costs::choose("prepared_mm", &recipe, &self.m.name)
        {
            return Ok(None);
        }
        let mut plans = BTreeMap::new();
        for (i, id) in &pending {
            let Some(p) = self.plans_of(&self.pending[*id].steps.clone())? else {
                return Ok(None);
            };
            plans.insert(*i, p);
        }
        let mut plain: BTreeMap<String, Addr> = binds
            .iter()
            .filter_map(|(k, b)| match b {
                Bound::At(a) => Some((k.clone(), a.clone())),
                Bound::Pend(_) => None,
            })
            .collect();
        let mut made = serde_json::Map::new();
        for (i, id) in &pending {
            let steps = &self.pending[*id].steps;
            made.insert(i.to_string(), steps.iter().map(Step::made_by).collect());
            let src = steps[0].src.clone().expect("a first step reads a source");
            plain.insert(format!("p{i}_x"), src);
            self.step_binds(&plans[i], &format!("p{i}_"), &mut plain);
        }
        Ok(Some((Value::Object(made), plain)))
    }

    /// The [.., M, N] window of the [.., mp, Np] product in `tmp` into the
    /// output's padded-rows form, else the output itself.
    fn product_out(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        tmp: &str,
        (batch, m, n, np, mp): (&[Id], Id, Id, i64, i64),
        in_form: bool,
    ) -> Result<(), Refusal> {
        let cols = if in_form {
            self.fresh_ranges(plan, &[np])[0]
        } else {
            n
        };
        let ids: Vec<Id> = batch.iter().copied().chain([m, cols]).collect();
        let ext: Vec<i64> = batch
            .iter()
            .map(|r| self.extent(*r))
            .chain([mp, np])
            .collect();
        let mut coef: Vec<(Id, i64)> = Vec::new();
        let mut step = 1;
        for (r, e) in ids.iter().zip(&ext).rev() {
            coef.push((*r, step));
            step *= e;
        }
        if !in_form {
            let (tplan, tbufs, fin) = self.half_target(plan, bufs)?;
            self.moved(&tplan, &tbufs, tmp, &coef)?;
            self.finish_half(fin);
            return Ok(());
        }
        let (dst, _) = self.base(bufs, &plan.out_slot);
        let (em, en) = (self.extent(m), self.extent(n));
        let bt = self.size(batch);
        let pkey = rows_key(en, np);
        let form = self.x.form(&dst, &pkey, 2 * bt * em * np);
        let fbytes = self
            .x
            .store(&dst)
            .forms
            .iter()
            .find(|(k, _)| *k == pkey)
            .unwrap()
            .1;
        let (tkey, fkey) = (self.key("mmpad"), self.key("mmform"));
        let alias = self.x.alias(form, fbytes);
        let mut moves = bufs.clone();
        let tbytes = self.x.store(tmp).nbytes;
        moves.insert(
            tkey.clone(),
            View {
                base: tmp.into(),
                offset: 0,
                nbytes: tbytes,
            },
        );
        moves.insert(
            fkey.clone(),
            View {
                base: alias,
                offset: 0,
                nbytes: fbytes,
            },
        );
        let read = Access {
            slot: tkey,
            dtype: "half".into(),
            coef,
            off: 0,
            table: None,
            gate: false,
        };
        let lp = self.laid_plan(plan, fkey, &read, &ids, None);
        let got = self.bare(&lp, &moves)?;
        assert!(got, "an f16 output: the gather takes any read");
        self.x.wrote(&dst, &pkey, false);
        self.x.store_mut(&dst).dtype = Some(np_name(&plan.out_dtype));
        Ok(())
    }
}
