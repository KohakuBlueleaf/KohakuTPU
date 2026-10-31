//! A cluster's sweeps and the drains of their tiles (L2.MG.008-.014,
//! .020-.023).

use super::*;

impl<'a> Ctx<'a> {
    fn rows_of(&self, v: &Val, view: &Term, env: &Env, st: &Stmt) -> Result<Val> {
        let body = self.body;
        let Term::View(_, sl) = view else {
            unreachable!()
        };
        let (rows, cols) = (&sl[0], &sl[1]);
        let ok_cols = matches!(cols, Term::Slice(None, None));
        let Term::Slice(Some(lo_t), hi_t) = rows else {
            return Err(fail(
                body,
                st,
                "L2.MG.009",
                "a slice of an operand takes rows: X[LO : +R, :]",
            ));
        };
        if !ok_cols {
            return Err(fail(
                body,
                st,
                "L2.MG.009",
                "a slice of an operand takes rows: X[LO : +R, :]",
            ));
        }
        let lo = value(body, st, lo_t, env)?;
        let (pos_hi, hi_raw) = match hi_t.as_deref() {
            Some(Term::Pos(x)) => (true, (**x).clone()),
            Some(h) => (false, h.clone()),
            None => (false, Term::name("None")),
        };
        let hi = value(body, st, &hi_raw, env)?;
        let (Term::Int(lo, _), Term::Int(hi, _)) = (lo, hi) else {
            return Err(fail(
                body,
                st,
                "L2.MG.009",
                "an operand's rows are constants of the instance",
            ));
        };
        let (lo, hi) = (lo as i64, hi as i64);
        let n = if pos_hi { hi } else { hi - lo };
        let g = self.t.mg_lanes;
        if lo % g != 0 || n % g != 0 || lo + n > v.shape.0 {
            return Err(fail(
                body,
                st,
                "L2.MG.009",
                format!(
                    "rows {lo} +{n} of {}: whole groups of {g} inside",
                    v.shape.0
                ),
            ));
        }
        let mut out = v.clone();
        out.shape = (n, v.shape.1);
        out.entry = v.entry + (lo / g) * v.nk;
        out.flow = false;
        Ok(out)
    }

    pub(super) fn gemm(&mut self, op: &Op, env: &Env, seg: usize) -> Result<()> {
        let body = self.body;
        let st = &op.st;
        let t = self.ty(op, "acc")?;
        let form = "NAME = gemm A, B[, ACC] : f32[M, N] @acc";
        enum Opnd<'x> {
            Slice(&'x Term, String),
            Name(String),
        }
        let mut names: Vec<Opnd> = Vec::new();
        for a in &op.args {
            match a {
                Term::View(n, s) if s.len() == 2 && self.vals.contains_key(n) => {
                    names.push(Opnd::Slice(a, n.clone()))
                }
                Term::Name(n) => names.push(Opnd::Name(n.clone())),
                _ => return Err(fail(body, st, "L2.MG.020", form)),
            }
        }
        if names.len() != 2 && names.len() != 3 {
            return Err(fail(body, st, "L2.MG.020", form));
        }
        let mut ops = Vec::new();
        for n in &names[..2] {
            ops.push(match n {
                Opnd::Slice(v, nm) => Some(self.rows_of(&self.vals[nm].clone(), v, env, st)?),
                Opnd::Name(nm) => self.vals.get(nm).cloned(),
            });
        }
        let (a, b) = match (&ops[0], &ops[1]) {
            (Some(a), Some(b)) if a.kind == "A" && b.kind == "B" => (a.clone(), b.clone()),
            _ => {
                return Err(fail(
                    body,
                    st,
                    "L2.MG.020",
                    "gemm reads an @A value and an @B value of this cluster",
                ))
            }
        };
        let (m, n) = (t.shape[0], t.shape[1]);
        self.acc_fits(op, &t)?;
        if a.shape.0 != m || b.shape.0 != n || a.nk != b.nk {
            return Err(fail(
                body,
                st,
                "L2.MG.020",
                format!(
                    "gemm of ({}, {}) and ({}, {}) into ({m}, {n})",
                    a.shape.0, a.shape.1, b.shape.0, b.shape.1
                ),
            ));
        }
        if !self.t.legal_nk(a.nk) {
            return Err(fail(
                body,
                st,
                "L2.MG.008",
                format!(
                    "K = {}: a sweep takes 1 block or a whole number of {}",
                    a.nk * self.t.kblock,
                    self.t.kstep
                ),
            ));
        }
        let mut acc = 0;
        if names.len() == 3 {
            let Opnd::Name(cn) = &names[2] else {
                return Err(fail(
                    body,
                    st,
                    "L2.MG.020",
                    "the accumulator operand is a name",
                ));
            };
            match self.vals.get(cn) {
                Some(c) if c.kind == "acc" && c.shape == (m, n) => acc = if c.zero { 0 } else { 1 },
                _ => {
                    return Err(fail(
                        body,
                        st,
                        "L2.MG.020",
                        format!("{cn} is not an f32{} @acc value", py_list(&t.shape)),
                    ))
                }
            }
        }
        let g = Gemm {
            gm: m / self.t.mg_lanes,
            gn: n / self.t.mg_lanes,
            nk: a.nk,
            ab: a.bank,
            bb: b.bank,
            acc,
            aoff: a.entry,
            boff: b.entry,
            emit: None,
            to: false,
        };
        let mut line = Line::new("", seg);
        line.gemm = Some(g);
        self.s.lines.push(line);
        let at = self.s.lines.len() - 1;
        self.s.reading = (Some(a.bank), Some(b.bank));
        let unit = self.s.unit.clone();
        let bank_n = self.t.bank;
        let (alo, ahi) = a.span(bank_n, self.t.mg_lanes);
        let (blo, bhi) = b.span(bank_n, self.t.mg_lanes);
        self.s
            .accesses
            .push(Access::new(&unit, false, (&unit, "A"), alo, ahi).flowing(a.flow));
        self.s
            .accesses
            .push(Access::new(&unit, false, (&unit, "B"), blo, bhi).flowing(b.flow));
        let mut v = Val::new("acc", (m, n));
        v.gemm = Some(at);
        self.vals.insert(op.target.clone().unwrap(), v);
        Ok(())
    }

