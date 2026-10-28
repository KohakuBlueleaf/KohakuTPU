//! Where a rows kernel writes its result and how it is finished (`_place`,
//! `_place_stat`, `_half_target`, `_moved`).

use super::*;

impl Runner<'_> {
    /// `_place`: the address and operand a rows kernel writes the result
    /// to, and how it is finished.
    pub(super) fn place(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        (kind, rows, cols, wp): (&str, &[Id], &[Id], i64),
        tmps: &mut Vec<String>,
    ) -> Result<(Addr, Operand, Done), Refusal> {
        if kind == "along" {
            return self.place_stat(plan, bufs, rows, tmps);
        }
        let w = self.size(cols);
        let out_rows: Vec<Id> = if kind == "rows" {
            rows.to_vec()
        } else {
            vec![]
        };
        let r = self.size(&out_rows);
        let (dst, doff) = self.base(bufs, &plan.out_slot);
        let n: i64 = plan.out_shape.iter().product();
        let whole = doff == 0
            && plan.out_strides.is_none()
            && n * item(&plan.out_dtype) == self.x.store(&dst).nbytes;
        let full = Operand {
            pitch: wp,
            ..Operand::new("full")
        };
        if wp != w && whole && is_float(&plan.out_dtype) {
            let key = rows_key(w, wp);
            let at = self.x.form(&dst, &key, 2 * r * wp);
            let done = Done::Form(dst, key, np_name(&plan.out_dtype));
            return Ok((at, full, done));
        }
        let (tplan, tbufs, fin) = self.half_target(plan, bufs)?;
        let (tdst, tdoff) = self.base(&tbufs, &tplan.out_slot);
        let o = self.classify(&self.out_access(&tplan), &out_rows, cols);
        if wp == w && tdoff.rem_euclid(self.hw.word) == 0 && self.takes(o.as_ref(), wp, 0) {
            self.window_ok(&tdst, &tplan)?;
            let o = o.unwrap();
            let pitch = if o.kind == "full" { o.pitch } else { wp };
            let at = self.x.addr(&tdst, 0);
            let out = Operand {
                pitch,
                offset: tdoff / 2,
                ..Operand::new("full")
            };
            return Ok((at, out, Done::Direct(tdst, tplan, fin)));
        }
        let t = self.x.storage(self.hw.f16_bytes(r * wp));
        tmps.push(t.clone());
        self.x.store_mut(&t).dtype = Some("float16".into());
        let mut coef: Vec<(Id, i64)> = self
            .dense(&self.live(&out_rows))
            .into_iter()
            .map(|(x, s)| (x, s * wp))
            .collect();
        for (x, s) in self.dense(&self.live(cols)) {
            put(&mut coef, x, s);
        }
        let at = self.x.addr(&t, 0);
        Ok((at, full, Done::Moved(t, tplan, tbufs, coef, fin)))
    }

    /// `_place_stat`: R values into the output's f16 values where those
    /// words are its own, else into a temporary moved in.
    fn place_stat(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        rows: &[Id],
        tmps: &mut Vec<String>,
    ) -> Result<(Addr, Operand, Done), Refusal> {
        let r = self.size(rows);
        let (tplan, tbufs, fin) = self.half_target(plan, bufs)?;
        let (tdst, tdoff) = self.base(&tbufs, &tplan.out_slot);
        let (first, total) = (tdoff / 2, self.x.store(&tdst).nbytes / 2);
        let sr = self.stat_rows();
        let own = first % sr == 0 && (r % sr == 0 || first + r == total);
        if plan.out_strides.is_none() && own {
            self.window_ok(&tdst, &tplan)?;
            let at = self.x.addr(&tdst, tdoff);
            return Ok((at, Operand::new("row"), Done::Direct(tdst, tplan, fin)));
        }
        let t = self.x.storage(self.stat_bytes(r));
        tmps.push(t.clone());
        self.x.store_mut(&t).dtype = Some("float16".into());
        let coef = self.dense(&self.live(rows));
        let at = self.x.addr(&t, 0);
        let done = Done::Window(t, tplan, tbufs, coef, fin);
        Ok((at, Operand::new("row"), done))
    }

    pub(super) fn finish(&mut self, done: Done) -> Result<(), Refusal> {
        match done {
            Done::Form(dst, key, dtype) => {
                self.x.wrote(&dst, &key, false);
                self.x.store_mut(&dst).dtype = Some(dtype);
            }
            Done::Direct(dst, tplan, fin) => {
                self.wrote_flat(&dst, &tplan);
                self.finish_half(fin);
            }
            Done::Moved(t, tplan, tbufs, coef, fin) => {
                self.moved(&tplan, &tbufs, &t, &coef)?;
                self.finish_half(fin);
            }
            Done::Window(t, tplan, tbufs, coef, fin) => {
                self.x.wrote(&t, &flat(), false);
                self.moved(&tplan, &tbufs, &t, &coef)?;
                self.finish_half(fin);
            }
        }
        Ok(())
    }

    /// `_half_target`'s `finish`: the output's f16 form written.
    pub(in crate::run) fn finish_half(&mut self, fin: Half) {
        if let Some((st, dt)) = fin {
            self.x.wrote(&st, &f16(), false);
            self.x.store_mut(&st).dtype = Some(dt);
        }
    }

    /// `_half_target`: the plan writing f16 values, as it is for an f16
    /// output, else into its output's f16 form (the rest current first
    /// when it writes part of a written buffer).
    pub(in crate::run) fn half_target(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
    ) -> Result<(Plan, Bufs, Half), Refusal> {
        if plan.out_dtype == "half" {
            return Ok((plan.clone(), bufs.clone(), None));
        }
        let (st, off) = self.base(bufs, &plan.out_slot);
        let it = item(&plan.out_dtype);
        let s = self.x.store(&st).clone();
        let total = s.nbytes / it;
        let n: i64 = plan.out_shape.iter().product();
        let part = off != 0 || plan.out_strides.is_some() || n != total;
        if part && (s.dtype.is_some() || s.host.is_some()) {
            let whole = View {
                base: st.clone(),
                offset: 0,
                nbytes: s.nbytes,
            };
            self.f16_of(&whole, &plan.out_dtype, total)?;
        }
        let f = self.x.form(&st, &f16(), 2 * total);
        let fbytes = self
            .x
            .store(&st)
            .forms
            .iter()
            .find(|(k, _)| *k == f16())
            .map(|(_, n)| *n)
            .unwrap();
        let alias = self.x.alias(f, fbytes);
        let key = self.key("f16");
        let nbytes = bufs[&plan.out_slot].nbytes / it * 2;
        let mut b2 = bufs.clone();
        b2.insert(
            key.clone(),
            View {
                base: alias,
                offset: off / it * 2,
                nbytes,
            },
        );
        let out = Plan {
            out_slot: key,
            out_dtype: "half".into(),
            ..plan.clone()
        };
        Ok((out, b2, Some((st, np_name(&plan.out_dtype)))))
    }

    /// `_moved`: `t` (f16 values at strides `coef` over the plan's output
    /// ranges) moved into its f16 output by the bare-read kernels.
    pub(in crate::run) fn moved(
        &mut self,
        plan: &Plan,
        bufs: &Bufs,
        t: &str,
        coef: &[(Id, i64)],
    ) -> Result<(), Refusal> {
        let key = self.key("placed");
        let read = Access {
            slot: key.clone(),
            dtype: "half".into(),
            coef: coef.to_vec(),
            off: 0,
            table: None,
            gate: false,
        };
        let sub = Plan {
            kind: "vector".into(),
            expr: Some(expr::load(0)),
            accesses: vec![read],
            reduce_ranges: vec![],
            reductions: vec![],
            ..plan.clone()
        };
        let mut b2 = bufs.clone();
        let nbytes = self.x.store(t).nbytes;
        b2.insert(
            key,
            View {
                base: t.into(),
                offset: 0,
                nbytes,
            },
        );
        let got = self.bare(&sub, &b2)?;
        assert!(got, "an f16 output: the gather takes any read");
        Ok(())
    }
}
