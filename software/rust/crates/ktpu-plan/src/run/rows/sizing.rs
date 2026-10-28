//! A rows kernel's machine facts and sizing (`vector.plan`,
//! `vector.rows_kernel`'s checks).

use super::*;

impl Runner<'_> {
    // --------------------------------------------------- machine facts
    /// Rows a word of a statistic holds (`STAT_ROWS`: f16 values a word).
    pub fn stat_rows(&self) -> i64 {
        self.m.facts().die.word_bytes as i64 / 2
    }

    pub fn stat_words(&self, r: i64) -> i64 {
        (r + self.stat_rows() - 1) / self.stat_rows()
    }

    /// `rows.stat_bytes`: R statistic values in whole words.
    pub fn stat_bytes(&self, r: i64) -> i64 {
        self.stat_words(r) * self.m.facts().die.word_bytes as i64
    }

    /// Reads a vector-core kernel walks beside its output (`MAX_READS`).
    pub fn max_reads(&self) -> usize {
        if mutant::on("tg.run/narrow_more") {
            return 16;
        }
        self.m.facts().vc.descriptors as usize - 1
    }

    /// `V.MAX_BLOCK` where Python reads it at run time (a block search's
    /// default `most` is bound at definition, so stays [`Hw::max_block`]).
    pub fn max_block(&self) -> i64 {
        if mutant::on("api.rows/max_block") {
            4096
        } else {
            self.hw.max_block()
        }
    }

    // ------------------------------------------------- kernel sizing
    fn block(&self, w: i64) -> Result<i64, Refusal> {
        self.hw
            .blocks()
            .into_iter()
            .find(|wc| w % wc == 0)
            .ok_or_else(|| {
                Refusal::new(
                    "API.ROW.008",
                    format!("{w} columns do not split into blocks of {}", self.hw.kblock),
                )
            })
    }

    /// L1 words of one band of `wc` f16 columns.
    fn band_words(&self, wc: i64) -> i64 {
        self.hw.band * wc / self.hw.halves
    }

    /// `vector.bands_fit`: bands the L1 holds double-buffered.
    fn bands_fit(&self, ops: &[Operand], out_kind: &str, wc: i64) -> i64 {
        let mut words: i64 = ops
            .iter()
            .filter(|o| o.kind == "full" || o.kind == "tile")
            .map(|_| self.band_words(wc))
            .sum();
        words += ops
            .iter()
            .filter(|o| o.kind == "row" || o.kind == "scalar")
            .count() as i64;
        words += if out_kind == "row" {
            1
        } else {
            self.band_words(wc)
        };
        let prologue: i64 = ops
            .iter()
            .filter(|o| o.kind == "col")
            .map(|_| wc / self.hw.halves)
            .sum();
        let room = self.m.facts().vc.l1_words as i64 - L1_MARGIN - prologue;
        if words == 0 {
            MAX_BANDS
        } else {
            room.div_euclid(2 * words)
        }
    }

    /// `vector.plan`.
    fn sizing(
        &self,
        ops: &[Operand],
        out_kind: &str,
        r: i64,
        w: i64,
        quantum: i64,
    ) -> Result<Sizing, Refusal> {
        let (band, kblock) = (self.hw.band, self.hw.kblock);
        if w % kblock != 0 {
            return Err(Refusal::new(
                "API.ROW.008",
                format!("a GT4 row is whole {kblock}-column blocks, not {w}"),
            ));
        }
        let need = (quantum / band).max(1);
        let widest = if w <= self.max_block() {
            w
        } else {
            self.block(w)?
        };
        let blocks: Vec<i64> = (1..=widest / kblock)
            .rev()
            .map(|k| kblock * k)
            .filter(|c| w % c == 0)
            .collect();
        let wc = blocks
            .iter()
            .copied()
            .find(|c| self.bands_fit(ops, out_kind, *c) >= need)
            .unwrap_or(*blocks.last().unwrap());
        let fit = self.bands_fit(ops, out_kind, wc);
        if fit < need {
            return Err(Refusal::new(
                "API.ROW.002",
                format!("{need} bands of {wc} columns do not fit a core's L1"),
            ));
        }
        let b = fit.clamp(1, MAX_BANDS);
        let mut rows = band * b;
        while rows % quantum != 0 {
            rows += band;
        }
        let nb = (r + rows - 1) / rows;
        Ok(Sizing {
            wc,
            nblk: w / wc,
            rows,
            nb,
            rp: nb * rows,
        })
    }

    /// `vector.rows_kernel`'s checks: `(blocks, out shape of a statistic)`.
    pub(super) fn rows_kernel(
        &self,
        e: &E,
        ops: &[Operand],
        r: i64,
        w: i64,
        out: &Operand,
    ) -> Result<(i64, Vec<i64>), Refusal> {
        let reds = expr::reduces(e);
        let stats = ops.iter().all(|o| o.kind == "row" || o.kind == "scalar");
        if out.kind == "row" && reds.is_empty() && !stats {
            return Err(Refusal::new(
                "API.ROW.009",
                "a statistic output comes from a row reduction or statistics",
            ));
        }
        let holds = out.kind == "row" || ops.iter().any(|o| o.kind == "row");
        let quantum = if holds {
            self.stat_rows()
        } else {
            self.hw.band
        };
        let pl = self.sizing(ops, &out.kind, r, w, quantum)?;
        if pl.nblk > 1 && reds.len() > 1 {
            return Err(Refusal::new(
                "API.ROW.009",
                "several reductions over a row wider than one block",
            ));
        }
        let rows = out.rows.max(self.stat_words(r) * self.stat_rows());
        let shape = if pl.nblk > 1 {
            vec![pl.nblk, rows]
        } else {
            vec![rows]
        };
        Ok((pl.nblk, shape))
    }
}
