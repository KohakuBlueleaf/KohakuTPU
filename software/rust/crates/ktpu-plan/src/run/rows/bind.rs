//! Each read bound to the rows kernel's view: as it is where the kernel
//! takes it, else laid out as f16 rows (`_bind`, `_laid_read`, `_laid_plan`,
//! `_table_over`).

use super::*;

impl Runner<'_> {
    /// `_bind`: read `a` as it is where the kernel takes it, else laid out
    /// over every row it reads and every column, rows W' apart.
    pub(super) fn bind(
        &mut self,
        plan: &Plan,
        a: &Access,
        (rows, cols, wp): (&[Id], &[Id], i64),
        bufs: &Bufs,
        tmps: &mut Vec<String>,
    ) -> Result<(Addr, Operand), Refusal> {
        let o = self.classify(a, rows, cols);
        let off = bufs.get(&a.slot).map(|v| v.offset).unwrap_or(0);
        let padded_column = o.as_ref().is_some_and(|o| o.kind == "col")
            && self.size(cols) < wp
            && !mutant::on("memory/unsafe-bounds");
        if !padded_column && self.takes(o.as_ref(), wp, off.div_euclid(item(&a.dtype))) {
            return self.addr_of(a, o.as_ref().unwrap(), bufs);
        }
        let (lr, lc) = (self.live(rows), self.live(cols));
        let over_r = if lr.iter().any(|r| a.c(*r) != 0) {
            lr
        } else {
            vec![]
        };
        let over_c = if lc.iter().any(|r| a.c(*r) != 0) {
            lc
        } else {
            vec![]
        };
        let pitch = if over_c.is_empty() { 1 } else { wp };
        let key = self.key("laid");
        let t = self.laid_read(plan, a, (&over_r, &over_c, pitch), bufs, &key)?;
        tmps.push(t.clone());
        let mut coef: Vec<(Id, i64)> = self
            .dense(&over_r)
            .into_iter()
            .map(|(r, s)| (r, s * pitch))
            .collect();
        for (r, s) in self.dense(&over_c) {
            put(&mut coef, r, s);
        }
        let laid = Access {
            slot: key.clone(),
            dtype: "half".into(),
            coef,
            off: 0,
            table: None,
            gate: false,
        };
        let o = self
            .classify(&laid, rows, cols)
            .expect("a laid read is a row form");
        let nbytes = self.x.store(&t).nbytes;
        let b2: Bufs = HashMap::from([(
            key,
            View {
                base: t,
                offset: 0,
                nbytes,
            },
        )]);
        self.addr_of(&laid, &o, &b2)
    }

    /// `_table_over`: a table's f16 values over `order`, each range it does
    /// not read broadcast.
    pub fn table_over(&self, acc: &Access, order: &[Id]) -> Vec<u16> {
        let t = acc.table.as_ref().unwrap();
        let ext: Vec<i64> = order.iter().map(|r| self.extent(*r)).collect();
        let n: i64 = ext.iter().product();
        let mut out = Vec::with_capacity(n as usize);
        let mut idx = vec![0i64; order.len()];
        for _ in 0..n {
            let at = acc.off
                + idx
                    .iter()
                    .zip(order)
                    .map(|(i, r)| acc.c(*r) * i)
                    .sum::<i64>();
            out.push(t.bits[at as usize]);
            for d in (0..order.len()).rev() {
                idx[d] += 1;
                if idx[d] < ext[d] {
                    break;
                }
                idx[d] = 0;
            }
        }
        out
    }

    /// Host f16 values written at the start of `t`.
    pub fn write_u16(&mut self, t: &str, vals: &[u16]) {
        let mut h = Sha256::new();
        for v in vals {
            h.update(v.to_le_bytes());
        }
        let at = self.x.addr(t, 0);
        self.x.write(at, 2 * vals.len() as i64, hex(&h.finalize()));
    }

    /// `_laid_read`: `a` as f16 over [over_r.., over_c..], rows `pitch`
    /// apart: a table on the host, a buffer's by the bare-read kernels.
    fn laid_read(
        &mut self,
        plan: &Plan,
        a: &Access,
        (over_r, over_c, pitch): (&[Id], &[Id], i64),
        bufs: &Bufs,
        key: &Slot,
    ) -> Result<String, Refusal> {
        let order: Vec<Id> = over_r.iter().chain(over_c).copied().collect();
        let (r, w) = (self.size(over_r), self.size(over_c));
        let t = self.x.storage(self.hw.f16_bytes(r * pitch));
        self.x.store_mut(&t).dtype = Some("float16".into());
        if a.slot == Slot::Table {
            let vals = self.table_over(a, &order);
            let mut laid = vec![0u16; (r * pitch) as usize];
            for i in 0..r {
                for j in 0..w {
                    laid[(i * pitch + j) as usize] = vals[(i * w + j) as usize];
                }
            }
            self.write_u16(&t, &laid);
            return Ok(t);
        }
        let dense = if pitch == w {
            t.clone()
        } else {
            self.x.storage(self.hw.f16_bytes(r * w))
        };
        let lp = self.laid_plan(plan, key.clone(), a, &order, None);
        let mut b2 = bufs.clone();
        let nbytes = self.x.store(&dense).nbytes;
        b2.insert(
            key.clone(),
            View {
                base: dense.clone(),
                offset: 0,
                nbytes,
            },
        );
        let got = match self.bare(&lp, &b2) {
            Ok(false) => Err(Refusal::new(
                "TG.RUN.016",
                format!("a read with no f16 form: strides {:?}", a.coef),
            )),
            Ok(true) if dense != t => {
                let (src, dst) = (self.x.addr(&dense, 0), self.x.addr(&t, 0));
                self.repitch(src, w, dst, pitch, r, w)
            }
            Ok(true) => Ok(()),
            Err(e) => Err(e),
        };
        if got.is_err() {
            self.x.free(&t);
        }
        if dense != t {
            self.x.free(&dense);
        }
        got.map(|_| t)
    }

    /// `_laid_plan`: `acc` as f16 over `order` into `key`, dense or at
    /// element `strides`.
    pub fn laid_plan(
        &self,
        plan: &Plan,
        key: Slot,
        acc: &Access,
        order: &[Id],
        strides: Option<Vec<i64>>,
    ) -> Plan {
        Plan {
            kind: "vector".into(),
            name: format!("{}_l", plan.name),
            out_slot: key,
            out_dtype: "half".into(),
            out_ranges: order.to_vec(),
            out_shape: order.iter().map(|r| self.extent(*r)).collect(),
            out_strides: strides,
            expr: Some(expr::load(0)),
            accesses: vec![acc.clone()],
            reduce_ranges: vec![],
            reductions: vec![],
            a: None,
            b: None,
            mnk: None,
            batch: vec![],
            ..plan.clone()
        }
    }
}