    pub(super) fn drain(
        &mut self,
        op: &Op,
        env: &Env,
        seg: usize,
        fuse: bool,
        links: &mut Links,
    ) -> Result<()> {
        let body = self.body;
        let st = &op.st;
        let all = st.positional();
        let add = all.iter().any(|p| p.as_name() == Some("add"));
        let pos: Vec<&Term> = all
            .into_iter()
            .filter(|p| p.as_name() != Some("add"))
            .collect();
        if pos.len() != 3 || !matches!(pos[0], Term::Name(_)) || !pos[1].is_arrow("->") {
            return Err(fail(
                body,
                st,
                "L2.MG.023",
                "drain ACC -> VIEW | drain ACC -> vc[H] | drain ACC -> mg[J] add [node=M]",
            ));
        }
        let kw = st.kwargs();
        if kw.iter().any(|(k, _)| *k != "node") {
            let mut keys: Vec<String> = kw.iter().map(|(k, _)| format!("'{k}'")).collect();
            keys.sort();
            return Err(fail(
                body,
                st,
                "L2.MG.023",
                format!("drain takes node=M only, got [{}]", keys.join(", ")),
            ));
        }
        let far = match st.kw("node") {
            Some(t) => Some(const_of(body, st, t, env)?),
            None => None,
        }
        .filter(|&f| f != self.s.node);
        let cname = pos[0].as_name().unwrap();
        let c = match self.vals.get(cname) {
            Some(c) if c.kind == "acc" && c.gemm.is_some() => c.clone(),
            _ => {
                return Err(fail(
                    body,
                    st,
                    "L2.MG.011",
                    format!("{cname} is no swept @acc value of this cluster"),
                ))
            }
        };
        let gi = c.gemm.unwrap();
        let view = match value(body, st, pos[2], env)? {
            Term::Name(n) => Term::View(n, vec![]),
            other => other,
        };
        let g = self.t.mg_lanes;
        let n = (c.shape.0 / g) * (c.shape.1 / g);
        let unit = self.s.unit.clone();
        if let Term::View(vn, sl) = &view {
            if vn == "mg" || vn == "vc" {
                let j = match sl.as_slice() {
                    [Term::Int(j, _)] => *j as i64,
                    _ => {
                        return Err(fail(
                            body,
                            st,
                            "L2.MG.012",
                            format!("drain ACC -> {vn}[J]: J a constant per instance"),
                        ))
                    }
                };
                if vn == "vc" {
                    if far.is_some() {
                        return Err(fail(
                            body,
                            st,
                            "L2.MG.014",
                            "drain ACC -> vc[H] node=M: a tile into another node's core",
                        ));
                    }
                    if add {
                        return Err(fail(
                            body,
                            st,
                            "L2.MG.012",
                            "drain ACC -> vc[H]: the tile's sub-tiles into core H's L1",
                        ));
                    }
                    let to = format!("vc[{j}]");
                    let x = links.send(body, st, &unit, &to, "l1", n, &[c.shape.0, c.shape.1])?;
                    let at = x.at;
                    if x.flow {
                        let g = self.s.lines[gi].gemm.as_mut().unwrap();
                        if g.emit.is_some() {
                            return Err(fail(
                                body,
                                st,
                                "L2.MG.022",
                                "a streamed tile leaves by its own last sweep",
                            ));
                        }
                        g.emit = Some(format!("to={to} l1={at} count credit"));
                        g.to = true;
                        self.s.held.push((
                            gi,
                            format!("drain n={n} to={to} l1={at} fused count credit"),
                        ));
                        self.s
                            .accesses
                            .push(Access::new(&unit, true, (&to, "l1"), at, at + n).flowing(true));
                        self.vals.get_mut(cname).unwrap().gemm = None;
                        return Ok(());
                    }
                    let mut ln = Line::new(format!("drain n={n} to={to} l1={at} count"), seg);
                    ln.drain = true;
                    self.s.lines.push(ln);
                    self.s
                        .accesses
                        .push(Access::new(&unit, true, (&to, "l1"), at, at + n));
                    self.vals.get_mut(cname).unwrap().gemm = None;
                    return Ok(());
                }
                if !add {
                    return Err(fail(
                        body,
                        st,
                        "L2.MG.023",
                        "drain ACC -> mg[J] add: into cluster J's tile, at full precision",
                    ));
                }
                let (text, key) = match far {
                    None => (
                        format!("drain n={n} to=mg[{j}] buf=2 l1=0"),
                        (format!("mg[{j}]"), "acc".to_string()),
                    ),
                    Some(f) => (
                        format!("drain n={n} to=mesh{f}.mg[{j}] buf=2 l1=0"),
                        (format!("node{f}"), format!("mg[{j}]")),
                    ),
                };
                let mut ln = Line::new(text, seg);
                ln.into = Some(j);
                ln.drain = true;
                ln.far = far;
                self.s.lines.push(ln);
                self.s
                    .accesses
                    .push(Access::new(&unit, true, (&key.0, &key.1), 0, 1));
                self.vals.get_mut(cname).unwrap().gemm = None;
                return Ok(());
            }
        }
        if far.is_some() {
            return Err(fail(
                body,
                st,
                "L2.MG.014",
                "node=M names another node's cluster: drain ACC -> mg[J] add node=M",
            ));
        }
        if add {
            return Err(fail(
                body,
                st,
                "L2.MG.023",
                "`add` is a drain into another cluster's accumulator",
            ));
        }
        let vname = match &view {
            Term::View(n, _) if body.buffers.contains_key(n) => n.clone(),
            other => {
                return Err(fail(
                    body,
                    st,
                    "L2.MG.011",
                    format!("drain to a memory view, got {}", repr(other)),
                ))
            }
        };
        let buf = &body.buffers[&vname];
        let off = (|| {
            let ax = M::axes_of(&view, &buf.shape)?;
            let got = M::shape_of(&ax);
            let want = vec![c.shape.0, c.shape.1];
            if got != want {
                return Err(format!(
                    "the view is {}, the tile {}",
                    super::super::mover::py_tuple(&got),
                    super::super::mover::py_tuple(&want)
                ));
            }
            M::drain_base(buf, &ax, self.t)
        })()
        .map_err(|e| fail(body, st, "L2.MG.011", e))?;
        let where_ = format!("{vname} + {off}");
        self.s
            .accesses
            .push(mem(&unit, true, &vname, off, off + n * M::subtile(self.t)));
        let g = self.s.lines[gi].gemm.as_mut().unwrap();
        if fuse && g.emit.is_none() {
            g.emit = Some(where_.clone());
            self.s
                .held
                .push((gi, format!("drain {where_} n={n} fused")));
        } else {
            let mut ln = Line::new(format!("drain {where_} n={n}"), seg);
            ln.drain = true;
            self.s.lines.push(ln);
        }
        self.vals.get_mut(cname).unwrap().gemm = None;
        Ok(())
    }
}
