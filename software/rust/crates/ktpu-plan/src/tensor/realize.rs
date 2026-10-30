//! A lazy tensor realised: its one kernel (rows of values, a row
//! statistic, or a product with its epilogue), its reads bound.

use super::*;

impl Session<'_> {
    // ------------------------------------------------------------ realise
    /// `_realize`: the node's one kernel.
    pub fn realize(&mut self, t: usize) -> Result<(), Refusal> {
        let Some(node) = self.ts[t].node.clone() else {
            return Ok(());
        };
        let mut ls = Vec::new();
        leaves(&node.expr, &mut ls);
        let prod = prod_of(&node.expr);
        if let Some(p) = &prod {
            ls.retain(|x| !Rc::ptr_eq(x, p));
            ls.insert(0, p.clone());
        }
        let e = lower(&node.expr, &ls, &mut HashMap::new());
        let info = json!({"api": count(&e), "shape": self.ts[t].shape});
        let st = match prod.as_deref() {
            Some(K::Prod(a, w)) => self.product(t, (*a, *w), &e, &ls[1..], &node.view, info)?,
            _ if self.ts[t].stat => self.statistic(t, &e, &ls, &node.view, info)?,
            _ => self.wide(t, &e, &ls, &node.view, info)?,
        };
        self.r.x.wrote(&st, &flat(), false);
        self.r.x.store_mut(&st).dtype = Some("float16".into());
        let x = &mut self.ts[t];
        x.buf = Some((st, "float16".into()));
        x.node = None;
        Ok(())
    }

    /// `_kind`: how a kernel over `view` reads `t`.
    fn kind(&self, t: usize, view: &[i64]) -> Result<&'static str, Refusal> {
        let x = &self.ts[t];
        let s = x.rows();
        if x.size() == 1 {
            return Ok("scalar");
        }
        if s.len() > view.len() {
            return unsupported(
                "API.READ.001",
                format!(
                    "a {} operand of a {} kernel",
                    py_tuple(&x.shape),
                    py_tuple(view)
                ),
            );
        }
        let mut s2 = vec![1; view.len() - s.len()];
        s2.extend(s);
        let k = view.len() - 1;
        if s2 == view {
            return Ok("full");
        }
        if s2[k] == view[k] && s2[..k].iter().all(|d| *d == 1) {
            return Ok("col");
        }
        if s2[k] == 1 && s2[..k] == view[..k] {
            return Ok("row");
        }
        unsupported(
            "API.READ.001",
            format!(
                "a {} operand broadcast to {}: kernels read whole rows, one row, a value a row or one value",
                py_tuple(&x.shape),
                py_tuple(view)
            ),
        )
    }

    /// `forms.f16` of a realised tensor's storage.
    fn f16(&mut self, t: usize, n: i64) -> Result<Addr, Refusal> {
        let (st, dt) = self.ts[t].buf.clone().unwrap();
        let nbytes = self.r.x.store(&st).nbytes;
        let whole = View {
            base: st,
            offset: 0,
            nbytes,
        };
        self.r.f16_of(&whole, tg_dtype(&dt), n)
    }

    /// `_addr`: the address and operand of a read.
    fn addr(&mut self, t: usize, kind: &str, stat_space: bool) -> Result<(Addr, Operand), Refusal> {
        let r = rows_of(&self.ts[t].rows());
        if stat_space && kind != "scalar" {
            let at = self.f16(t, r)?;
            return Ok((
                at,
                Operand {
                    rows: r,
                    ..Operand::new("row")
                },
            ));
        }
        if kind == "row" || kind == "scalar" {
            let n = self.ts[t].size().max(1);
            return Ok((self.f16(t, n)?, Operand::new(kind)));
        }
        if self.ts[t].stat {
            return unsupported(
                "API.READ.002",
                format!(
                    "a row statistic {} read as a row of values: keep the reduced axis (keepdim=True) so it broadcasts down the rows",
                    py_tuple(&self.ts[t].shape)
                ),
            );
        }
        let n = self.ts[t].size();
        let pitch = if kind == "full" {
            *self.ts[t].shape.last().unwrap()
        } else {
            0
        };
        Ok((
            self.f16(t, n)?,
            Operand {
                pitch,
                ..Operand::new(kind)
            },
        ))
    }

    fn bind(
        &mut self,
        ls: &[KE],
        view: &[i64],
        stat_space: bool,
    ) -> Result<(BTreeMap<String, Addr>, Vec<Operand>), Refusal> {
        let mut binds = BTreeMap::new();
        let mut ops = Vec::new();
        for (i, x) in ls.iter().enumerate() {
            let K::Ref(t) = x.as_ref() else {
                unreachable!("a read")
            };
            let kind = self.kind(*t, view)?;
            let (at, o) = self.addr(*t, kind, stat_space)?;
            binds.insert(format!("in{i}"), at);
            ops.push(o);
        }
        Ok((binds, ops))
    }

    /// `_out_storage`.
    fn out_storage(&mut self, t: usize, stat: bool) -> (String, Addr) {
        let rows = self.ts[t].rows();
        let r = rows_of(&rows);
        let nbytes = if stat {
            self.r.stat_bytes(r)
        } else {
            let band = self.r.hw.band;
            let overrun = (r + band - 1) / band * band - r;
            self.r.hw.f16_bytes((r + overrun) * rows.last().unwrap())
        };
        let st = self.r.x.storage(nbytes);
        let at = self.r.x.addr(&st, 0);
        (st, at)
    }

    /// A rows kernel's refusal as `Unsupported`, the output freed
    /// (`RowsError`, `ValueError`); any other refusal as it is.
    fn rows_refused(&mut self, st: &str, e: Refusal) -> Refusal {
        if e.clause == "TG.RUN.014" || e.clause.starts_with("API.ROW") {
            self.r.x.free(st);
            return Refusal::new("API.FUSE.003", e.message);
        }
        e
    }

    /// `_wide`: rows of values.
    fn wide(
        &mut self,
        t: usize,
        e: &E,
        ls: &[KE],
        view: &[i64],
        info: Value,
    ) -> Result<String, Refusal> {
        let (r, w) = (rows_of(view), *view.last().unwrap());
        let mut kinds = Vec::new();
        for x in ls {
            let K::Ref(u) = x.as_ref() else {
                unreachable!("a read")
            };
            kinds.push((*u, self.kind(*u, view)?));
        }
        let kblock = self.r.hw.kblock;
        if w % kblock != 0 {
            if kinds.iter().any(|(u, k)| *k != "full" || self.ts[*u].stat) {
                return unsupported(
                    "API.ROW.005",
                    format!(
                        "rows of {w} (not whole {kblock}-column blocks) with a broadcast operand"
                    ),
                );
            }
            let (st, out) = self.out_storage(t, false);
            let mut binds = BTreeMap::new();
            for (i, (u, _)) in kinds.iter().enumerate() {
                let n = self.ts[*u].size();
                binds.insert(format!("in{i}"), self.f16(*u, n)?);
            }
            binds.insert("out".into(), out);
            let call = Call {
                gen: "vector.flat_kernel".into(),
                args: json!({
                    "e": expr_arg(e),
                    "dtypes": vec!["f16"; ls.len()],
                    "n": self.ts[t].size(),
                    "out_dtype": "f16",
                    "cores": self.r.cores(),
                }),
            };
            self.r.x.launch(call, "dsl", info, binds);
            return Ok(st);
        }
        let (binds, ops) = self.bind(ls, view, false)?;
        let (st, out) = self.out_storage(t, false);
        let full = Operand {
            pitch: w,
            ..Operand::new("full")
        };
        match self.r.rows_run(e, &ops, &binds, r, w, &full, &out, &info) {
            Ok(()) => Ok(st),
            Err(err) => Err(self.rows_refused(&st, err)),
        }
    }

    /// `_statistic`: a row statistic.
    fn statistic(
        &mut self,
        t: usize,
        e: &E,
        ls: &[KE],
        view: &[i64],
        info: Value,
    ) -> Result<String, Refusal> {
        let r = rows_of(view);
        let (st, out) = self.out_storage(t, true);
        let w = *view.last().unwrap();
        let kblock = self.r.hw.kblock;
        let (binds, ops, w) = if w == 1 {
            let (b, o) = self.bind(ls, view, true)?;
            (b, o, kblock)
        } else {
            if w % kblock != 0 {
                return unsupported(
                    "API.ROW.005",
                    format!("a reduction of rows of {w}: whole {kblock}-column blocks"),
                );
            }
            let (b, o) = self.bind(ls, view, false)?;
            (b, o, w)
        };
        match self
            .r
            .rows_run(e, &ops, &binds, r, w, &Operand::new("row"), &out, &info)
        {
            Ok(()) => Ok(st),
            Err(err) => Err(self.rows_refused(&st, err)),
        }
    }

    /// `_matmul`: the product of `a` and `w` ([N, K]) with its epilogue.
    fn product(
        &mut self,
        t: usize,
        (a, w): (usize, usize),
        e: &E,
        extras: &[KE],
        view: &[i64],
        info: Value,
    ) -> Result<String, Refusal> {
        let (sa, sw) = (self.ts[a].shape.clone(), self.ts[w].shape.clone());
        let (m, k) = (sa[sa.len() - 2], sa[sa.len() - 1]);
        let n = sw[sw.len() - 2];
        let bt: i64 = if sa.len() > 2 {
            sa[..sa.len() - 2].iter().product()
        } else {
            1
        };
        let shared = sw.len() == 2;
        let knobs = self.r.knobs.matmul;
        let mut mm = Mm::plan(self.r.m, &knobs, bt, m, n, k, shared, None)
            .map_err(|e| Refusal::new("API.OP.006", e.message))?;
        let src = Operand {
            pitch: k,
            ..Operand::new("full")
        };
        let view_of = |me: &Self, u: usize| {
            let st = me.ts[u].buf.clone().unwrap();
            let nbytes = me.r.x.store(&st.0).nbytes;
            (
                View {
                    base: st.0,
                    offset: 0,
                    nbytes,
                },
                st.1,
            )
        };
        let (va, da) = view_of(self, a);
        let f16 = match mm.takes_f16(self.r.m) {
            true => self.r.f16_rows(&va, tg_dtype(&da), &src, k)?,
            false => None,
        };
        mm.knobs.activation_fp16 = f16.is_some();
        let qa = match f16 {
            Some(at) => at,
            None => self.r.packed(
                &va,
                tg_dtype(&da),
                &src,
                (m, k, mm.mp, mm.kp),
                "a",
                (mm.tm / self.r.hw.band, mm.nk, bt),
            )?,
        };
        let (vw, dw) = view_of(self, w);
        let nb = if shared { 1 } else { bt };
        let qb = self.r.packed(
            &vw,
            tg_dtype(&dw),
            &src,
            (n, k, n.max(mm.n), mm.kp),
            "b",
            (mm.tn / self.r.hw.band, mm.nk, nb),
        )?;
        let mut binds = BTreeMap::from([("a".to_string(), qa), ("b".to_string(), qb)]);
        let mut ops = Vec::new();
        let mut lays = Vec::new();
        let padded = mm.mp != m && !crate::mutant::on("memory/unsafe-bounds");
        for (i, x) in extras.iter().enumerate() {
            let K::Ref(u) = x.as_ref() else {
                unreachable!("a read")
            };
            let kind = self.kind(*u, view)?;
            let (mut at, o) = self.addr(*u, kind, false)?;
            if padded && (kind == "full" || kind == "row") {
                let w = if kind == "full" { o.pitch } else { 1 };
                let nbytes = if kind == "row" {
                    self.r.stat_bytes(bt * mm.mp)
                } else {
                    self.r.hw.f16_bytes(bt * mm.mp * w)
                };
                let lay = self.r.x.storage(nbytes);
                let to = self.r.x.addr(&lay, 0);
                self.batch_rows(at, to.clone(), bt, (m, mm.mp), w)?;
                lays.push(lay);
                at = to;
            }
            binds.insert(format!("in{}", i + 1), at);
            ops.push(o);
        }
        let (st, out) = self.out_storage(t, false);
        let tmp = padded.then(|| self.r.x.storage(self.r.hw.f16_bytes(bt * mm.mp * n)));
        let kout = match &tmp {
            Some(s) => self.r.x.addr(s, 0),
            None => out.clone(),
        };
        binds.insert("out".into(), kout.clone());
        let ms = |kind: UnitKind| self.r.m.units(kind).len() as i64;
        let call = mm.kernel_call(expr_arg(e), &ops, n, (ms(UnitKind::Mg), ms(UnitKind::Vc)));
        let mut info = info;
        info["mm"] = mm.made_by();
        self.r.x.launch(call, "matmul", info, binds);
        for lay in lays {
            self.r.x.free(&lay);
        }
        if let Some(s) = tmp {
            self.batch_rows(kout, out, bt, (mm.mp, m), n)?;
            self.r.x.free(&s);
        }
        Ok(st)
    }

    /// `bt` batches of rows `w` elements wide moved from `from` (each batch
    /// `rows.0` rows apart) into `to` (`rows.1` apart), the fewer of the two
    /// rows a batch.
    fn batch_rows(
        &mut self,
        from: Addr,
        to: Addr,
        bt: i64,
        rows: (i64, i64),
        w: i64,
    ) -> Result<(), Refusal> {
        let live = rows.0.min(rows.1) * w;
        self.r.relayout(
            from,
            bt * rows.0 * w,
            &[(bt, rows.0 * w), (live, 1)],
            0,
            &[(bt, rows.1 * w), (live, 1)],
            bt * rows.1 * w,
            to,
            0,
            false,
        )
    }
}
