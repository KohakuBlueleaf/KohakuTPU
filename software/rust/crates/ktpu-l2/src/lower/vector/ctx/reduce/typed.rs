//! Parallel typed reduction orchestration.

use super::*;
use crate::lower::vector::gen::typed::{self as ir, Select, Symbol};
use crate::lower::vector::tree::typed as tt;

#[cfg(all(test, feature = "witness"))]
mod tests;

fn symbol(s: &str) -> Symbol {
    Symbol(s.strip_prefix('%').unwrap().parse().unwrap())
}

impl Ctx<'_> {
    pub(in crate::lower::vector::ctx) fn reduces_typed(
        &mut self,
        g: &mut tt::Gen,
        o: &Op,
        grps: &[Vec<i64>],
        local: &mut IndexMap<String, Val>,
        fused: &IndexMap<String, (Term, Term)>,
    ) -> VResult<()> {
        let pos: Vec<Term> = o.st.positional().into_iter().cloned().collect();
        if pos.len() != 1 || o.ty.as_ref().map(|t| t.shape.len() != 1).unwrap_or(true) {
            return Err(self.fail(
                &o.st,
                "L2.OP.011",
                format!("NAME = {} ROWS : f32[R] @reg", o.op),
            ));
        }
        let code = code_of(&o.op).unwrap();
        let name = pos[0].as_name().map(str::to_string);
        let target = o.target.clone().unwrap();
        let inplace = self.inplace && local.contains_key(&target);
        if !local.contains_key(&target) {
            local.insert(target.clone(), Val::new("stat", ty(o).shape[0]));
        }
        if self.per_band != 0 {
            let mut cols: Vec<i64> = (0..self.per_band).collect();
            let mut arg = pos[0].clone();
            if let Term::View(an, sl) = &pos[0] {
                let bad = || {
                    self.fail(
                        &o.st,
                        "L2.OP.024",
                        "a reduction over X[:, C0:C1]: blocks of 32 columns",
                    )
                };
                let (rows_s, cols_s) = if sl.len() == 2 {
                    (Some(&sl[0]), Some(&sl[1]))
                } else {
                    (None, None)
                };
                let (lo, hi) = match cols_s {
                    Some(Term::Slice(lo, hi)) => (
                        lo.as_ref()
                            .map(|x| value(self.body, &o.st, x, &self.env))
                            .transpose()?,
                        hi.as_ref()
                            .map(|x| value(self.body, &o.st, x, &self.env))
                            .transpose()?,
                    ),
                    _ => (None, None),
                };
                let rows_whole = matches!(rows_s, Some(Term::Slice(None, None)));
                let (Some(Term::Int(lo, _)), Some(Term::Int(hi, _))) = (lo, hi) else {
                    return Err(bad());
                };
                let (lo, hi) = (lo as i64, hi as i64);
                if !rows_whole
                    || lo % 32 != 0
                    || hi % 32 != 0
                    || !(0 <= lo && lo < hi && hi <= 32 * self.per_band)
                {
                    return Err(bad());
                }
                cols = (lo / 32..hi / 32).collect();
                arg = Term::Name(an.clone());
            }
            let srcs = match name.as_ref().and_then(|n| fused.get(n)) {
                Some((a, b)) => vec![a.clone(), b.clone()],
                None => vec![arg],
            };
            let vals: Vec<Val> = srcs
                .iter()
                .map(|t| self.get(o, t, Some("row"), Some(local)))
                .collect::<VResult<_>>()?;
            local.get_mut(&target).unwrap().mode = "gt4".into();
            let mut trees = vec![];
            for grp in grps {
                let band = grp[0] / self.per_band;
                let into = if inplace {
                    local[&target].regs.get(&band).map(|s| symbol(s))
                } else {
                    None
                };
                let rs: Vec<_> = grp
                    .iter()
                    .copied()
                    .filter(|r| cols.contains(&(r % self.per_band)))
                    .collect();
                let mut regs = vec![];
                for &r in &rs {
                    if vals.len() == 2 {
                        let mut pair = vec![];
                        for v in &vals {
                            pair.push(symbol(&v.regs[&self.index(v, r)?]));
                        }
                        regs.push(pair);
                    } else {
                        regs.push(vec![symbol(&vals[0].regs[&r])]);
                    }
                }
                if let Some(width) =
                    o.st.kw("valid_cols")
                        .filter(|_| !crate::mutant::on("l2.class/no-mask"))
                {
                    let width = crate::ir::const_of(self.body, &o.st, width, &self.env)?;
                    if width <= 0
                        || width % 32 != 0
                        || width > self.per_band * 32
                        || vals.len() != 1
                    {
                        return Err(self.fail(
                            &o.st,
                            "L2.OP.024",
                            "valid_cols must select whole GT4 blocks",
                        ));
                    }
                    let stage = format!("s{}", self.t.stage_reg());
                    let neutral = if o.op == "rmax" {
                        let v = g.sym();
                        g.code.push(ir::Event::Fixed(ir::seti(
                            &stage,
                            Term::Neg(Box::new(Term::Float(f64::INFINITY))),
                        )));
                        g.emit(tt::Insn {
                            stmt: ir::math(
                                "vmov",
                                v.operand(Select::Whole),
                                vec![Term::name(&stage)],
                                vec![],
                            ),
                            syms: vec![v.clone()],
                        });
                        Some(v)
                    } else {
                        None
                    };
                    for (reg, r) in regs.iter_mut().zip(&rs) {
                        let masked = g.sym();
                        g.code.push(ir::Event::Fixed(ir::seti(
                            &stage,
                            Term::int(i128::from((r % self.per_band) * 32 < width)),
                        )));
                        let mut syms = vec![masked.clone(), reg[0].clone()];
                        let other = match &neutral {
                            Some(n) => {
                                syms.push(n.clone());
                                n.operand(Select::Whole)
                            }
                            None => Term::name("k0"),
                        };
                        g.emit(tt::Insn {
                            stmt: ir::math(
                                "vsel",
                                masked.operand(Select::Whole),
                                vec![reg[0].operand(Select::Whole), other, Term::name(&stage)],
                                vec![],
                            ),
                            syms,
                        });
                        reg[0] = masked;
                    }
                    self.staged.clear();
                }
                let (steps, d) = tt::tree4(g, code, &regs, into, vals.len() == 2);
                local.get_mut(&target).unwrap().regs.insert(band, d.name());
                trees.push(steps);
            }
            tt::interleave(g, &trees);
            if o.st.kw("carry").is_some() {
                if !self.classed {
                    return Err(self.fail(
                        &o.st,
                        "L2.OP.024",
                        "carried reductions require a classed stream",
                    ));
                }
                for (band, result) in local.get_mut(&target).unwrap().regs.iter_mut() {
                    if !grps
                        .iter()
                        .any(|group| group.first().is_some_and(|r| r / self.per_band == *band))
                    {
                        continue;
                    }
                    let (acc, _) = self
                        .carries
                        .entry(format!("{target}:{band}"))
                        .or_insert_with(|| (g.sym().name(), o.op == "rmax"));
                    self.pinned.insert(acc.clone());
                    let a = symbol(acc);
                    let r = symbol(result);
                    g.emit(tt::Insn {
                        stmt: ir::math(
                            code,
                            a.operand(Select::Whole),
                            vec![a.operand(Select::Whole), r.operand(Select::Whole)],
                            vec![],
                        ),
                        syms: vec![a.clone(), a, r],
                    });
                    *result = acc.clone();
                }
            }
            return Ok(());
        }
        let mut trees = vec![];
        for rws in grps {
            if rws.len() != 8 {
                return Err(self.fail(
                    &o.st,
                    "L2.OP.023",
                    "a reduction runs over groups of 8 rows",
                ));
            }
            let is_fused = name
                .as_ref()
                .map(|n| fused.contains_key(n))
                .unwrap_or(false);
            let rows = if is_fused {
                let (a, b) = fused[name.as_ref().unwrap()].clone();
                let a = self.get(o, &a, Some("row"), Some(local))?;
                let b = self.get(o, &b, Some("row"), Some(local))?;
                let mut out = vec![];
                for &r in rws {
                    out.push(vec![
                        symbol(&a.regs[&self.index(&a, r)?]),
                        symbol(&b.regs[&self.index(&b, r)?]),
                    ]);
                }
                out
            } else {
                let src = self.get(o, &pos[0], Some("row"), Some(local))?;
                rws.iter().map(|r| vec![symbol(&src.regs[r])]).collect()
            };
            let into = if inplace {
                local[&target].regs.get(&(rws[0] / 8)).map(|s| symbol(s))
            } else {
                None
            };
            let (steps, result) = tt::tree(g, code, &rows, is_fused, into);
            trees.push(steps);
            local
                .get_mut(&target)
                .unwrap()
                .regs
                .insert(rws[0] / 8, result.name());
        }
        tt::interleave(g, &trees);
        Ok(())
    }
}
