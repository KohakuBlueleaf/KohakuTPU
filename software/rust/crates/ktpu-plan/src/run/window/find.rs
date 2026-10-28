//! A plan as a window contraction (`window.find`), its gated reduced
//! ranges trimmed first (`window._trim`).

use super::*;

impl Runner<'_> {
    /// The accesses with each range of `cut` narrowed to its `[a, b)`,
    /// tables sliced (`_trim`, `_box` without the selectors).
    fn narrowed(
        &mut self,
        accesses: &[Access],
        cut: &[(Id, (i64, i64))],
    ) -> (Vec<Access>, Vec<(Id, Id)>) {
        let mut new: Vec<(Id, Id)> = Vec::new();
        for (r, (a, b)) in cut {
            let n = self.resized(*r, b - a);
            new.push((*r, n));
        }
        let renamed = |r: Id| new.iter().find(|(x, _)| *x == r).map_or(r, |(_, n)| *n);
        let bound = |r: Id| cut.iter().find(|(x, _)| *x == r).map(|(_, b)| *b);
        let mut out = Vec::new();
        for acc in accesses {
            let mut off = acc.off
                + acc
                    .coef
                    .iter()
                    .filter_map(|(r, c)| bound(*r).map(|(a, _)| c * a))
                    .sum::<i64>();
            let mut coef: Vec<(Id, i64)> =
                acc.coef.iter().map(|(r, c)| (renamed(*r), *c)).collect();
            let mut table = acc.table.clone();
            if acc.slot == Slot::Table && acc.coef.iter().any(|(r, _)| bound(*r).is_some()) {
                let ax = self.table_axes(acc);
                let bounds: Vec<(i64, i64)> = ax
                    .rs
                    .iter()
                    .zip(&ax.ext)
                    .map(|(r, e)| bound(*r).unwrap_or((0, *e)))
                    .collect();
                let bits: Vec<u16> = ax
                    .slice(&bounds)
                    .iter()
                    .map(|i| acc.table.as_ref().unwrap().bits[*i])
                    .collect();
                let keep: Vec<Id> = ax.rs.iter().map(|r| renamed(*r)).collect();
                coef = keep.iter().copied().zip(self.mult(&keep)).collect();
                off = 0;
                table = Some(Table {
                    shape: vec![bits.len()],
                    bits,
                });
            }
            out.push(Access {
                off,
                coef,
                table,
                ..acc.clone()
            });
        }
        (out, new)
    }

    /// `window._trim`: each reduced range a gate holds false outside one
    /// interval of narrowed to it.
    fn trim(&mut self, plan: &Plan, gates: &[usize], reduced: &[Id]) -> Plan {
        let mut cut: Vec<(Id, (i64, i64))> = Vec::new();
        for i in gates {
            let ax = self.table_axes(&plan.accesses[*i]);
            let st = ax.strides();
            for (k, r) in ax.rs.iter().enumerate() {
                if !reduced.contains(r) {
                    continue;
                }
                let on: Vec<i64> = (0..ax.ext[k])
                    .filter(|p| {
                        (0..ax.vals.len() as i64)
                            .any(|f| (f / st[k]) % ax.ext[k] == *p && ax.vals[f as usize] != 0.0)
                    })
                    .collect();
                if on.is_empty() || on[on.len() - 1] - on[0] + 1 != on.len() as i64 {
                    continue;
                }
                let e = self.extent(*r);
                let slot = match cut.iter().position(|(x, _)| x == r) {
                    Some(p) => p,
                    None => {
                        cut.push((*r, (0, e)));
                        cut.len() - 1
                    }
                };
                let (a, b) = cut[slot].1;
                cut[slot].1 = (a.max(on[0]), b.min(on[on.len() - 1] + 1));
            }
        }
        cut.retain(|(r, (a, b))| (*a, *b) != (0, self.extent(*r)) && a < b);
        if cut.is_empty() {
            return plan.clone();
        }
        let (accesses, new) = self.narrowed(&plan.accesses, &cut);
        let renamed = |r: &Id| new.iter().find(|(x, _)| x == r).map_or(*r, |(_, n)| *n);
        Plan {
            accesses,
            reduce_ranges: plan.reduce_ranges.iter().map(renamed).collect(),
            reductions: plan
                .reductions
                .iter()
                .map(|(k, v)| (k.clone(), v.iter().map(renamed).collect()))
                .collect(),
            ..plan.clone()
        }
    }

    /// `window.find`: `plan` as a window contraction, or None.
    pub(super) fn find(&mut self, plan: &Plan) -> Option<Contraction> {
        let (red, arg) = one_sum(plan)?;
        let (loads, gates, scale) = factors(&arg, &plan.accesses)?;
        if loads.len() != 2 {
            return None;
        }
        let (a0, b0) = (&plan.accesses[loads[0]], &plan.accesses[loads[1]]);
        if gates.is_empty() && !self.windowed(a0) && !self.windowed(b0) {
            return None;
        }
        let reduced = reductions_of(plan, &red);
        let plan = self.trim(plan, &gates, &reduced);
        let red_ranges = self.live(&reductions_of(&plan, &red));
        let (a0, b0) = (&plan.accesses[loads[0]], &plan.accesses[loads[1]]);
        let (rs, g) = if gates.is_empty() {
            (vec![], vec![])
        } else {
            self.gate_of(&plan, &gates)
        };
        let tied = if gates.is_empty() {
            vec![]
        } else {
            self.coupled(&rs, &g)
        };
        let side = |me: &Self, x: &Access| Side {
            acc: x.clone(),
            dims: me.dims(x, &tied),
            lane: None,
        };
        let (mut sa, mut sb) = (side(self, a0), side(self, b0));
        if !gates.is_empty() {
            if let Some(s) = self.boxed(&sa, &rs, &g, true) {
                sa = s;
            } else {
                sb = self.boxed(&sb, &rs, &g, true)?;
            }
        }
        let out = self.live(&plan.out_ranges);
        let (ra, rb) = (sa.reads(), sb.reads());
        if red_ranges
            .iter()
            .any(|r| !(ra.contains(r) && rb.contains(r)))
        {
            return None;
        }
        if out.iter().any(|r| !ra.contains(r) && !rb.contains(r)) {
            return None;
        }
        if ra
            .iter()
            .chain(&rb)
            .any(|r| !out.contains(r) && !red_ranges.contains(r))
        {
            return None;
        }
        let bt: Vec<Id> = out
            .iter()
            .copied()
            .filter(|r| ra.contains(r) && rb.contains(r))
            .collect();
        let mut ma: Vec<Id> = out
            .iter()
            .copied()
            .filter(|r| ra.contains(r) && !rb.contains(r))
            .collect();
        let mut nb: Vec<Id> = out
            .iter()
            .copied()
            .filter(|r| rb.contains(r) && !ra.contains(r))
            .collect();
        if self.size(&nb) > self.size(&ma) {
            std::mem::swap(&mut sa, &mut sb);
            std::mem::swap(&mut ma, &mut nb);
        }
        let plain = |me: &Self, s: &Side, r: Id| {
            let d = &s.dims[s.dim(r)];
            d.terms == [(r, 1)] && d.bx == (0, me.span(d))
        };
        let mut lane: Option<Id> = None;
        let h = self.hw.halves;
        for r in &red_ranges {
            if !(plain(self, &sa, *r) && plain(self, &sb, *r)) {
                continue;
            }
            let key = |x: Id| (self.extent(x) % h == 0, self.extent(x));
            if lane.is_none_or(|l| key(*r) > key(l)) {
                lane = Some(*r);
            }
        }
        let pad = match lane {
            Some(l) => self.hw.whole_halves(self.extent(l)),
            None => h,
        };
        if let Some(l) = lane {
            sa.lane = Some(sa.dim(l));
            sb.lane = Some(sb.dim(l));
        }
        let mut ks: Vec<Id> = red_ranges
            .iter()
            .copied()
            .filter(|r| Some(*r) != lane)
            .collect();
        ks.sort_by_key(|r| (-sa.dims[sa.dim(*r)].stride, self.range_id_of(*r)));
        Some(Contraction {
            plan,
            red,
            scale,
            a: sa,
            b: sb,
            bt,
            ma,
            nb,
            ks,
            pad,
        })
    }
}
