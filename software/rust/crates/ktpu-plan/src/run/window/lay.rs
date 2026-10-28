//! A window contraction run (`run._window`): each side laid out dense
//! (`_lay`, `_padded`, `_pad_copy`, `_lane_copies`), the product on the
//! clusters, the rest of the expression reading it.

use super::*;

impl Runner<'_> {
    /// `run._window`: each side laid out dense as [batch.., outer.., K..,
    /// lane], the product on the clusters into a temporary, the rest of the
    /// expression reading it. False when the plan is not one.
    pub(in crate::run) fn window(&mut self, plan: &Plan, bufs: &Bufs) -> Result<bool, Refusal> {
        if crate::mutant::on("tg.run/no_window") {
            return Ok(true);
        }
        let Some(c) = self.find(plan) else {
            return Ok(false);
        };
        let mut tmps = Vec::new();
        let got = self.window_run(&c, bufs, &mut tmps);
        for t in tmps {
            self.x.free(&t);
        }
        got.map(|_| true)
    }

    fn window_run(
        &mut self,
        c: &Contraction,
        bufs: &Bufs,
        tmps: &mut Vec<String>,
    ) -> Result<(), Refusal> {
        let plan = &c.plan;
        let (em, en, ek) = (self.size(&c.ma), self.size(&c.nb), self.size(&c.ks) * c.pad);
        let fresh = self.fresh_ranges(plan, &[em, en, ek, c.pad]);
        let (m, n, k, lane) = (fresh[0], fresh[1], fresh[2], fresh[3]);
        let mut bufs = bufs.clone();
        let mut keys = Vec::new();
        for (side, outer) in [(&c.a, &c.ma), (&c.b, &c.nb)] {
            let key = self.key(&format!("win{}", keys.len()));
            let order: Vec<Id> = c.bt.iter().chain(outer.iter()).copied().collect();
            let is_a = std::ptr::eq(side, &c.a);
            self.lay(c, &mut bufs, tmps, side, is_a, &order, lane, &key)?;
            keys.push(key);
        }
        let side_read = |me: &Self, key: &Slot, r: Id, rows: i64| {
            let mut coef: Vec<(Id, i64)> =
                c.bt.iter()
                    .copied()
                    .zip(me.mult(&c.bt).into_iter().map(|s| s * rows * ek))
                    .collect();
            coef.push((r, ek));
            coef.push((k, 1));
            Access {
                slot: key.clone(),
                dtype: "half".into(),
                coef,
                off: 0,
                table: None,
                gate: false,
            }
        };
        let a = side_read(self, &keys[0], m, em);
        let b = side_read(self, &keys[1], n, en);
        let pkey = self.key("winp");
        let pbytes = self.hw.f16_bytes(self.size(&c.bt) * em * en);
        let tp = self.x.storage(pbytes);
        tmps.push(tp.clone());
        bufs.insert(
            pkey.clone(),
            View {
                base: tp,
                offset: 0,
                nbytes: pbytes,
            },
        );
        let out: Vec<Id> = c.bt.iter().copied().chain([m, n]).collect();
        let mm = Plan {
            kind: "matmul".into(),
            name: format!("{}_mm", plan.name),
            out_slot: pkey.clone(),
            out_dtype: "half".into(),
            out_shape: out.iter().map(|r| self.extent(*r)).collect(),
            out_ranges: out,
            out_strides: None,
            expr: Some(expr::load(0)),
            accesses: vec![],
            reduce_ranges: vec![k],
            reductions: vec![],
            a: Some(a),
            b: Some(b),
            mnk: Some((m, n, vec![k])),
            batch: c.bt.clone(),
            ..plan.clone()
        };
        self.product(&mm, &bufs)?;
        let all: Vec<Id> = c.bt.iter().chain(&c.ma).chain(&c.nb).copied().collect();
        let read = Access {
            slot: pkey,
            dtype: "half".into(),
            coef: self.dense(&all),
            off: 0,
            table: None,
            gate: false,
        };
        let mut prod = expr::load(plan.accesses.len());
        if c.scale != 1.0 {
            prod = expr::op("mul", vec![expr::constant(c.scale), prod]);
        }
        fn swap(x: &E, red: &E, prod: &E) -> E {
            if Rc::ptr_eq(x, red) {
                return prod.clone();
            }
            match x.as_ref() {
                Expr::Op(n, a) => expr::op(n, a.iter().map(|y| swap(y, red, prod)).collect()),
                _ => x.clone(),
            }
        }
        let mut accs = plan.accesses.clone();
        accs.push(read);
        let (e, accs, _) = prune(&swap(plan.expr.as_ref().unwrap(), &c.red, &prod), &accs);
        let epi = Plan {
            expr: Some(e),
            accesses: accs,
            reduce_ranges: vec![],
            reductions: vec![],
            ..plan.clone()
        };
        let (epi, bufs, _) = self.reform(&epi, &bufs);
        self.vector(&epi, &bufs)
    }

    /// `run._lay`: `side`'s read dense at `bufs[key]` as f16 [outer..,
    /// K.., lane], walked by the mover from its padded source.
    #[allow(clippy::too_many_arguments)]
    fn lay(
        &mut self,
        c: &Contraction,
        bufs: &mut Bufs,
        tmps: &mut Vec<String>,
        side: &Side,
        is_a: bool,
        outer: &[Id],
        lane: Id,
        key: &Slot,
    ) -> Result<(), Refusal> {
        let (src, strides, off) = self.padded_side(c, bufs, tmps, side, is_a)?;
        let order: Vec<Id> = outer.iter().chain(&c.ks).copied().chain([lane]).collect();
        let mut coef: Vec<(Id, i64)> = outer
            .iter()
            .chain(&c.ks)
            .map(|r| (*r, side.mult(*r) * strides[&side.dim(*r)]))
            .collect();
        coef.push((lane, 1));
        if same_map(&coef, &self.dense(&order)) && off == 0 {
            let v = bufs[&src].clone();
            bufs.insert(key.clone(), v);
            return Ok(());
        }
        let nbytes = self.hw.f16_bytes(self.size(&order));
        let t = self.x.storage(nbytes);
        tmps.push(t.clone());
        self.x.wrote(&t, &flat(), false);
        bufs.insert(
            key.clone(),
            View {
                base: t,
                offset: 0,
                nbytes,
            },
        );
        let read = Access {
            slot: src,
            dtype: "half".into(),
            coef,
            off,
            table: None,
            gate: false,
        };
        let sub = Plan {
            kind: "vector".into(),
            name: format!("{}_w", c.plan.name),
            ..self.laid_plan(&c.plan, key.clone(), &read, &order, None)
        };
        if !self.move_(&sub, bufs)? {
            return Err(Refusal::new(
                "TG.RUN.004",
                "a window operand not walked in whole words",
            ));
        }
        Ok(())
    }

    /// `run._padded`: `(slot, {coordinate: element stride}, element of
    /// coordinate 0)` of `side`'s source with its lane innermost in whole
    /// words and zero outside its gate's box.
    #[allow(clippy::type_complexity)]
    fn padded_side(
        &mut self,
        c: &Contraction,
        bufs: &mut Bufs,
        tmps: &mut Vec<String>,
        side: &Side,
        copies: bool,
    ) -> Result<(Slot, HashMap<usize, i64>, i64), Refusal> {
        let acc = &side.acc;
        let axes: Vec<usize> = (0..side.dims.len())
            .filter(|d| Some(*d) != side.lane)
            .collect();
        let full = side.dims.iter().all(|d| d.bx == (0, self.span(d)));
        let (st, voff) = self.base(bufs, &acc.slot);
        if let Some(l) = side.lane {
            let ld = &side.dims[l];
            if full
                && ld.stride == 1
                && self.extent(ld.terms[0].0) == c.pad
                && axes
                    .iter()
                    .all(|d| side.dims[*d].stride % self.hw.halves == 0)
                && (voff / 2 + acc.off) % self.hw.halves == 0
                && acc.dtype == "half"
                && self.x.store(&st).has(&flat())
            {
                let strides = (0..side.dims.len())
                    .map(|d| (d, side.dims[d].stride))
                    .collect();
                return Ok((acc.slot.clone(), strides, acc.off));
            }
        }
        let key = self.key("pad");
        self.pad_copy(
            bufs, tmps, acc, &side.dims, &axes, &key, side.lane, c.pad, copies,
        )
    }

    /// `run._pad_copy`: `acc`'s source over its coordinates (`axes`
    /// outermost first, then the lane, `pad` wide) gathered dense as f16
    /// into `bufs[key]`, every element outside the boxes zero.
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    pub(super) fn pad_copy(
        &mut self,
        bufs: &mut Bufs,
        tmps: &mut Vec<String>,
        acc: &Access,
        dims: &[Dim],
        axes: &[usize],
        key: &Slot,
        lane: Option<usize>,
        pad: i64,
        copies: bool,
    ) -> Result<(Slot, HashMap<usize, i64>, i64), Refusal> {
        let ext: Vec<i64> = axes
            .iter()
            .map(|d| self.span(&dims[*d]))
            .chain([pad])
            .collect();
        let dense: Vec<i64> = (0..ext.len())
            .map(|i| ext[i + 1..].iter().product())
            .collect();
        let mut strides: HashMap<usize, i64> = axes.iter().copied().zip(dense).collect();
        if let Some(l) = lane {
            strides.insert(l, 1);
        }
        let (mut walk, mut dwalk) = (Vec::new(), Vec::new());
        let (mut first, mut dat) = (acc.off, 0);
        for d in axes.iter().copied().chain(lane) {
            let dim = &dims[d];
            let (a, b) = dim.bx;
            walk.push((b - a, dim.stride));
            dwalk.push((b - a, strides[&d]));
            first += dim.stride * (self.lo(dim) + a);
            dat += strides[&d] * a;
        }
        let n: i64 = ext.iter().product();
        let nbytes = 2 * self.hw.whole_halves(n);
        let s = self.x.storage(nbytes);
        tmps.push(s.clone());
        let at = self.x.addr(&s, 0);
        if lane.is_none() && copies {
            self.lane_copies(acc, bufs, &walk, first, &dwalk, dat, nbytes / 2, at)?;
        } else if !self.gather_to(acc, bufs, &walk, first, &dwalk, dat, n, at, false)? {
            return Err(Refusal::new(
                "TG.RUN.004",
                "a padded operand with no f16 form",
            ));
        }
        self.x.wrote(&s, &flat(), false);
        self.x.store_mut(&s).dtype = Some("float16".into());
        bufs.insert(
            key.clone(),
            View {
                base: s,
                offset: 0,
                nbytes,
            },
        );
        let off = -axes
            .iter()
            .map(|d| strides[d] * self.lo(&dims[*d]))
            .sum::<i64>();
        Ok((key.clone(), strides, off))
    }

    /// `run._lane_copies`: each value `walk` reaches in every f16 lane of
    /// its destination word, every other word zero.
    #[allow(clippy::too_many_arguments)]
    fn lane_copies(
        &mut self,
        acc: &Access,
        bufs: &Bufs,
        walk: &[(i64, i64)],
        first: i64,
        dwalk: &[(i64, i64)],
        dat: i64,
        m: i64,
        dst: crate::program::Addr,
    ) -> Result<(), Refusal> {
        self.x.paths.insert("run._lane_copies");
        let (st, off) = self.base(bufs, &acc.slot);
        let it = crate::run::item(&acc.dtype);
        let nbytes = self.x.store(&st).nbytes;
        let total = nbytes / it;
        let whole = View {
            base: st,
            offset: 0,
            nbytes,
        };
        let src = self.f16_of(&whole, &acc.dtype, total)?;
        let h = self.hw.halves;
        let w: Vec<(i64, i64)> = walk.iter().copied().chain([(h, 0)]).collect();
        let dw: Vec<(i64, i64)> = dwalk.iter().copied().chain([(h, 1)]).collect();
        self.relayout(
            src,
            total,
            &w,
            first + off.div_euclid(it),
            &dw,
            m,
            dst,
            dat,
            false,
        )
    }
}
