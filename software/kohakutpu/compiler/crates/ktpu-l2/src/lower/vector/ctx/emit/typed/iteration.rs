//! Flat typed iteration events; block nesting follows complete band emission.

use super::*;
use crate::lower::vector::tree::typed as tt;

// Transfer plans store unit keys, not instruction text; materialize their indexed operand.
fn unit_operand(key: &str) -> Term {
    let (name, index) = key.split_once('[').expect("indexed transfer unit");
    let index: i64 = index.strip_suffix(']').unwrap().parse().unwrap();
    Term::View(name.into(), vec![Term::dec(index.into())])
}

impl Ctx<'_> {
    pub(in crate::lower::vector::ctx) fn iteration_typed(
        &mut self,
        g: &mut Emitter,
        body: &[Arc<Node>],
        parity: usize,
        prefetch: bool,
    ) -> VResult<()> {
        let prefetch = prefetch && !on("l2.vector/no-prefetch");
        let ops = ops_of(body);
        if ops.is_empty() {
            return Err(VErr::Value("max() iterable argument is empty".into()));
        }
        let has_if = body.iter().any(|o| matches!(o.as_ref(), Node::If(_)));
        if has_if {
            self.check_if(body)?;
        }
        let reduces = ops.iter().any(|o| is_in(&REDUCE, o.op_name()));
        let mut typed: Vec<&Op> = ops
            .iter()
            .map(|o| op(o))
            .filter(|o| {
                o.ty.as_ref()
                    .map(|t| t.space == "l1" || t.space == "reg")
                    .unwrap_or(false)
            })
            .collect();
        let mut before: Vec<Val> = Vec::new();
        for o in &ops {
            for a in op(o).st.positional() {
                if let Some(n) = match a {
                    Term::View(n, _) | Term::Name(n) => Some(n),
                    _ => None,
                } {
                    if let Some(v) = self.vals.get(n) {
                        before.push(v.clone());
                    }
                }
            }
        }
        before.retain(|v| v.kind == "row" && v.mode == "gt4" && v.cols != 0);
        let stats =
            !typed.is_empty() && typed.iter().all(|o| ty(o).shape.len() == 1) && before.is_empty();
        let gt4 = stats
            || !before.is_empty()
            || ops.iter().any(|o| {
                o.ty()
                    .map(|t| t.mode.as_deref() == Some("gt4"))
                    .unwrap_or(false)
            });
        let first_st = op(&ops[0]).st.clone();
        if gt4 {
            typed.retain(|o| ty(o).shape.len() == 2);
            let mut widths: Vec<i64> = typed
                .iter()
                .filter(|o| mode_of(o.ty.as_ref()) == "gt4")
                .map(|o| ty(o).shape[1])
                .collect();
            widths.extend(before.iter().map(|v| v.cols));
            let wide = widths.iter().copied().chain([32]).max().unwrap();
            if wide % 32 != 0 || widths.iter().any(|w| *w != wide && *w != 32) {
                return Err(self.fail(
                    &first_st,
                    "L2.REF.002",
                    "GT4 values of one width, a multiple of 32",
                ));
            }
            self.per_band = wide / 32;
        } else {
            let two: Vec<&Op> = typed
                .iter()
                .copied()
                .filter(|o| ty(o).shape.len() == 2)
                .collect();
            if !two.is_empty() {
                typed = two;
            }
        }
        let mut rows: Vec<i64> = typed
            .iter()
            .map(|o| count_of(ty(o), self.t.vlmax))
            .collect();
        if stats {
            let m = ops
                .iter()
                .filter_map(|o| o.ty().filter(|t| !t.shape.is_empty()).map(|t| t.shape[0]))
                .max()
                .unwrap();
            rows = vec![m / 4];
        }
        rows.sort();
        rows.dedup();
        let tile = rows
            .iter()
            .copied()
            .chain(before.iter().filter(|_| gt4).map(|v| v.rows))
            .max()
            .ok_or_else(|| VErr::Value("max() iterable argument is empty".into()))?;
        self.tile = tile;
        let narrow: Vec<i64> = if gt4 && self.per_band != 0 {
            vec![tile / self.per_band, self.per_band]
        } else {
            vec![]
        };
        if rows
            .iter()
            .any(|r| *r != tile && *r != 1 && !narrow.contains(r))
        {
            return Err(self.fail(
                &first_st,
                "L2.REF.002",
                format!(
                    "one register count in the stream loop, got {}",
                    crate::lower::cluster::py_list(&rows)
                ),
            ));
        }
        let group = if gt4 {
            self.per_band
        } else if reduces {
            8
        } else {
            4.min(tile)
        };
        if tile % group != 0 {
            return Err(self.fail(
                &first_st,
                "L2.REF.002",
                format!("{tile} rows do not split into groups of {group}"),
            ));
        }
        let stores: Vec<&Op> = ops
            .iter()
            .map(|o| op(o))
            .filter(|o| o.op == "store" || o.op == "send")
            .collect();
        let packed: HashSet<&str> = ops
            .iter()
            .map(|o| op(o))
            .filter(|o| o.op == "pack")
            .filter_map(|o| o.target.as_deref())
            .collect();
        let direct: Vec<&Op> = stores
            .iter()
            .copied()
            .filter(|o| {
                let at = if o.op == "send" { 0 } else { 2 };
                match o.st.positional().get(at) {
                    Some(Term::Name(n)) => !packed.contains(n.as_str()),
                    _ => true,
                }
            })
            .collect();
        let shared: Vec<&Op> = stores
            .iter()
            .copied()
            .filter(|o| self.descs[self.of[&o.uid]].shared.is_some())
            .collect();
        g.fixed(ir::instruction("vbar", vec![], vec![]));
        if prefetch {
            g.fixed(ir::sync("fill", "unpack", None));
            if !direct.is_empty() || !shared.is_empty() {
                g.fixed(ir::sync("fill", "drain", None));
            }
            for &d in &self.loads {
                let d = &self.descs[d];
                self.fill_phase(g, d, d.buf[1 - parity], true, self.phase + 1);
            }
        }
        let streams: Vec<&Op> = ops
            .iter()
            .map(|o| op(o))
            .filter(|o| o.op == "recv")
            .collect();
        for o in &ops {
            let o = op(o);
            if o.op == "load" {
                let d = &self.descs[self.of[&o.uid]];
                let v = l1val(Some(d), ty(o), d.buf[parity], self.t);
                self.vals.insert(o.target.clone().unwrap(), v);
            } else if o.op == "recv" {
                let PlanX::S(x) = &self.plan[&o.uid] else {
                    unreachable!()
                };
                let mut v = Val::new("l1", count_of(ty(o), self.t.vlmax)).with_mode("gt4");
                v.buf = x.slots[parity];
                g.fixed(ir::wait("unpack", "rx", x.size));
                self.vals.insert(o.target.clone().unwrap(), v);
            }
        }
        let mut local: IndexMap<String, Val> = IndexMap::new();
        let groups: Vec<Vec<i64>> = (0..tile)
            .step_by(group as usize)
            .map(|k| (k..k + group).collect())
            .collect();
        if self.band < groups.len() && !groups.len().is_multiple_of(self.band) {
            return Err(VErr::Pressure);
        }
        let width = self.band.min(groups.len());
        if has_if && width != groups.len() {
            return Err(VErr::Pressure);
        }
        let bands: Vec<Vec<Vec<i64>>> = groups.chunks(width).map(|c| c.to_vec()).collect();
        let fused = fusions(&ops);
        let first_reduce = body
            .iter()
            .position(|o| matches!(o.as_ref(), Node::Op(x) if is_in(&REDUCE, &x.op)));
        if self.ahead {
            self.unpacks_typed(g, &ops, &flat(&bands[0]), &mut local, None)?;
        }
        for (k, grps) in bands.iter().enumerate() {
            let rws = flat(grps);
            if !self.ahead {
                self.unpacks_typed(g, &ops, &rws, &mut local, None)?;
            } else if k + 1 < bands.len() {
                self.unpacks_typed(g, &ops, &flat(&bands[k + 1]), &mut local, None)?;
            }
            let mut again: HashSet<String> = HashSet::new();
            for (i, n) in body.iter().enumerate() {
                if let Node::If(blk) = n.as_ref() {
                    if !self.pin {
                        for b in ops_of(&blk.body) {
                            self.unpin_typed(
                                g,
                                op(&b),
                                &rws,
                                &mut local,
                                &mut again,
                                Some(&fused),
                            )?;
                        }
                    }
                    g.fixed(ir::sync("fe", "math", None));
                    g.fixed(ir::instruction(
                        "getstk",
                        vec![Term::name(format!("s{}", self.t.stk_s()))],
                        vec![],
                    ));
                    g.fixed(ir::instruction(
                        "skipz",
                        vec![Term::name(format!("s{}", self.t.stk_s()))],
                        vec![],
                    ));
                    g.code.push(Event::Open);
                    self.inplace = true;
                    for b in &blk.body {
                        let Node::Op(x) = b.as_ref() else {
                            if crate::mutant::on("l2.vector/sticky-unchecked") {
                                continue;
                            }
                            return Err(self.fail(
                                b.st(),
                                "L2.OP.027",
                                "if sticky holds math and reductions",
                            ));
                        };
                        self.compute_typed(g, x, grps, &rws, &mut local, &fused)?;
                    }
                    self.inplace = false;
                    g.code.push(Event::Close);
                    continue;
                }
                let o = op(n);
                if ["load", "store", "send", "recv", "unpack", "pack"].contains(&o.op.as_str())
                    || o.target
                        .as_ref()
                        .map(|t| fused.contains_key(t))
                        .unwrap_or(false)
                {
                    continue;
                }
                if self.remat && first_reduce.map(|f| i > f).unwrap_or(false) {
                    self.reunpack_typed(g, &ops, o, &rws, &mut local, &mut again)?;
                }
                if !self.pin {
                    self.unpin_typed(g, o, &rws, &mut local, &mut again, Some(&fused))?;
                }
                self.compute_typed(g, o, grps, &rws, &mut local, &fused)?;
            }
            if !self.pin {
                for o in &ops {
                    if op(o).op == "pack" {
                        self.unpin_typed(g, op(o), &rws, &mut local, &mut again, None)?;
                    }
                }
            }
            if k == 0 {
                g.fixed(ir::sync(
                    "pack",
                    "drain",
                    Some(Term::int(if self.classed {
                        0
                    } else {
                        stores.len() as i128
                    })),
                ));
                if !shared.is_empty() {
                    g.fixed(ir::sync("pack", "unpack", None));
                }
            }
            self.packs_typed(g, &ops, &rws, &mut local, parity)?;
        }
        for o in &streams {
            let PlanX::S(x) = &self.plan[&o.uid] else {
                unreachable!()
            };
            g.fixed(ir::sync("drain", "unpack", None));
            g.fixed(ir::instruction(
                "vpost",
                vec![unit_operand(&x.src)],
                vec![ir::kw("n", Term::dec(x.size.into()))],
            ));
        }
        g.fixed(ir::sync("drain", "pack", None));
        if !direct.is_empty() {
            let slack = if prefetch { self.loads.len() } else { 0 };
            g.fixed(ir::sync("drain", "fill", Some(Term::int(slack as i128))));
        }
        for o in &stores {
            let mut d = &self.descs[self.of[&o.uid]];
            if let (Some(alt), 1) = (d.alt, parity) {
                d = &self.descs[alt];
            }
            let at_ix = if o.op == "send" { 0 } else { 2 };
            let name = o.st.positional()[at_ix].clone();
            let src = self.get(o, &name, Some("l1"), Some(&local))?;
            let mut options = vec![];
            if !d.to.is_empty() {
                options.push(ir::kw("to", unit_operand(&d.to)));
                options.push(ir::kw("buf", Term::dec(d.side.into())));
            }
            if d.credit {
                g.fixed(ir::wait("drain", "cr", d.words));
            }
            g.fixed(
                ir::memory(
                    ir::Memory::Drain,
                    Term::name(format!("a{}", d.ad)),
                    src.buf,
                    None,
                    true,
                    options,
                )
                .unwrap(),
            );
        }
        Ok(())
    }

    fn compute_typed(
        &mut self,
        g: &mut Emitter,
        o: &Op,
        grps: &[Vec<i64>],
        rws: &[i64],
        local: &mut IndexMap<String, Val>,
        fused: &IndexMap<String, (Term, Term)>,
    ) -> VResult<()> {
        if is_in(&REDUCE, &o.op) {
            let mut tree = tt::Gen {
                n: g.n,
                code: std::mem::take(&mut g.code),
            };
            let result = self.reduces_typed(&mut tree, o, grps, local, fused);
            g.n = tree.n;
            g.code = tree.code;
            result
        } else {
            self.math_typed(g, o, rws, local)
        }
    }
}
