//! Reductions over different range sets materialised one at a time
//! (`_materialise`, `_leaf`), a product under elementwise math found
//! (`_epilogue_contraction`, `_contraction`), a long range refolded
//! (`_refold`).

use super::*;

impl Runner<'_> {
    /// `_leaf`: `leaf` alone into a dense f16 temporary over the ranges
    /// outside its reduction; a product's leaf laid out so the clusters
    /// take it. `(plan, ranges, clusters)`.
    fn leaf(&mut self, plan: &Plan, leaf: &E) -> (Plan, Vec<Id>, bool) {
        let rin = reductions_of(plan, leaf);
        let (sub_e, sub_acc, remap) = prune(leaf, &plan.accesses);
        let mut outside: Vec<Id> = Vec::new();
        for a in &sub_acc {
            for (r, _) in &a.coef {
                if self.extent(*r) > 1 && !rin.contains(r) && !outside.contains(r) {
                    outside.push(*r);
                }
            }
        }
        let n = plan.out_ranges.len();
        outside.sort_by_key(|r| {
            let stride = sub_acc.iter().map(|a| a.c(*r)).max().unwrap_or(0);
            let rank = plan.out_ranges.iter().position(|x| x == r).unwrap_or(n);
            (-stride, rank, self.range_id(*r))
        });
        let base = Plan {
            kind: "vector".into(),
            name: format!("{}_r", plan.name),
            out_dtype: "half".into(),
            out_strides: None,
            expr: Some(sub_e),
            accesses: sub_acc,
            reduce_ranges: rin.clone(),
            reductions: remap
                .iter()
                .map(|(k, _)| (k.clone(), rin.clone()))
                .collect(),
            ..plan.clone()
        };
        let laid = |me: &Self, order: &[Id]| Plan {
            out_ranges: order.to_vec(),
            out_shape: order.iter().map(|r| me.extent(*r)).collect(),
            ..base.clone()
        };
        if outside.len() <= ORDER_TRIES {
            for order in permutations(&outside) {
                let p = laid(self, &order);
                let probe = self.coalesce(&p);
                if self.contraction(&probe).is_some() {
                    return (probe, order, true);
                }
            }
        }
        (laid(self, &outside), outside, false)
    }

    fn range_id(&self, r: Id) -> i64 {
        match self.g.arg(r) {
            Arg::Range { id, .. } => *id,
            _ => 0,
        }
    }

    /// `_epilogue_contraction`: a product under elementwise math the
    /// planner did not see as one; the clusters take its leaf.
    pub(in crate::run) fn epilogue_contraction(&mut self, plan: &Plan) -> bool {
        let e = plan.expr.as_ref().unwrap();
        if plan.reduce_ranges.is_empty() || matches!(e.as_ref(), Expr::Reduce(..)) {
            return false;
        }
        let leaf = leaf_reduce(e);
        if all_reduces(e).len() != 1 {
            return false;
        }
        self.leaf(plan, &leaf).2
    }

    /// `_contraction`: one sum of a product of two buffer reads as a matmul
    /// plan.
    pub(in crate::run) fn contraction(&self, plan: &Plan) -> Option<Plan> {
        let Expr::Reduce(op, arg) = plan.expr.as_ref()?.as_ref() else {
            return None;
        };
        let Expr::Op(name, args) = arg.as_ref() else {
            return None;
        };
        if op != "sum" || name != "mul" || args.len() != 2 {
            return None;
        }
        let mut reads = Vec::new();
        for x in args {
            let Expr::Load(i) = x.as_ref() else {
                return None;
            };
            reads.push(plan.accesses[*i].clone());
        }
        if reads.iter().any(|r| r.slot == Slot::Table) {
            return None;
        }
        let ks = self.live(&plan.reduce_ranges);
        if ks.is_empty() {
            return None;
        }
        for (a, b) in [(&reads[0], &reads[1]), (&reads[1], &reads[0])] {
            if let Some((m, n, batch)) = lower::mnk(&self.g, plan, a, b, &ks) {
                return Some(Plan {
                    kind: "matmul".into(),
                    expr: Some(expr::load(0)),
                    accesses: vec![],
                    a: Some(a.clone()),
                    b: Some(b.clone()),
                    mnk: Some((m, n, ks.clone())),
                    batch,
                    reduce_ranges: ks,
                    reductions: vec![],
                    ..plan.clone()
                });
            }
        }
        None
    }

    /// `_materialise`: an innermost reduction first into a temporary over
    /// the ranges outside it, read back by the rest.
    pub(in crate::run) fn materialise(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        let e = plan.expr.as_ref().unwrap();
        let leaf = leaf_reduce(e);
        let key = self.key("tmp");
        let (sub, outside, _) = self.leaf(plan, &leaf);
        let sub = Plan {
            out_slot: key.clone(),
            ..sub
        };
        let nbytes = self.hw.f16_bytes(self.size(&outside));
        let tmp = self.x.storage(nbytes);
        let read = Access {
            slot: key.clone(),
            dtype: "half".into(),
            coef: self.dense(&outside),
            off: 0,
            table: None,
            gate: false,
        };
        let nacc = plan.accesses.len();
        let mut reds: Vec<(E, Vec<Id>)> = Vec::new();
        let outer_e = rebuild(
            e,
            &mut |x| Rc::ptr_eq(x, &leaf).then(|| expr::load(nacc)),
            &mut |x, new| {
                if matches!(x.as_ref(), Expr::Reduce(..)) {
                    reds.push((new.clone(), reductions_of(plan, x)));
                }
                new
            },
        );
        let mut accs = plan.accesses.clone();
        accs.push(read);
        let (outer_e, outer_acc, remap) = prune(&outer_e, &accs);
        let left: Vec<(E, Vec<Id>)> = remap
            .iter()
            .map(|(k, v)| {
                let rs = &reds.iter().find(|(r, _)| Rc::ptr_eq(r, v)).unwrap().1;
                (k.clone(), rs.clone())
            })
            .collect();
        let mut rr: Vec<Id> = Vec::new();
        for r in left.iter().flat_map(|(_, rs)| rs) {
            if !rr.contains(r) {
                rr.push(*r);
            }
        }
        let outer = Plan {
            expr: Some(outer_e),
            accesses: outer_acc,
            reductions: left,
            reduce_ranges: rr,
            ..plan.clone()
        };
        let mut sb = bufs.clone();
        sb.insert(
            key,
            View {
                base: tmp.clone(),
                offset: 0,
                nbytes,
            },
        );
        let got = self
            .vector(&sub, &sb)
            .and_then(|_| self.vector(&outer, &sb));
        self.x.free(&tmp);
        got
    }

    /// `_refold`: one elementwise range longer than a row block viewed as
    /// [n / c, c] rows.
    pub(in crate::run) fn refold(&mut self, plan: &Plan) -> Option<Plan> {
        let plan = if plan.out_strides.is_none() {
            self.coalesce(plan)
        } else {
            plan.clone()
        };
        let live = self.live(&plan.out_ranges);
        if live.len() != 1 || !plan.reduce_ranges.is_empty() || plan.out_strides.is_some() {
            return None;
        }
        let r = live[0];
        let n = self.extent(r);
        let c = self
            .hw
            .blocks()
            .into_iter()
            .find(|w| n % w == 0 && n / w > 1);
        if n <= self.max_block() {
            return None;
        }
        let c = c?;
        let used: Vec<Id> = plan
            .accesses
            .iter()
            .flat_map(|a| a.coef.iter().map(|(q, _)| *q))
            .chain(plan.out_ranges.iter().copied())
            .collect();
        let nxt = 1 + used.iter().map(|q| self.range_id(*q)).max().unwrap();
        let axis = match self.g.arg(r) {
            Arg::Range { axis, .. } => axis.clone(),
            _ => unreachable!("an output range"),
        };
        let rr = self.g.range(n / c, nxt, &axis);
        let cc = self.g.range(c, nxt + 1, &axis);
        let split = |coef: &[(Id, i64)]| -> Vec<(Id, i64)> {
            coef.iter()
                .flat_map(|(q, s)| {
                    if *q == r {
                        vec![(rr, s * c), (cc, *s)]
                    } else {
                        vec![(*q, *s)]
                    }
                })
                .collect()
        };
        let out_ranges: Vec<Id> = plan
            .out_ranges
            .iter()
            .flat_map(|q| if *q == r { vec![rr, cc] } else { vec![*q] })
            .collect();
        Some(Plan {
            out_shape: out_ranges.iter().map(|q| self.extent(*q)).collect(),
            out_ranges,
            accesses: plan
                .accesses
                .iter()
                .map(|a| Access {
                    coef: split(&a.coef),
                    ..a.clone()
                })
                .collect(),
            ..plan.clone()
        })
    }
}
