//! The instance's code: constants (L2.VC.012), the prologue before the
//! stream loop, and one loop iteration's band order (L2.VC.005, .013, .016).

use super::*;

pub(super) mod typed;

impl<'a> Ctx<'a> {
    // ------------------------------------------------------------- constants

    /// Every number an op reads in a scalar register, the most read first;
    /// past the scalar file held in a pinned register or set before a read.
    #[cfg(feature = "witness")]
    pub fn constants(&mut self, g: &mut Gen, ops: &[Arc<Node>]) {
        let fs = numbers(ops);
        let stk = self.t.stk_s() as usize;
        let kept = if fs.len() <= stk { fs.len() } else { stk - 1 };
        for (i, f) in fs[..kept].iter().enumerate() {
            self.scalars.insert(FKey::of(*f), format!("s{i}"));
            g.raw(format!("seti s{i} = {}", super::super::fmt_float(*f)));
        }
        if self.hold {
            let stage = stage_regs(self.t);
            for f in &fs[kept..] {
                let v = g.sym();
                self.pinned.insert(v.clone());
                self.held.insert(FKey::of(*f), v.clone());
                g.raw(format!(
                    "seti {} = {}",
                    stage[0],
                    super::super::fmt_float(*f)
                ));
                g.emit(format!("vmov {{0}}, {}", stage[0]), &[&v]);
            }
        }
    }

    /// `f`'s source for an op whose sources so far are `srcs`; a held
    /// constant's register joins `syms`.
    #[cfg(feature = "witness")]
    pub(super) fn constant(
        &mut self,
        g: &mut Gen,
        f: f64,
        syms: &mut Vec<String>,
        srcs: &[String],
        sel: &str,
    ) -> VResult<String> {
        let k = FKey::of(f);
        if let Some(v) = self.held.get(&k) {
            syms.push(v.clone());
            return Ok(format!("{{{}}}{sel}", syms.len()));
        }
        if let Some(s) = kreg(f) {
            return Ok(s.to_string());
        }
        if let Some(s) = self.scalars.get(&k) {
            return Ok(s.clone());
        }
        for (reg, (got, at)) in &self.staged {
            let crossed = g.code[*at..].iter().any(Code::is_block_edge)
                && !crate::mutant::on("l2.vector/staged-across-blocks");
            if *got == k && !crossed {
                return Ok(reg.clone());
            }
        }
        let stage = stage_regs(self.t);
        let Some(reg) = stage.iter().find(|r| !srcs.contains(r)).cloned() else {
            return Err(VErr::Pressure);
        };
        g.raw(format!("seti {reg} = {}", super::super::fmt_float(f)));
        self.staged.insert(reg.clone(), (k, g.code.len()));
        Ok(reg)
    }

    // -------------------------------------------------------------- prologue

