//! A rows kernel run over [R, W] (`runtime/rows.py`): its operands based,
//! launched; a reduction across column blocks as per-block partials folded;
//! an innermost reduction first into a statistic.

use super::*;

#[cfg(test)]
mod deletion;

impl Runner<'_> {
    /// `rows._based`: each offset moved into its bound address where whole
    /// words.
    fn based(
        &self,
        ops: &[Operand],
        binds: &mut BTreeMap<String, Addr>,
        port: &str,
    ) -> Vec<Operand> {
        let (h, word) = (self.hw.halves, self.hw.word);
        let mut out = Vec::new();
        for (i, o) in ops.iter().enumerate() {
            let key = if port == "in" {
                format!("in{i}")
            } else {
                port.to_string()
            };
            let it = if o.dtype == "f32" { 4 } else { 2 };
            let moved = if o.kind == "scalar" {
                o.offset.div_euclid(h) * h
            } else {
                o.offset
            };
            let shift = it * moved;
            let rows = if o.kind == "row" || o.kind == "scalar" {
                (o.rows - moved).max(0)
            } else if o.pitch != 0 {
                (o.rows - o.offset.div_euclid(o.pitch)).max(0)
            } else {
                o.rows
            };
            if moved == 0 || shift.rem_euclid(word) != 0 || !binds.contains_key(&key) {
                out.push(o.clone());
                continue;
            }
            binds.get_mut(&key).unwrap().offset += if mutant::on("tg.rows/based_zero") {
                0
            } else {
                shift
            };
            out.push(Operand {
                offset: o.offset - moved,
                rows,
                ..o.clone()
            });
        }
        out
    }

    /// `rows._launch`.
    #[allow(clippy::too_many_arguments)]
    fn launch_rows(
        &mut self,
        e: &E,
        ops: &[Operand],
        mut binds: BTreeMap<String, Addr>,
        r: i64,
        w: i64,
        out: &Operand,
        info: Value,
    ) -> Result<(), Refusal> {
        let ops = self.based(ops, &mut binds, "in");
        let out = self
            .based(std::slice::from_ref(out), &mut binds, "out")
            .remove(0);
        self.rows_kernel(e, &ops, r, w, &out)?;
        let call = Call {
            gen: "vector.rows_kernel".into(),
            args: json!({
                "e": expr_arg(e),
                "operands": ops.iter().map(Operand::json).collect::<Vec<_>>(),
                "R": r,
                "W": w,
                "out": out.json(),
                "cores": self.cores(),
            }),
        };
        self.x.launch(call, "dsl", info, binds);
        Ok(())
    }

    /// `rows._whole_rows`.
    fn whole_rows(&self, ops: &[Operand], w: i64) -> Result<(), Refusal> {
        let h = self.hw.halves;
        for o in ops {
            if o.kind == "full"
                && (o.offset.rem_euclid(h) != 0
                    || o.pitch.rem_euclid(h) != 0
                    || o.offset.rem_euclid(o.pitch) + w > o.pitch)
            {
                return Err(Refusal::new(
                    "API.ROW.001",
                    format!(
                        "a rows kernel read off whole rows of {w}: {}",
                        py_operand(o)
                    ),
                ));
            }
            if o.kind == "col" && o.offset.rem_euclid(h) != 0 {
                return Err(Refusal::new(
                    "API.ROW.001",
                    format!("a rows kernel row read off a word: {}", py_operand(o)),
                ));
            }
        }
        Ok(())
    }

    /// `rows.run`: `e` over [R, W] into `out` at `out_at`.
    #[allow(clippy::too_many_arguments)]
    pub fn rows_run(
        &mut self,
        e: &E,
        ops: &[Operand],
        binds: &BTreeMap<String, Addr>,
        r: i64,
        w: i64,
        out: &Operand,
        out_at: &Addr,
        info: &Value,
    ) -> Result<(), Refusal> {
        self.whole_rows(ops, w)?;
        let reds = all_reduces(e);
        let blocks = if mutant::on("memory/unsafe-bounds") || mutant::on("api.rows/max_block") {
            if w > self.max_block() {
                2
            } else {
                1
            }
        } else if let Some(red) = reds.first() {
            self.rows_kernel(red, ops, r, w, &Operand::new("row"))?.0
        } else {
            1
        };
        if blocks > 1 && !reds.is_empty() && (out.kind != "row" || reds.len() > 1) {
            return self.stat_first(e, ops, binds, r, w, out, out_at, info);
        }
        let mut b = binds.clone();
        b.insert("out".into(), out_at.clone());
        if out.kind != "row" {
            return self.launch_rows(e, ops, b, r, w, out, info.clone());
        }
        if blocks == 1 {
            return self.launch_rows(e, ops, b, r, w, &Operand::new("row"), info.clone());
        }
        let red = reds[0].clone();
        let (nblk, shape) = self.rows_kernel(&red, ops, r, w, &Operand::new("row"))?;
        let rq = *shape.last().unwrap();
        let nbytes = 2 * shape.iter().product::<i64>();
        let mut parts = [self.x.raw(nbytes), self.x.raw(nbytes)];
        let got = self.partials(
            e,
            &red,
            ops,
            binds,
            (r, w, nblk, rq),
            &mut parts,
            out,
            out_at,
            info,
        );
        for p in &parts {
            self.x.free(p);
        }
        got
    }

    /// `rows.run`'s partials: one a column block, halved to one, `e`
    /// around the reduction over it.
    #[allow(clippy::too_many_arguments)]
    fn partials(
        &mut self,
        e: &E,
        red: &E,
        ops: &[Operand],
        binds: &BTreeMap<String, Addr>,
        (r, w, nblk, rq): (i64, i64, i64, i64),
        parts: &mut [String; 2],
        out: &Operand,
        out_at: &Addr,
        info: &Value,
    ) -> Result<(), Refusal> {
        let part = Operand::new("row");
        let mut b1 = binds.clone();
        b1.insert("out".into(), self.x.addr(&parts[0], 0));
        let mut info1 = info.clone();
        info1["blocks"] = json!(nblk);
        self.launch_rows(red, ops, b1, r, w, &part, info1)?;
        let Expr::Reduce(rop, _) = red.as_ref() else {
            unreachable!()
        };
        let both = expr::op(fold_op(rop), vec![expr::load(0), expr::load(1)]);
        let mut width = nblk;
        while width > 1 {
            let half = width / 2;
            let (src, dst) = (parts[0].clone(), parts[1].clone());
            let n = half * rq;
            let o2 = [
                Operand {
                    rows: n,
                    ..Operand::new("row")
                },
                Operand {
                    rows: n,
                    offset: (width - half) * rq,
                    ..Operand::new("row")
                },
            ];
            let b2 = BTreeMap::from([
                ("in0".to_string(), self.x.addr(&src, 0)),
                ("in1".to_string(), self.x.addr(&src, 0)),
            ]);
            let at = self.x.addr(&dst, 0);
            self.rows_run(&both, &o2, &b2, n, self.hw.kblock, &part, &at, info)?;
            if width - half > half {
                let call = Call {
                    gen: "convert.copy_kernel".into(),
                    args: json!({"nbytes": 2 * rq}),
                };
                let at = BTreeMap::from([
                    ("src".to_string(), self.x.addr(&src, 2 * half * rq)),
                    ("dst".to_string(), self.x.addr(&dst, 2 * half * rq)),
                ]);
                self.x.launch(call, "copy", json!({}), at);
            }
            parts.reverse();
            width -= half;
        }
        let (post, keep) = substitute(e, Some(red), Some(&expr::load(0)), 1);
        let mut ops2 = vec![Operand {
            rows: r,
            ..Operand::new("row")
        }];
        let mut b2 = BTreeMap::from([("in0".to_string(), self.x.addr(&parts[0], 0))]);
        for (j, i) in keep.iter().enumerate() {
            if ops[*i].kind != "row" && ops[*i].kind != "scalar" {
                return row_err(
                    "a row-wide operand outside a reduction whose result is a statistic",
                );
            }
            ops2.push(ops[*i].clone());
            b2.insert(format!("in{}", j + 1), binds[&format!("in{i}")].clone());
        }
        self.rows_run(&post, &ops2, &b2, r, self.hw.kblock, out, out_at, info)
    }

    /// `rows._stat_first`: an innermost reduction first into a statistic,
    /// read back as a row operand.
    #[allow(clippy::too_many_arguments)]
    fn stat_first(
        &mut self,
        e: &E,
        ops: &[Operand],
        binds: &BTreeMap<String, Addr>,
        r: i64,
        w: i64,
        out: &Operand,
        out_at: &Addr,
        info: &Value,
    ) -> Result<(), Refusal> {
        if mutant::on("tg.rows/stat_first_skip") {
            return Ok(());
        }
        self.x.paths.insert("rows._stat_first");
        let leaf = leaf_reduce(e);
        let (inner, used) = substitute(&leaf, None, None, 0);
        let o1: Vec<Operand> = used.iter().map(|i| ops[*i].clone()).collect();
        let held = Operand {
            rows: r,
            ..Operand::new("row")
        };
        let stat = self.x.raw(self.stat_bytes(r));
        let at = self.x.addr(&stat, 0);
        let b1: BTreeMap<String, Addr> = used
            .iter()
            .enumerate()
            .map(|(j, i)| (format!("in{j}"), binds[&format!("in{i}")].clone()))
            .collect();
        let got = self
            .rows_run(&inner, &o1, &b1, r, w, &held, &at, info)
            .and_then(|_| {
                if mutant::on("tg.rows/stat_continue_skip") {
                    return Ok(());
                }
                let n = ops.len();
                let post = swap(e, &leaf, &expr::load(n));
                let mut b2 = binds.clone();
                b2.insert(format!("in{n}"), at.clone());
                let mut o2 = ops.to_vec();
                o2.push(held.clone());
                self.rows_run(&post, &o2, &b2, r, w, out, out_at, info)
            });
        self.x.free(&stat);
        got
    }
}
