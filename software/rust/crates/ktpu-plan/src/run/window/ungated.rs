//! Kept ranges only tables read (`run._table_only`, `_enumerated`) and
//! padded reads from zero-padded copies (`run._ungated`).

use super::*;

impl Runner<'_> {
    /// `run._table_only`: kept ranges only folded tables read.
    pub(in crate::run) fn table_only(&self, plan: &Plan) -> Vec<Id> {
        let read = |table: bool| -> Vec<Id> {
            plan.accesses
                .iter()
                .filter(|a| (a.slot == Slot::Table) == table)
                .flat_map(|a| a.coef.iter().filter(|(_, c)| *c != 0).map(|(r, _)| *r))
                .collect()
        };
        let (buf, tab) = (read(false), read(true));
        plan.out_ranges
            .iter()
            .copied()
            .filter(|r| self.extent(*r) > 1 && tab.contains(r) && !buf.contains(r))
            .collect()
    }

    /// `run._enumerated`: the plan once a value of the kept ranges
    /// `phases`, each into its slab of an f16 temporary, then the temporary
    /// moved into the output.
    pub(in crate::run) fn enumerated(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        phases: &[Id],
    ) -> Result<(), Refusal> {
        self.x.paths.insert("run._enumerated");
        let rest: Vec<Id> = plan
            .out_ranges
            .iter()
            .copied()
            .filter(|r| !phases.contains(r))
            .collect();
        let slab = self.hw.whole_halves(self.size(&rest));
        let strides: Vec<i64> = self
            .mult(phases)
            .into_iter()
            .map(|s| s * slab)
            .chain(self.mult(&rest))
            .collect();
        let order: Vec<Id> = phases.iter().chain(&rest).copied().collect();
        let key = self.key("phase");
        let nbytes = self.hw.f16_bytes(self.size(phases) * slab);
        let t = self.x.storage(nbytes);
        let got = (|| -> Result<(), Refusal> {
            let laid = Plan {
                out_slot: key.clone(),
                out_dtype: "half".into(),
                out_shape: order.iter().map(|r| self.extent(*r)).collect(),
                out_ranges: order.clone(),
                out_strides: Some(strides.clone()),
                ..plan.clone()
            };
            let mut tb = bufs.clone();
            tb.insert(
                key.clone(),
                View {
                    base: t.clone(),
                    offset: 0,
                    nbytes,
                },
            );
            let ext: Vec<i64> = phases.iter().map(|r| self.extent(*r)).collect();
            for at in grid(&ext) {
                let bx: Vec<(Id, (i64, i64))> = phases
                    .iter()
                    .zip(&at)
                    .map(|(r, v)| (*r, (*v, v + 1)))
                    .collect();
                let (sub, mut sb) = self.cut(&laid, &bx, &tb, &[]).expect("no selectors");
                let off = sb[&key].offset;
                sb.insert(
                    key.clone(),
                    View {
                        base: t.clone(),
                        offset: off,
                        nbytes: 2 * slab,
                    },
                );
                self.vector(&sub, &sb)?;
            }
            let read = Access {
                slot: key.clone(),
                dtype: "half".into(),
                coef: order.iter().copied().zip(strides.iter().copied()).collect(),
                off: 0,
                table: None,
                gate: false,
            };
            let moved = Plan {
                expr: Some(expr::load(0)),
                accesses: vec![read],
                reduce_ranges: vec![],
                reductions: vec![],
                ..plan.clone()
            };
            self.vector(&moved, &tb)
        })();
        self.x.free(&t);
        got
    }

    /// `run._ungated`: each padded read read from a zero-padded copy of its
    /// source over its coordinates; the gates stay selects.
    pub(in crate::run) fn ungated(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        self.x.paths.insert("run._ungated");
        fn walk(x: &E, gates: &[usize], plan: &Plan, guards: &mut Vec<(usize, Vec<usize>)>) {
            match x.as_ref() {
                Expr::Load(i) => {
                    if plan.accesses[*i].slot != Slot::Table && !gates.is_empty() {
                        let slot = match guards.iter().position(|(j, _)| j == i) {
                            Some(p) => p,
                            None => {
                                guards.push((*i, vec![]));
                                guards.len() - 1
                            }
                        };
                        for g in gates {
                            if !guards[slot].1.contains(g) {
                                guards[slot].1.push(*g);
                            }
                        }
                    }
                }
                Expr::Op(n, a) => {
                    if n == "where" {
                        if let Expr::Load(g) = a[0].as_ref() {
                            if plan.accesses[*g].gate {
                                let mut more = gates.to_vec();
                                if !more.contains(g) {
                                    more.push(*g);
                                }
                                walk(&a[1], &more, plan, guards);
                                walk(&a[2], gates, plan, guards);
                                return;
                            }
                        }
                    }
                    a.iter().for_each(|y| walk(y, gates, plan, guards));
                }
                Expr::Reduce(_, a) => walk(a, gates, plan, guards),
                Expr::Const(_) => {}
            }
        }
        let mut guards = Vec::new();
        walk(plan.expr.as_ref().unwrap(), &[], plan, &mut guards);
        let mut bufs = bufs.clone();
        let mut tmps = Vec::new();
        let mut accesses = plan.accesses.clone();
        let got = (|| -> Result<(), Refusal> {
            for (i, mut gs) in guards {
                let acc = accesses[i].clone();
                gs.sort();
                let (rs, g) = self.gate_of(plan, &gs);
                let mine: Vec<Id> = acc
                    .coef
                    .iter()
                    .filter(|(_, c)| *c != 0)
                    .map(|(r, _)| *r)
                    .collect();
                let keep: Vec<usize> = (0..rs.len()).filter(|k| mine.contains(&rs[*k])).collect();
                let ext: Vec<i64> = rs.iter().map(|r| self.extent(*r)).collect();
                let kext: Vec<i64> = keep.iter().map(|k| ext[*k]).collect();
                let mut kst = vec![1i64; keep.len()];
                for k in (0..keep.len().saturating_sub(1)).rev() {
                    kst[k] = kst[k + 1] * kext[k + 1];
                }
                let mut gk = vec![false; kext.iter().product::<i64>() as usize];
                for (p, at) in grid(&ext).iter().enumerate() {
                    if g[p] {
                        let f: i64 = keep.iter().zip(&kst).map(|(k, s)| at[*k] * s).sum();
                        gk[f as usize] = true;
                    }
                }
                let rs: Vec<Id> = keep.iter().map(|k| rs[*k]).collect();
                let tied = self.coupled(&rs, &gk);
                let side = Side {
                    acc: acc.clone(),
                    dims: self.dims(&acc, &tied),
                    lane: None,
                };
                if !gk.iter().any(|x| *x) {
                    accesses[i] = Access {
                        slot: Slot::Table,
                        dtype: "half".into(),
                        coef: vec![],
                        off: 0,
                        table: Some(Table {
                            shape: vec![1],
                            bits: vec![0],
                        }),
                        gate: false,
                    };
                    continue;
                }
                let Some(side) = self.boxed(&side, &rs, &gk, false) else {
                    return Err(Refusal::new(
                        "TG.RUN.010",
                        "a padded read whose gate reads a range it does not",
                    ));
                };
                let key = self.key("ungated");
                let axes: Vec<usize> = (0..side.dims.len()).collect();
                let (key, strides, off) = self.pad_copy(
                    &mut bufs, &mut tmps, &acc, &side.dims, &axes, &key, None, 1, false,
                )?;
                let coef = side
                    .dims
                    .iter()
                    .enumerate()
                    .flat_map(|(d, dim)| {
                        let sd = strides[&d];
                        dim.terms.iter().map(move |(r, m)| (*r, m * sd))
                    })
                    .collect();
                accesses[i] = Access {
                    slot: key,
                    dtype: "half".into(),
                    coef,
                    off,
                    table: None,
                    gate: false,
                };
            }
            let accesses: Vec<Access> = accesses
                .iter()
                .map(|a| Access {
                    gate: false,
                    ..a.clone()
                })
                .collect();
            let p = Plan {
                accesses,
                ..plan.clone()
            };
            self.vector(&p, &bufs)
        })();
        for t in tmps {
            self.x.free(&t);
        }
        got
    }
}
