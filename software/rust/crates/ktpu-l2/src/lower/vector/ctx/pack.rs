//! A band's packs into the buffers its stores and sends drain (L2.OP.025,
//! .026, L2.STAT.003, .008).

use super::*;

mod typed;

impl<'a> Ctx<'a> {
    fn store_dests(&self, ops: &[Arc<Node>], target: &str, with_send: bool) -> Vec<usize> {
        let mut seen: IndexMap<Vec<i64>, usize> = IndexMap::new();
        for n in ops {
            let s = op(n);
            let pos = s.st.positional();
            let hit = (s.op == "store" && pos.get(2).and_then(|p| p.as_name()) == Some(target))
                || (with_send
                    && s.op == "send"
                    && pos.first().and_then(|p| p.as_name()) == Some(target));
            if hit {
                let d = self.of[&s.uid];
                // One pack a buffer: a later store of the same buffers wins the slot.
                seen.insert(self.descs[d].buf.clone(), d);
            }
        }
        seen.into_values().collect()
    }

    #[cfg(feature = "witness")]
    pub(super) fn packs(
        &mut self,
        g: &mut Gen,
        ops: &[Arc<Node>],
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
        parity: usize,
    ) -> VResult<()> {
        for n in ops {
            let o = op(n);
            if o.op != "pack" {
                continue;
            }
            let arg = o.st.positional()[0].clone();
            let stat = self.get(o, &arg, None, Some(local))?;
            if stat.kind == "stat" {
                self.pack_stat(g, o, ops, &stat, rows, local, parity)?;
                continue;
            }
            let src = self.get(o, &arg, Some("row"), Some(local))?;
            let target = o.target.clone().unwrap();
            let dests = self.store_dests(ops, &target, true);
            if dests.is_empty() {
                return Err(self.fail(&o.st, "L2.OP.025", "a packed value is stored or sent"));
            }
            let buf = self.descs[dests[0]].buf[parity];
            let t = ty(o);
            if t.dtype == "mx7a" || t.dtype == "mx7b" {
                if src.mode != "gt4" {
                    return Err(self.fail(
                        &o.st,
                        "L2.OP.026",
                        "an MXFP7 pack takes 4 x 32 blocks: a @reg(gt4) value",
                    ));
                }
                let only = read_once(ops, o);
                for &r in rows {
                    let tsym = g.sym();
                    let reg = src.regs[&r].clone();
                    if !(only && quantiser_order(g, &reg, &tsym)) {
                        for i in 0..4 {
                            for q in 0..2 {
                                g.emit(
                                    format!(
                                        "vmov {{0}}[{}:], {{1}}[{}:] vl=16",
                                        2 * i + q,
                                        4 * q + i
                                    ),
                                    &[&tsym, &reg],
                                );
                            }
                        }
                    }
                    for &d in &dests {
                        let at = self.descs[d].buf[parity] + r * 4;
                        g.emit(format!("vpack {{0}} -> l1[{at}] {}", t.dtype), &[&tsym]);
                    }
                }
                let mut v = Val::new("l1", count_of(t, self.t.vlmax)).with_mode(&t.dtype);
                v.buf = buf;
                local.insert(target, v);
                continue;
            }
            let mode = mode_of(Some(t));
            let packed = match crate::mutant::on("l2.vector/pack-first-dest") {
                true => &dests[..1],
                false => &dests[..],
            };
            for &d in packed {
                for (_, i) in self.once(&src, rows)? {
                    let line =
                        pack_line(&self.descs[d], self.descs[d].buf[parity], i, mode, &t.dtype);
                    g.emit(line, &[&src.regs[&i]]);
                }
            }
            let mut v = Val::new("l1", count_of(t, self.t.vlmax)).with_mode(mode);
            v.buf = buf;
            local.insert(target, v);
        }
        Ok(())
    }

    /// A GT4 band statistic packed 16 rows a word.
    #[allow(clippy::too_many_arguments)]
    #[cfg(feature = "witness")]
    fn pack_stat(
        &mut self,
        g: &mut Gen,
        o: &Op,
        ops: &[Arc<Node>],
        stat: &Val,
        rows: &[i64],
        local: &mut IndexMap<String, Val>,
        parity: usize,
    ) -> VResult<()> {
        let t = ty(o);
        if t.shape.len() != 1 {
            if !crate::mutant::on("l2.vector/statistic-band-pack") {
                return Err(self.fail(&o.st, "RUL.030", super::super::RUL_030));
            }
            let target = o.target.clone().unwrap();
            let dests = self.store_dests(ops, &target, false);
            let bands: std::collections::BTreeSet<_> =
                rows.iter().map(|r| r / self.per_band).collect();
            for &d in &dests {
                for &b in &bands {
                    g.emit(
                        pack_line(&self.descs[d], self.descs[d].buf[parity], b, "gt4", "f16"),
                        &[&stat.regs[&b]],
                    );
                }
            }
            let mut v = Val::new("l1", bands.len() as i64).with_mode("gt4");
            v.buf = self.descs[dests[0]].buf[parity];
            local.insert(target, v);
            return Ok(());
        }
        if self.per_band == 0 {
            return Err(self.fail(
                &o.st,
                "L2.STAT.008",
                "a statistic packs from GT4 rows as f16[R] @l1",
            ));
        }
        let target = o.target.clone().unwrap();
        let dests = self.store_dests(ops, &target, false);
        if dests.is_empty() {
            return Err(self.fail(&o.st, "L2.OP.025", "a packed value is stored"));
        }
        let buf = self.descs[dests[0]].buf[parity];
        let (nb, rows_out) = (self.tile / self.per_band, t.shape[0]);
        if rows_out % 16 != 0 || (rows_out != 4 * nb && rows_out != 16 * (nb == 1) as i64) {
            return Err(self.fail(
                &o.st,
                "L2.STAT.003",
                "a statistic packs 16 rows (four bands, or one band every row's) a word",
            ));
        }
        if rows.iter().any(|r| r / self.per_band == nb - 1) {
            for w in 0..rows_out / 16 {
                let regs: Vec<String> = (0..4)
                    .map(|k| stat.regs[&((4 * w + k) % nb)].clone())
                    .collect();
                let word = compact(g, &regs);
                for &d in &dests {
                    g.emit(
                        format!(
                            "vpack {{0}} -> l1[{}] f16 n=1",
                            self.descs[d].buf[parity] + w
                        ),
                        &[&word],
                    );
                }
            }
        }
        let mut v = Val::new("l1", rows_out / 16);
        v.buf = buf;
        local.insert(target, v);
        Ok(())
    }
}
