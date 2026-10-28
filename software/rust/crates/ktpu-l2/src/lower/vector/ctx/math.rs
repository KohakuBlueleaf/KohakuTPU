//! Row and statistic math (L2.OP.001-.008, .017, .019, .022): one V2
//! instruction a register, compares through p1, lane crossings.

use super::*;

mod typed;

#[cfg(feature = "witness")]
impl<'a> Ctx<'a> {
    /// `y = xlane x, K` / `y = xrot x, K` on a multiply by 1.0 (exact).
    fn xlane(
        &mut self,
        g: &mut Gen,
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
                local[&target].regs[&i].clone()
            } else {
                g.sym()
            };
            let s = v.regs[&self.index(&v, r)?].clone();
            g.emit(format!("vmul {{0}}, k1, {mode}({{1}}, {k})"), &[&d, &s]);
            local.get_mut(&target).unwrap().regs.insert(i, d);
        }
        Ok(())
    }

    pub(super) fn math(
        &mut self,
        g: &mut Gen,
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
            return self.xlane(g, o, rows, local);
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
            let (per, sel, vl) = if self.per_band != 0 {
                (self.per_band, "", "")
            } else {
                (8, "[0:]", " vl=16")
            };
            if self.per_band != 0 {
                local.get_mut(&target).unwrap().mode = "gt4".into();
            }
            let mut grs: Vec<i64> = rows.iter().map(|r| r / per).collect();
            grs.sort();
            grs.dedup();
            for grp in grs {
                let mut syms: Vec<String> = Vec::new();
                let mut srcs: Vec<String> = Vec::new();
                for a in &pos {
                    if let Some(f) = number(a) {
                        let s = self.constant(g, f, &mut syms, &srcs, sel)?;
                        srcs.push(s);
                        continue;
                    }
                    let v = self.get(o, a, Some("stat"), Some(local))?;
                    syms.push(v.regs[&grp].clone());
                    srcs.push(format!("{{{}}}{sel}", syms.len()));
                }
                let d = if inplace {
                    local[&target].regs[&grp].clone()
                } else {
                    g.sym()
                };
                let mut all = vec![d.clone()];
                all.extend(syms.iter().cloned());
                if is_cmp {
                    g.emit_owned(format!("{code} p1, {}", srcs.join(", ")), all);
                    g.emit("vmov {0}, k0", &[&d]);
                    g.emit("vmov {0}, k1 if=p1", &[&d]);
                } else if o.op == "sel" {
                    g.emit_owned(
                        format!("vsel {{0}}, {}, {}, {}", srcs[1], srcs[2], srcs[0]),
                        all,
                    );
                } else {
                    g.emit_owned(format!("{code} {{0}}{sel}, {}{vl}", srcs.join(", ")), all);
                }
                local.get_mut(&target).unwrap().regs.insert(grp, d);
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
            let mut syms: Vec<String> = Vec::new();
            let mut srcs: Vec<String> = Vec::new();
            for (j, a) in pos.iter().enumerate() {
                if let Some(f) = number(a) {
                    let s = self.constant(g, f, &mut syms, &srcs, "")?;
                    srcs.push(s);
                    continue;
                }
                if let Term::View(an, sl) = a {
                    if matches!(sl.last(), Some(Term::NewAxis)) && !on("l2.vector/broadcast-unread")
                    {
                        let v = self.get(o, &Term::Name(an.clone()), Some("stat"), Some(local))?;
                        if self.per_band != 0 {
                            syms.push(v.regs[&(r / self.per_band)].clone());
                            srcs.push(format!("{{{}}}", syms.len()));
                            continue;
                        }
                        let lane = format!("bcast({{1}}[0], lane={}, sh=3)", r % 8);
                        if j != on_b {
                            let tsym = g.sym();
                            g.emit(
                                format!("vmul {{0}}, k1, {lane}"),
                                &[&tsym, &v.regs[&(r / 8)]],
                            );
                            syms.push(tsym);
                            srcs.push(format!("{{{}}}", syms.len()));
                            continue;
                        }
                        syms.push(v.regs[&(r / 8)].clone());
                        srcs.push(lane.replace("{1}", &format!("{{{}}}", syms.len())));
                        continue;
                    }
                }
                let v = self.get(o, a, Some("row"), Some(local))?;
                syms.push(v.regs[&self.index(&v, r)?].clone());
                srcs.push(format!("{{{}}}", syms.len()));
            }
            let d = if inplace {
                local[&target].regs[&i].clone()
            } else {
                g.sym()
            };
            let mut all = vec![d.clone()];
            all.extend(syms.iter().cloned());
            if is_cmp {
                g.emit_owned(format!("{code} p1, {}", srcs.join(", ")), all);
                g.emit("vmov {0}, k0", &[&d]);
                g.emit("vmov {0}, k1 if=p1", &[&d]);
            } else if o.op == "sel" {
                g.emit_owned(
                    format!("vsel {{0}}, {}, {}, {}", srcs[1], srcs[2], srcs[0]),
                    all,
                );
            } else {
                g.emit_owned(format!("{code} {{0}}, {}", srcs.join(", ")), all);
            }
            local.get_mut(&target).unwrap().regs.insert(i, d);
        }
        Ok(())
    }
}
