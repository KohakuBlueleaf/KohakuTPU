//! Parallel typed row/statistic math with explicit operands and symbol order.

use super::*;
use crate::lower::vector::ctx::emit::typed::{symbol, Emitter};
use crate::lower::vector::gen::typed::{self as ir, Select, Symbol};

#[cfg(all(test, feature = "witness"))]
mod tests;

fn result(
    g: &mut Emitter,
    op: &str,
    code: &str,
    d: Symbol,
    syms: Vec<Symbol>,
    srcs: Vec<Term>,
    sel: Select,
) {
    let mut all = vec![d.clone()];
    all.extend(syms);
    if is_in(&CMP, op) {
        g.op(ir::math(code, Term::name("p1"), srcs, vec![]), all);
        g.op(
            ir::math(
                "vmov",
                d.operand(Select::Whole),
                vec![Term::name("k0")],
                vec![],
            ),
            vec![d.clone()],
        );
        g.op(
            ir::math(
                "vmov",
                d.operand(Select::Whole),
                vec![Term::name("k1")],
                vec![ir::kw("if", Term::name("p1"))],
            ),
            vec![d],
        );
    } else if op == "sel" {
        g.op(
            ir::math(
                "vsel",
                d.operand(Select::Whole),
                vec![srcs[1].clone(), srcs[2].clone(), srcs[0].clone()],
                vec![],
            ),
            all,
        );
    } else {
        let options = if sel == Select::Stride(0) {
            vec![ir::kw("vl", Term::int(16))]
        } else {
            vec![]
        };
        g.op(ir::math(code, d.operand(sel), srcs, options), all);
    }
}

