//! Each memory view of the instance as a descriptor and its L1 buffers
//! (L2.VC.003-.004, .009, .011, .020-.024).

use super::*;

impl<'a> Ctx<'a> {
    pub fn prepare(&mut self, n: &Node, looped: bool) -> VResult<()> {
        let got = self.prepare_one(n, looped);
        if on("l2.vector/no-twin") {
            for d in self.descs.iter_mut() {
                d.value.clear();
            }
        }
        got
    }

    fn prepare_one(&mut self, n: &Node, looped: bool) -> VResult<()> {
        let Node::Op(o) = n else { return Ok(()) };
        if o.op == "send" {
            return self.send(o);
        }
        if o.op == "unpack" && looped {
            if let Some(src @ Term::View(..)) = o.st.positional().first().copied() {
                let src = src.clone();
                self.band_view(o, &src)?;
            }
            return Ok(());
        }
        if o.op != "load" && o.op != "store" {
            return Ok(());
        }
        let pos = o.st.positional();
        let (view, mut want, rows);
        if o.op == "load" {
            if pos.len() != 1 || !matches!(pos[0], Term::View(..) | Term::Name(_)) {
                return Err(self.fail(&o.st, "L2.VC.003", "NAME = load VIEW : f16[R, C] @l1"));
            }
            let t = match &o.ty {
                Some(t) if t.space == "l1" && (t.dtype == "f16" || t.dtype == "f32") => t,
                _ => {
                    return Err(self.fail(
                        &o.st,
                        "L2.VC.003",
                        "a load is f16[R, C] or f32[R, C] @l1",
                    ))
                }
            };
            view = as_view(pos[0]);
            let item = if t.dtype == "f32" { 4 } else { 2 };
            let n = t.shape[0]
                * if t.shape.len() == 2 {
                    t.shape[t.shape.len() - 1]
                } else {
                    1
                };
            want = Some(n * item / 32);
            rows = t.mode.as_deref() == Some("gt4");
        } else {
            if pos.len() != 3 || !matches!(pos[0], Term::View(..) | Term::Name(_)) {
                return Err(self.fail(&o.st, "L2.VC.003", "store VIEW <- VALUE"));
            }
            view = as_view(pos[0]);
            want = None;
            let packed = match pos[2] {
                Term::Name(nm) => self.packed(nm),
                _ => None,
            };
            let pt = packed.as_ref().and_then(|p| p.ty().cloned());
            rows = pt
                .as_ref()
                .map(|t| t.mode.as_deref() == Some("gt4"))
                .unwrap_or(false);
            if let Some(pt) = &pt {
                if pt.dtype == "f16" || pt.dtype == "f32" {
                    let n = pt.shape[0]
                        * if pt.shape.len() == 2 {
                            pt.shape[pt.shape.len() - 1]
                        } else {
                            1
                        };
                    want = Some(n * if pt.dtype == "f32" { 4 } else { 2 } / 32);
                }
            }
        }
        let first = match (looped, self.trips.first()) {
            (false, _) => None,
            (true, Some(t)) => Some(*t),
            (true, None) if self.classed => Some(crate::ir::const_of(
                self.body,
                &o.st,
                &self.lp.as_ref().unwrap().lo,
                &self.env,
            )?),
            (true, None) => return Err(VErr::Value("list index out of range".into())),
        };
        let (name, off, walk, mut words, kind, rows_w, vrows) = self.walk(o, &view, first, rows)?;
        let t = if o.op == "load" { o.ty.clone() } else { None };
        let bcast = t
            .as_ref()
            .map(|t| kind == Kind::Rows && vrows == 1 && t.shape[0] > 1)
            .unwrap_or(false);
        if bcast {
            want = Some(rows_w);
        }
        let mapped = self.mapped_tile(o, first)?;
        if let Some(w) = want.filter(|_| mapped.is_none()) {
            if words != w && !crate::mutant::on("l2.vector/view-size-unchecked") {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.020",
                    format!("the view holds {words} words, the value {w}"),
                ));
            }
        }
        if o.op == "load" && kind == Kind::Mx7 {
            return Err(self.fail(
                &o.st,
                "L2.VC.023",
                "an MXFP7 buffer is a cluster's operand: a core stores into it",
            ));
        }
        if let Some(t) = &t {
            if (kind != Kind::Plain) != (t.mode.as_deref() == Some("gt4")) {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.023",
                    "a tile= buffer loads @l1(gt4); a row-major one @l1",
                ));
            }
            if kind == Kind::Plain && t.shape.len() == 2 && t.shape[1] != self.t.vlmax {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.003",
                    format!(
                        "a row-major load is f16[R, {}], or a GT4 band (@l1(gt4)) of whole 32-column blocks",
                        self.t.vlmax
                    ),
                ));
            }
            if t.shape.len() == 1 && t.dtype != "f16" {
                return Err(self.fail(&o.st, "L2.STAT.001", "a row statistic loads from f16"));
            }
        }
        if off % 32 != 0 {
            return Err(self.fail(
                &o.st,
                "L2.VC.021",
                format!("a view {off} bytes in: a descriptor starts on a 32-byte word"),
            ));
        }
        let mut phases = Vec::new();
        let mut advances = Vec::new();
        let mut mapped_span = None;
        if let Some(tile) = &mapped {
            let tiles = if looped {
                self.trips
                    .iter()
                    .map(|&trip| self.mapped_tile(o, Some(trip)).map(|t| t.unwrap()))
                    .collect::<VResult<Vec<_>>>()?
            } else {
                vec![crate::lower::readmap::Tile {
                    anchor: tile.anchor,
                    shape: tile.shape.clone(),
                }]
            };
            words = tiles
                .iter()
                .map(|t| t.shape.words.len().max(1) as i64)
                .max()
                .unwrap();
            mapped_span = Some((
                tiles.iter().map(|t| t.anchor).min().unwrap(),
                tiles
                    .iter()
                    .map(|t| t.anchor + t.shape.extent())
                    .max()
                    .unwrap(),
            ));
            for (i, tile) in tiles.iter().enumerate() {
                let delta = tiles.get(i + 1).map_or(0, |next| next.anchor - tile.anchor);
                advances.push(delta);
                phases.push(tile.shape.clone());
            }
        }
        let mut inc = 0;
        if mapped.is_none() && looped && (self.trips.len() > 1 || self.classed) {
            let first = first.unwrap();
            inc = if self.classed {
                self.class_origin(o, &view, first + 1)? - off
            } else {
                self.walk(o, &view, Some(first + 1), rows)?.1 - off
            };
            let last = self.trips.last().copied().unwrap_or(first);
            let end = self.walk(o, &view, Some(last), rows)?.1;
            if end != off + inc * (last - first) {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.021",
                    "the view is not affine in the loop variable",
                ));
            }
            let step = self.t.step();
            if !(-step <= inc && inc < step) {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.004",
                    format!(
                        "a {inc}-byte step: ainc takes {} signed bits",
                        self.t.ainc_bits
                    ),
                ));
            }
            if inc % 32 != 0 {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.004",
                    format!("a {inc}-byte step: a descriptor steps whole words"),
                ));
            }
        }
        if self.descs.len() == self.t.descriptors {
            return Err(self.fail(
                &o.st,
                "L2.VC.021",
                format!("more than {} memory views", self.t.descriptors),
            ));
        }
        let (mut shared, mut twin) = (None, None);
        let val = match (o.op.as_str(), pos.get(2)) {
            ("store", Some(Term::Name(n))) => n.clone(),
            _ => String::new(),
        };
        let lay = if kind == Kind::Rows { rows_w } else { 0 };
        if looped && !val.is_empty() {
            twin = self
                .descs
                .iter()
                .position(|x| x.value == val && x.words == words && x.rows_w == lay);
        }
        if twin.is_none()
            && looped
            && o.op == "store"
            && self.top + (if self.classed { 1 } else { 2 }) * words > self.land
        {
            shared = self.loads.iter().copied().find(|&x| {
                self.descs[x].words == words
                    && self.descs[x].rows_w == lay
                    && !self.descs.iter().any(|s| s.shared == Some(x))
            });
        }
        if mapped.is_some()
            && looped
            && twin.is_none()
            && shared.is_none()
            && self.top
                + words
                    * if self.classed || self.serial || self.trips.len() == 1 {
                        1
                    } else {
                        2
                    }
                > self.land
        {
            return Err(VErr::Pressure);
        }
        let buf = if let Some(tw) = twin {
            self.descs[tw].buf.clone()
        } else if let Some(sh) = shared {
            self.descs[sh].buf.clone()
        } else if looped {
            let a = self.alloc(words, o)?;
            let b = if self.classed || self.serial || (mapped.is_some() && self.trips.len() == 1) {
                a
            } else {
                self.alloc(words, o)?
            };
            vec![a, b]
        } else {
            self.down -= words;
            let kept = self
                .rel
                .as_ref()
                .map(|r| Some(&r.1) == o.target.as_ref())
                .unwrap_or(false)
                || !self.pin;
            let floor = if kept { self.top } else { 0 };
            if self.down < floor && !self.pin {
                return Err(VErr::Pressure);
            }
            if self.down < floor {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.022",
                    format!("L1 holds {} words for this core's buffers", self.land),
                ));
            }
            vec![self.down]
        };
        let mut d = Desc {
            ad: self.descs.len(),
            mapped: mapped.map(|t| t.shape),
            phases,
            advances,
            mapped_span,
            buffer: Some(name),
            off,
            words,
            inc,
            buf,
            walk,
            kind: o.op.clone(),
            ..Default::default()
        };
        if kind == Kind::Rows {
            d.rows_w = rows_w;
            d.bcast = bcast;
        }
        d.shared = shared;
        d.value = val;
        d.twin = twin;
        let ix = self.descs.len();
        self.descs.push(d);
        self.of.insert(o.uid, ix);
        if o.op == "load" && looped {
            self.loads.push(ix);
        }
        Ok(())
    }

    /// The row-major next origin may lie past the allocation when no next trip runs.
    fn class_origin(&self, o: &Op, view: &Term, at: i64) -> VResult<i64> {
        let lp = self.lp.as_ref().unwrap();
        let view = value(self.body, &o.st, view, &bind(&self.env, &lp.var, at))?;
        let Term::View(name, slices) = view else {
            return Err(self.fail(&o.st, "L2.VC.021", "class stream requires a row-major view"));
        };
        let buf = &self.body.buffers[&name];
        if let Some((_, gn)) = buf.tile() {
            if buf.shape.len() != 2 || buf.shape[1] != gn * self.t.mg_lanes {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.021",
                    "class tiled stream needs stacked tile rows",
                ));
            }
            let mut origin = [0; 2];
            for (i, slice) in slices.iter().enumerate() {
                origin[i] = match slice {
                    Term::Slice(None, _) => 0,
                    Term::Slice(Some(lo), _) => {
                        crate::ir::const_of(self.body, &o.st, lo, &self.env)?
                    }
                    Term::Int(lo, _) => *lo as i64,
                    _ => {
                        return Err(self.fail(
                            &o.st,
                            "L2.VC.021",
                            "class tiled stream needs ordinary axes",
                        ))
                    }
                };
            }
            if origin.iter().any(|v| v % self.t.mg_lanes != 0) {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.021",
                    "class tiled origin must align to subtiles",
                ));
            }
            return Ok(crate::lower::memory::subtile_address(
                buf,
                origin[0] / self.t.mg_lanes,
                origin[1] / self.t.mg_lanes,
                self.t,
            ));
        }
        if buf.is_mx7() {
            return Err(self.fail(
                &o.st,
                "L2.VC.021",
                "class stream stages packed layouts first",
            ));
        }
        let mut stride = if buf.dtype == "f32" { 4 } else { 2 };
        let mut strides = vec![0; buf.shape.len()];
        for (i, dim) in buf.shape.iter().enumerate().rev() {
            strides[i] = stride;
            stride *= dim;
        }
        let mut offset = 0;
        for (slice, stride) in slices.iter().zip(strides) {
            let lo = match slice {
                Term::Slice(None, _) => 0,
                Term::Slice(Some(lo), _) => crate::ir::const_of(self.body, &o.st, lo, &self.env)?,
                Term::Int(lo, _) => *lo as i64,
                _ => {
                    return Err(self.fail(
                        &o.st,
                        "L2.VC.021",
                        "class stream requires ordinary axes",
                    ))
                }
            };
            offset += lo * stride;
        }
        Ok(offset)
    }

    /// A peer drain of each trip's packed rows into the cluster's bank.
    fn send(&mut self, o: &Op) -> VResult<()> {
        let PlanX::T(x) = self.plan[&o.uid].clone() else {
            unreachable!()
        };
        let name = o.st.positional()[0].as_name().unwrap().to_string();
        let lp_ops = ops_of(&self.lp.as_ref().unwrap().body);
        let packed = lp_ops
            .iter()
            .find(|p| p.target() == Some(name.as_str()))
            .unwrap();
        let words = count_of(packed.ty().unwrap(), self.t.vlmax) * 4;
        if self.descs.len() == self.t.descriptors {
            return Err(self.fail(
                &o.st,
                "L2.VC.021",
                format!("more than {} memory views", self.t.descriptors),
            ));
        }
        let a = self.alloc(words, o)?;
        let b = self.alloc(words, o)?;
        let side = match o.st.kw("side").and_then(|s| s.as_name()) {
            Some("B") => 1,
            _ => 0,
        };
        let mut d = Desc {
            ad: self.descs.len(),
            buffer: None,
            off: 4 * x.at,
            words,
            inc: if self.trips.len() > 1 { words } else { 0 },
            buf: vec![a, b],
            walk: Walk::flat(32, words),
            to: x.dst.clone(),
            side,
            kind: "send".into(),
            kind_op: o.uid,
            count: true,
            ..Default::default()
        };
        d.credit = x.flow;
        if !x.slots.is_empty() {
            d.off = 4 * x.slots[0];
            d.inc = 0;
            if self.descs.len() + 1 == self.t.descriptors {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.021",
                    format!("more than {} memory views", self.t.descriptors),
                ));
            }
            let mut alt = d.clone();
            alt.ad = d.ad + 1;
            alt.off = 4 * x.slots[1];
            let ix = self.descs.len();
            d.alt = Some(ix + 1);
            self.descs.push(d);
            self.descs.push(alt);
            self.of.insert(o.uid, ix);
            return Ok(());
        }
        let ix = self.descs.len();
        self.descs.push(d);
        self.of.insert(o.uid, ix);
        Ok(())
    }

    /// `unpack H[ROWS, :]` of a value made before the loop: each trip's rows
    /// follow the last trip's, read through the unpack pointer.
    fn band_view(&mut self, o: &Op, view: &Term) -> VResult<()> {
        let Term::View(vname, _) = view else {
            unreachable!()
        };
        let src = self
            .pre
            .iter()
            .find(|p| p.target() == Some(vname.as_str()))
            .cloned();
        let src_ty = match src.as_deref() {
            Some(Node::Op(s)) if (s.op == "recv" || s.op == "load") && s.ty.is_some() => {
                s.ty.clone().unwrap()
            }
            _ => {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.009",
                    format!("{vname}: a value made before the stream loop"),
                ))
            }
        };
        if self.rel.is_some() {
            return Err(self.fail(
                &o.st,
                "L2.VC.009",
                "one banded unpack a loop (one unpack pointer)",
            ));
        }
        let shape = &src_ty.shape;
        let lp = self.lp.clone().unwrap();
        let mut firsts = Vec::new();
        for tt in [self.trips[0], *self.trips.last().unwrap()] {
            let v = value(self.body, &o.st, view, &bind(&self.env, &lp.var, tt))?;
            let ax = M::axes_of(&v, shape).map_err(|e| self.fail(&o.st, "L2.VC.009", e))?;
            let (lo, c0, c) = (ax[0].lo, ax[1].lo, ax[1].size);
            if c0 != 0 || c != shape[1] || M::shape_of(&ax) != ty(o).shape {
                return Err(self.fail(
                    &o.st,
                    "L2.VC.009",
                    "a banded unpack takes whole rows of the value",
                ));
            }
            firsts.push(lo * shape[1] / self.t.vlmax);
        }
        let per = count_of(ty(o), self.t.vlmax);
        if firsts[1] - firsts[0] != per * (self.trips.len() as i64 - 1) {
            return Err(self.fail(
                &o.st,
                "L2.VC.009",
                "each trip's rows follow the last trip's",
            ));
        }
        if mode_of(o.ty.as_ref()) != mode_of(Some(&src_ty)) {
            let m = mode_of(Some(&src_ty));
            return Err(self.fail(
                &o.st,
                "L2.OP.025",
                format!("an @l1({m}) value unpacks to @reg({m})"),
            ));
        }
        self.rel = Some((o.uid, vname.clone(), firsts[0], per));
        Ok(())
    }
}
