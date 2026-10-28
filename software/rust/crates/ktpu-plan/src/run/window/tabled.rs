//! Tabled sums (`window.tabled`, `_swapped`, `run._uploaded`): one sum of a
//! read times tables as a sum over the read's coordinates of the read
//! times one table accumulated on the host.

use super::*;

impl Runner<'_> {
    /// `window._over`: a table's values over `order`, each range it does not
    /// read broadcast.
    fn over(&self, acc: &Access, order: &[Id]) -> Vec<f64> {
        self.table_over(acc, order)
            .into_iter()
            .map(fold::f16_value)
            .collect()
    }

    /// `window.tabled`: one sum of a buffer read times tables coupling a
    /// kept range with a reduced one, as a sum over the read's source
    /// coordinates of the read times one table accumulated on the host.
    fn tabled(&mut self, plan: &Plan) -> Option<Plan> {
        let (red, arg) = one_sum(plan)?;
        let (loads, tables, scale) = weights(&arg, &plan.accesses)?;
        if loads.len() != 1 || tables.is_empty() {
            return None;
        }
        let x = plan.accesses[loads[0]].clone();
        let reduced = self.live(&reductions_of(plan, &red));
        let out = self.live(&plan.out_ranges);
        let lp: Vec<Id> = out.iter().chain(&reduced).copied().collect();
        let reads = |a: &Access| -> Vec<Id> {
            a.coef
                .iter()
                .filter(|(r, c)| *c != 0 && self.extent(*r) > 1)
                .map(|(r, _)| *r)
                .collect()
        };
        let tread: Vec<Id> = tables
            .iter()
            .flat_map(|(i, _)| reads(&plan.accesses[*i]))
            .collect();
        if tread.iter().chain(&reads(&x)).any(|r| !lp.contains(r)) {
            return None;
        }
        let tab: Vec<Id> = lp.iter().copied().filter(|r| tread.contains(r)).collect();
        if !tab.iter().any(|r| reduced.contains(r)) || !tab.iter().any(|r| out.contains(r)) {
            return None;
        }
        let mut t = vec![scale; self.size(&tab) as usize];
        for (i, how) in &tables {
            let v = self.over(&plan.accesses[*i], &tab);
            for (tj, vj) in t.iter_mut().zip(v) {
                *tj *= match how {
                    How::Value => vj,
                    How::On => (vj != 0.0) as i64 as f64,
                    How::Off => (vj == 0.0) as i64 as f64,
                };
            }
        }
        let nz: Vec<bool> = t.iter().map(|v| *v != 0.0).collect();
        let tied = self.coupled(&tab, &nz);
        let xs = self.dims(&x, &tied);
        let js: Vec<Dim> = xs
            .iter()
            .filter(|d| d.ranges().any(|r| reduced.contains(&r)))
            .cloned()
            .collect();
        let kept: Vec<Id> = xs
            .iter()
            .filter(|d| !js.contains(d))
            .flat_map(|d| d.ranges().collect::<Vec<_>>())
            .collect();
        let o_rs: Vec<Id> = out
            .iter()
            .copied()
            .filter(|r| tab.contains(r) || js.iter().any(|d| d.has(*r)))
            .collect();
        let gr: Vec<Id> = o_rs.iter().chain(&reduced).copied().collect();
        let gext: Vec<i64> = gr.iter().map(|r| self.extent(*r)).collect();
        let span: Vec<i64> = o_rs
            .iter()
            .map(|r| self.extent(*r))
            .chain(js.iter().map(|d| self.span(d)))
            .collect();
        let mut sst = vec![1i64; span.len()];
        for k in (0..span.len().saturating_sub(1)).rev() {
            sst[k] = sst[k + 1] * span[k + 1];
        }
        let tst: Vec<i64> = {
            let e: Vec<i64> = tab.iter().map(|r| self.extent(*r)).collect();
            let mut s = vec![1i64; e.len()];
            for k in (0..e.len().saturating_sub(1)).rev() {
                s[k] = s[k + 1] * e[k + 1];
            }
            s
        };
        let total: i64 = span.iter().product();
        let mut acc = vec![0f64; total as usize];
        let lows: Vec<i64> = js.iter().map(|d| self.lo(d)).collect();
        for at in grid(&gext) {
            let pos = |r: Id| at[gr.iter().position(|x| *x == r).unwrap()];
            let ti: i64 = tab.iter().zip(&tst).map(|(r, s)| pos(*r) * s).sum();
            let mut f: i64 = o_rs.iter().enumerate().map(|(k, r)| pos(*r) * sst[k]).sum();
            for (j, d) in js.iter().enumerate() {
                let c: i64 = d.terms.iter().map(|(r, m)| m * pos(*r)).sum::<i64>() - lows[j];
                f += c * sst[o_rs.len() + j];
            }
            acc[f as usize] += t[ti as usize];
        }
        if acc.iter().all(|v| *v == 0.0) {
            return Some(self.swapped(plan, &red, &expr::constant(0.0), &[], &[]));
        }
        let mut bx = Vec::new();
        for k in o_rs.len()..span.len() {
            let on: Vec<i64> = (0..span[k])
                .filter(|p| {
                    (0..total).any(|f| (f / sst[k]) % span[k] == *p && acc[f as usize] != 0.0)
                })
                .collect();
            bx.push((on[0], on[on.len() - 1] + 1));
        }
        let bounds: Vec<(i64, i64)> = o_rs
            .iter()
            .map(|r| (0, self.extent(*r)))
            .chain(bx.iter().copied())
            .collect();
        let cut = Axes {
            rs: vec![],
            ext: span.clone(),
            vals: vec![],
        };
        let bits: Vec<u16> = cut
            .slice(&bounds)
            .iter()
            .map(|i| fold::f16_bits(acc[*i]))
            .collect();
        let jr = self.fresh_ranges(plan, &bx.iter().map(|(a, b)| b - a).collect::<Vec<_>>());
        let mut xc: Vec<(Id, i64)> = x
            .coef
            .iter()
            .copied()
            .filter(|(r, _)| kept.contains(r))
            .collect();
        xc.extend(jr.iter().copied().zip(js.iter().map(|d| d.stride)));
        let xoff = x.off
            + js.iter()
                .zip(&bx)
                .map(|(d, (a, _))| d.stride * (self.lo(d) + a))
                .sum::<i64>();
        let xa = Access {
            slot: x.slot.clone(),
            dtype: x.dtype.clone(),
            coef: xc,
            off: xoff,
            table: None,
            gate: false,
        };
        let order: Vec<Id> = o_rs.iter().chain(&jr).copied().collect();
        let ta = Access {
            slot: Slot::Table,
            dtype: x.dtype.clone(),
            coef: self.dense(&order),
            off: 0,
            table: Some(Table {
                shape: vec![bits.len()],
                bits,
            }),
            gate: false,
        };
        let n = plan.accesses.len();
        let new = expr::reduce(
            "sum",
            expr::op("mul", vec![expr::load(n), expr::load(n + 1)]),
        );
        Some(self.swapped(plan, &red, &new, &[xa, ta], &jr))
    }

    /// `window._swapped`: the reduction `red` replaced by `new` (a reduction
    /// over `rs`, or none), reading `extra` after the plan's own accesses,
    /// unused accesses dropped.
    fn swapped(&self, plan: &Plan, red: &E, new: &E, extra: &[Access], rs: &[Id]) -> Plan {
        struct Walk<'a> {
            red: &'a E,
            new: &'a E,
            rs: &'a [Id],
            used: Vec<usize>,
            reductions: Vec<(E, Vec<Id>)>,
            memo: HashMap<usize, E>,
        }
        impl Walk<'_> {
            fn go(&mut self, x: &E) -> E {
                let id = Rc::as_ptr(x) as usize;
                if let Some(v) = self.memo.get(&id) {
                    return v.clone();
                }
                let got = if Rc::ptr_eq(x, self.red) {
                    match self.new.as_ref() {
                        Expr::Reduce(op, a) => {
                            let a = a.clone();
                            let got = expr::reduce(op, self.go(&a));
                            self.reductions.push((got.clone(), self.rs.to_vec()));
                            got
                        }
                        _ => self.new.clone(),
                    }
                } else {
                    match x.as_ref() {
                        Expr::Load(i) => {
                            let j = self.used.iter().position(|u| u == i).unwrap_or_else(|| {
                                self.used.push(*i);
                                self.used.len() - 1
                            });
                            expr::load(j)
                        }
                        Expr::Op(n, a) => {
                            let args = a.iter().map(|y| self.go(y)).collect();
                            expr::op(n, args)
                        }
                        _ => x.clone(),
                    }
                };
                self.memo.insert(id, got.clone());
                got
            }
        }
        let mut w = Walk {
            red,
            new,
            rs,
            used: Vec::new(),
            reductions: Vec::new(),
            memo: HashMap::new(),
        };
        let e = w.go(plan.expr.as_ref().unwrap());
        let all: Vec<&Access> = plan.accesses.iter().chain(extra).collect();
        Plan {
            expr: Some(e),
            accesses: w.used.iter().map(|i| all[*i].clone()).collect(),
            reduce_ranges: rs.to_vec(),
            reductions: w.reductions,
            ..plan.clone()
        }
    }

    /// `run._uploaded`: each folded table the reductions read uploaded as an
    /// f16 buffer.
    fn uploaded(&mut self, plan: &Plan, bufs: &Bufs) -> Result<(), Refusal> {
        fn walk(x: &E, inside: bool, under: &mut Vec<usize>) {
            match x.as_ref() {
                Expr::Load(i) if inside => {
                    if !under.contains(i) {
                        under.push(*i);
                    }
                }
                Expr::Op(_, a) => a.iter().for_each(|y| walk(y, inside, under)),
                Expr::Reduce(_, a) => walk(a, true, under),
                _ => {}
            }
        }
        let mut under = Vec::new();
        walk(plan.expr.as_ref().unwrap(), false, &mut under);
        under.sort();
        let mut bufs = bufs.clone();
        let mut accesses = plan.accesses.clone();
        let mut tmps = Vec::new();
        for i in under {
            let a = accesses[i].clone();
            if a.slot != Slot::Table {
                continue;
            }
            let bits = &a.table.as_ref().unwrap().bits;
            let nbytes = self.hw.f16_bytes(bits.len() as i64);
            let t = self.x.storage(nbytes);
            tmps.push(t.clone());
            self.write_u16(&t, bits);
            self.x.store_mut(&t).dtype = Some("float16".into());
            let key = self.key("table");
            bufs.insert(
                key.clone(),
                View {
                    base: t,
                    offset: 0,
                    nbytes,
                },
            );
            accesses[i] = Access {
                slot: key,
                dtype: "half".into(),
                table: None,
                ..a
            };
        }
        let p = Plan {
            accesses,
            ..plan.clone()
        };
        let got = self.vector(&p, &bufs);
        for t in tmps {
            self.x.free(&t);
        }
        got
    }

    /// `run.tabled` then `_uploaded`: false when the plan is not one.
    pub(in crate::run) fn tabled_run(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        match self.tabled(plan) {
            Some(tab) => self.uploaded(&tab, bufs).map(|_| true),
            None => Ok(false),
        }
    }
}