impl Ctx<'_> {
    /// `op`'s first source through its domain guard ([`domain_constants`]);
    /// `srcs` and `syms`, holding that source alone, become the guarded copy.
    fn domain_typed(
        &mut self,
        g: &mut Emitter,
        op: &str,
        srcs: &mut Vec<Term>,
        syms: &mut Vec<Symbol>,
        sel: Select,
    ) -> VResult<()> {
        if domain_constants(op).is_empty() {
            return Ok(());
        }
        let vl = if sel == Select::Stride(0) {
            vec![ir::kw("vl", Term::int(16))]
        } else {
            vec![]
        };
        let predicated = |key: &str| {
            let mut options = vec![ir::kw(key, Term::name("p1"))];
            options.extend(vl.clone());
            options
        };
        let a = srcs[0].clone();
        let t = g.sym();
        let x = t.operand(sel);
        let mut reads = vec![t.clone()];
        reads.extend(syms.iter().cloned());

        if op == "exp2" {
            let mut s = reads.clone();
            let floor =
                self.constant_typed(g, EXP2_FLOOR, &mut s, std::slice::from_ref(&a), sel)?;
            g.op(
                ir::math("vmax", x.clone(), vec![a.clone(), floor], vl.clone()),
                s,
            );
        } else {
            g.op(
                ir::math("vabs", x.clone(), vec![a.clone()], vl.clone()),
                reads.clone(),
            );
        }
        let mut s = vec![t.clone()];
        let range = self.constant_typed(g, EXP2_RANGE, &mut s, std::slice::from_ref(&x), sel)?;
        g.op(
            ir::math(
                "vcmplt",
                Term::name("p1"),
                vec![x.clone(), range],
                vl.clone(),
            ),
            s,
        );
        if op == "exp2" {
            let doubled = ir::math(
                "vadd",
                x.clone(),
                vec![x.clone(), x.clone()],
                predicated("unless"),
            );
            g.op(doubled, vec![t.clone()]);
        } else {
            g.op(
                ir::math("vadd", x.clone(), vec![a.clone(), a.clone()], vl.clone()),
                reads.clone(),
            );
            g.op(
                ir::math("vmov", x.clone(), vec![a], predicated("if")),
                reads,
            );
        }

        *srcs = vec![x];
        *syms = vec![t];
        Ok(())
    }

    fn xlane_typed(
        &mut self,
        g: &mut Emitter,
        o: &Op,
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
    ) -> VResult<()> {
        let t = ty(o).clone();
        let k = xlane_k(self, o)?;
        if t.shape.len() == 1 {
            return Err(self.fail(
                &o.st,
                "L2.OP.022",
                format!("{} runs on rows (f32[R, C] @reg)", o.op),
            ));
        }
        let target = o.target.clone().unwrap();
        let existed = local.contains_key(&target);
        if !existed {
            let mut v = Val::new("row", count_of(&t, self.t.vlmax)).with_mode(mode_of(Some(&t)));
            v.cols = *t.shape.last().unwrap();
            local.insert(target.clone(), v);
        }
        let src = o.st.positional()[0].clone();
        let once = self.once(&local[&target], rows)?;
        let mode = code_of(&o.op).unwrap();
        for (r, i) in once {
            let v = self.get(o, &src, Some("row"), Some(local))?;
            let d = if self.inplace && existed {
                symbol(&local[&target].regs[&i])
            } else {
                g.sym()
            };
            let s = symbol(&v.regs[&self.index(&v, r)?]);
            g.op(
                ir::math(
                    "vmul",
                    d.operand(Select::Whole),
                    vec![
                        Term::name("k1"),
                        ir::crossbar(mode, s.operand(Select::Whole), k),
                    ],
                    vec![],
                ),
                vec![d.clone(), s],
            );
            local.get_mut(&target).unwrap().regs.insert(i, d.name());
        }
        Ok(())
    }

    pub(in crate::lower::vector::ctx) fn math_typed(
        &mut self,
        g: &mut Emitter,
        o: &Op,
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
    ) -> VResult<()> {
        let pos: Vec<Term> = o.st.positional().into_iter().cloned().collect();
        let t = match &o.ty {
            Some(t) if t.space == "reg" && o.target.is_some() => t.clone(),
            _ => {
                return Err(self.fail(
                    &o.st,
                    "L2.OP.019",
                    format!("NAME = {} ... : f32[..] @reg", o.op),
                ))
            }
        };
        if is_in(&XLANE, &o.op) {
            return self.xlane_typed(g, o, rows, local);
        }
        let math = [&UNARY[..], &BINARY[..], &TERNARY[..], &CMP[..]]
            .iter()
            .any(|t| is_in(t, &o.op));
        let code = if math { code_of(&o.op) } else { None }.or(if o.op == "sel" {
            Some("vsel")
        } else {
            None
        });
        let Some(code) = code else {
            return Err(self.fail(&o.st, "L2.OP.017", format!("no vector op '{}'", o.op)));
        };
        let want = if is_in(&UNARY, &o.op) {
            1
        } else if is_in(&BINARY, &o.op) || is_in(&CMP, &o.op) {
            2
        } else {
            3
        };
        if pos.len() != want {
            return Err(self.fail(
                &o.st,
                "L2.OP.017",
                format!("{} takes {want} operands", o.op),
            ));
        }
        let target = o.target.clone().unwrap();
        let inplace = self.inplace && local.contains_key(&target);
        let is_cmp = is_in(&CMP, &o.op);
        if t.shape.len() == 1 {
            if (is_cmp || o.op == "sel") && self.per_band == 0 {
                return Err(self.fail(
                    &o.st,
                    "L2.OP.008",
                    format!("{} runs on rows or GT4 statistics", o.op),
                ));
            }
            if !local.contains_key(&target) {
                local.insert(target.clone(), Val::new("stat", t.shape[0]));
            }
            let (per, sel) = if self.per_band != 0 {
                (self.per_band, Select::Whole)
            } else {
                (8, Select::Stride(0))
            };
            if self.per_band != 0 {
                local.get_mut(&target).unwrap().mode = "gt4".into();
            }
            let mut grs: Vec<i64> = rows.iter().map(|r| r / per).collect();
            grs.sort();
            grs.dedup();
            for grp in grs {
                let mut syms = vec![];
                let mut srcs = vec![];
                for (j, a) in pos.iter().enumerate() {
                    if let Some(f) = number(a) {
                        let s = self.constant_typed(g, f, &mut syms, &srcs, sel)?;
                        srcs.push(s);
                    } else {
                        let v = self.get(o, a, Some("stat"), Some(local))?;
                        let s = symbol(&v.regs[&grp]);
                        srcs.push(s.operand(sel));
                        syms.push(s);
                    }
                    if j == 0 {
                        self.domain_typed(g, &o.op, &mut srcs, &mut syms, sel)?;
                    }
                }
                let d = if inplace {
                    symbol(&local[&target].regs[&grp])
                } else {
                    g.sym()
                };
                result(g, &o.op, code, d.clone(), syms, srcs, sel);
                local.get_mut(&target).unwrap().regs.insert(grp, d.name());
            }
            return Ok(());
        }
        if !local.contains_key(&target) {
            let mut v = Val::new("row", count_of(&t, self.t.vlmax)).with_mode(mode_of(Some(&t)));
            v.cols = *t.shape.last().unwrap();
            local.insert(target.clone(), v);
        }
        let on_b = if o.op == "sel" { 2 } else { 1 };
        let once = self.once(&local[&target], rows)?;
        for (r, i) in once {
            let mut syms = vec![];
            let mut srcs = vec![];
            for (j, a) in pos.iter().enumerate() {
                'operand: {
                    if let Some(f) = number(a) {
                        let s = self.constant_typed(g, f, &mut syms, &srcs, Select::Whole)?;
                        srcs.push(s);
                        break 'operand;
                    }
                    if let Term::View(an, sl) = a {
                        if matches!(sl.last(), Some(Term::NewAxis))
                            && !on("l2.vector/broadcast-unread")
                        {
                            let v =
                                self.get(o, &Term::Name(an.clone()), Some("stat"), Some(local))?;
                            if self.per_band != 0 {
                                let s = symbol(&v.regs[&(r / self.per_band)]);
                                srcs.push(s.operand(Select::Whole));
                                syms.push(s);
                                break 'operand;
                            }
                            let s = symbol(&v.regs[&(r / 8)]);
                            let lane = ir::bcast(s.operand(Select::Chunk(0)), r % 8, 3);
                            if j != on_b {
                                let tsym = g.sym();
                                g.op(
                                    ir::math(
                                        "vmul",
                                        tsym.operand(Select::Whole),
                                        vec![Term::name("k1"), lane],
                                        vec![],
                                    ),
                                    vec![tsym.clone(), s],
                                );
                                srcs.push(tsym.operand(Select::Whole));
                                syms.push(tsym);
                                break 'operand;
                            }
                            srcs.push(lane);
                            syms.push(s);
                            break 'operand;
                        }
                    }
                    let v = self.get(o, a, Some("row"), Some(local))?;
                    let s = symbol(&v.regs[&self.index(&v, r)?]);
                    srcs.push(s.operand(Select::Whole));
                    syms.push(s);
                }
                if j == 0 {
                    self.domain_typed(g, &o.op, &mut srcs, &mut syms, Select::Whole)?;
                }
            }
            let d = if inplace {
                symbol(&local[&target].regs[&i])
            } else {
                g.sym()
            };
            result(g, &o.op, code, d.clone(), syms, srcs, Select::Whole);
            local.get_mut(&target).unwrap().regs.insert(i, d.name());
        }
        Ok(())
    }
}