    #[cfg(feature = "witness")]
    pub fn prologue(&mut self, g: &mut Gen, n: &Node) -> VResult<()> {
        let o = op(n);
        match o.op.as_str() {
            "recv" => {
                let PlanX::T(x) = &self.plan[&o.uid] else {
                    unreachable!()
                };
                g.raw(format!("vwait unpack rx n={}", x.size));
                let mut v =
                    Val::new("l1", count_of(ty(o), self.t.vlmax)).with_mode(mode_of(o.ty.as_ref()));
                v.buf = x.at;
                self.vals.insert(o.target.clone().unwrap(), v);
            }
            "load" => {
                let d = &self.descs[self.of[&o.uid]];
                g.raw(format!("vfill a{} -> l1[{}]", d.ad, d.buf[0]));
                g.raw("vbar");
                let v = l1val(Some(d), ty(o), d.buf[0], self.t);
                self.vals.insert(o.target.clone().unwrap(), v);
            }
            "unpack" => {
                let src = self.get(o, o.st.positional()[0], Some("l1"), None)?;
                if ty(o).shape.len() == 1 {
                    return Err(self.fail(
                        &o.st,
                        "L2.STAT.006",
                        "a row statistic unpacks in the stream loop",
                    ));
                }
                let mut v = Val::new("row", count_of(ty(o), self.t.vlmax)).with_mode(&src.mode);
                v.cols = *ty(o).shape.last().unwrap();
                if self.pin {
                    for r in 0..v.rows {
                        let s = g.sym();
                        v.regs.insert(r, s.clone());
                        self.pinned.insert(s.clone());
                        g.emit(unpack_line(&src, r), &[&s]);
                    }
                }
                self.vals.insert(o.target.clone().unwrap(), v);
            }
            other => {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.018",
                    format!("before the stream loop: load and unpack, not {other}"),
                ))
            }
        }
        Ok(())
    }

    pub(super) fn get(
        &self,
        o: &Op,
        t: &Term,
        kind: Option<&str>,
        local: Option<&IndexMap<String, Val>>,
    ) -> VResult<Val> {
        let Term::Name(n) = t else {
            return Err(self.fail(
                &o.st,
                "L2.REF.003",
                format!("wanted a value name, got {}", repr(t)),
            ));
        };
        let v = local.and_then(|l| l.get(n)).or_else(|| self.vals.get(n));
        let Some(v) = v else {
            return Err(self.fail(&o.st, "L2.REF.003", format!("no value {n}")));
        };
        if let Some(k) = kind {
            if v.kind != k {
                return Err(self.fail(
                    &o.st,
                    "L2.REF.003",
                    format!("{n} is a {} value, not {k}", v.kind),
                ));
            }
        }
        Ok(v.clone())
    }

    // ------------------------------------------------------------- iteration

    #[cfg(feature = "witness")]
    pub fn iteration(
        &mut self,
        g: &mut Gen,
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
                    super::super::super::cluster::py_list(&rows)
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
        g.raw("vbar");
        if prefetch {
            g.raw("vsync fill on=unpack");
            if !direct.is_empty() || !shared.is_empty() {
                g.raw("vsync fill on=drain");
            }
            for &d in &self.loads {
                let d = &self.descs[d];
                g.raw(format!("vfill a{} -> l1[{}] rel", d.ad, d.buf[1 - parity]));
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
                g.raw(format!("vwait unpack rx n={}", x.size));
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
            self.unpacks(g, &ops, &flat(&bands[0]), &mut local, None)?;
        }
        for (k, grps) in bands.iter().enumerate() {
            let rws = flat(grps);
            if !self.ahead {
                self.unpacks(g, &ops, &rws, &mut local, None)?;
            } else if k + 1 < bands.len() {
                self.unpacks(g, &ops, &flat(&bands[k + 1]), &mut local, None)?;
            }
            let mut again: HashSet<String> = HashSet::new();
            for (i, n) in body.iter().enumerate() {
                if let Node::If(blk) = n.as_ref() {
                    if !self.pin {
                        for b in ops_of(&blk.body) {
                            self.unpin(g, op(&b), &rws, &mut local, &mut again, Some(&fused))?;
                        }
                    }
                    g.raw("vsync fe on=math");
                    g.raw(format!("getstk s{}", self.t.stk_s()));
                    g.raw(format!("skipz s{}", self.t.stk_s()));
                    g.raw("{");
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
                        self.compute(g, x, grps, &rws, &mut local, &fused)?;
                    }
                    self.inplace = false;
                    g.raw("}");
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
                    self.reunpack(g, &ops, o, &rws, &mut local, &mut again)?;
                }
                if !self.pin {
                    self.unpin(g, o, &rws, &mut local, &mut again, Some(&fused))?;
                }
                self.compute(g, o, grps, &rws, &mut local, &fused)?;
            }
            if !self.pin {
                for o in &ops {
                    if op(o).op == "pack" {
                        self.unpin(g, op(o), &rws, &mut local, &mut again, None)?;
                    }
                }
            }
            if k == 0 {
                g.raw(format!(
                    "vsync pack on=drain slack={}",
                    if self.classed { 0 } else { stores.len() }
                ));
                if !shared.is_empty() {
                    g.raw("vsync pack on=unpack");
                }
            }
            self.packs(g, &ops, &rws, &mut local, parity)?;
        }
        for o in &streams {
            let PlanX::S(x) = &self.plan[&o.uid] else {
                unreachable!()
            };
            g.raw("vsync drain on=unpack");
            g.raw(format!("vpost {} n={}", x.src, x.size));
        }
        g.raw("vsync drain on=pack");
        if !direct.is_empty() {
            let slack = if prefetch { self.loads.len() } else { 0 };
            g.raw(format!("vsync drain on=fill slack={slack}"));
        }
        for o in &stores {
            let mut d = &self.descs[self.of[&o.uid]];
            if let (Some(alt), 1) = (d.alt, parity) {
                d = &self.descs[alt];
            }
            let at_ix = if o.op == "send" { 0 } else { 2 };
            let name = o.st.positional()[at_ix].clone();
            let src = self.get(o, &name, Some("l1"), Some(&local))?;
            let at = src.buf;
            let peer = if d.to.is_empty() {
                String::new()
            } else {
                format!(" to={} buf={}", d.to, d.side)
            };
            if d.credit {
                g.raw(format!("vwait drain cr n={}", d.words));
            }
            g.raw(format!("vdrain a{} <- l1[{at}] rel{peer}", d.ad));
        }
        Ok(())
    }

    #[cfg(feature = "witness")]
    fn compute(
        &mut self,
        g: &mut Gen,
        o: &Op,
        grps: &[Vec<i64>],
        rws: &[i64],
        local: &mut IndexMap<String, Val>,
        fused: &IndexMap<String, (Term, Term)>,
    ) -> VResult<()> {
        if is_in(&REDUCE, &o.op) {
            self.reduces(g, o, grps, local, fused)
        } else {
            self.math(g, o, rws, local)
        }
    }

    /// One `if sticky` block of math whose new names stay inside it.
    fn check_if(&self, body: &[Arc<Node>]) -> VResult<()> {
        if crate::mutant::on("l2.vector/sticky-unchecked") {
            return Ok(());
        }
        let blocks: Vec<usize> = body
            .iter()
            .enumerate()
            .filter(|(_, o)| matches!(o.as_ref(), Node::If(_)))
            .map(|(i, _)| i)
            .collect();
        if blocks.len() > 1 {
            return Err(self.fail(
                body[blocks[1]].st(),
                "L2.OP.027",
                "one if sticky block a stream loop",
            ));
        }
        let at = blocks[0];
        let mut before: HashSet<String> = body[..at]
            .iter()
            .filter_map(|o| o.target().map(str::to_string))
            .collect();
        before.extend(self.vals.keys().cloned());
        let mut inner: HashSet<String> = HashSet::new();
        for o in ops_of(body[at].body()) {
            let x = op(&o);
            if ["load", "store", "send", "unpack", "pack"].contains(&x.op.as_str()) {
                return Err(self.fail(&x.st, "L2.OP.027", "if sticky holds math and reductions"));
            }
            if let Some(t) = &x.target {
                if !before.contains(t) {
                    inner.insert(t.clone());
                }
            }
        }
        for o in ops_of(&body[at + 1..]) {
            let x = op(&o);
            for a in x.st.positional() {
                if let Some(n) = value_name(a) {
                    if inner.contains(n) {
                        return Err(self.fail(
                            &x.st,
                            "L2.OP.027",
                            format!("{n} is made only inside if sticky"),
                        ));
                    }
                }
            }
        }
        Ok(())
    }
}
