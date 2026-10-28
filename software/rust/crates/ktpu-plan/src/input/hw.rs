//! The unit facts the run paths size by, each read from the machine (2p.3:
//! the machine file is their only source), and the sizes derived from them.

use ktpu_machine::Machine;

/// Registers a value of a rows kernel's band takes (`vector.MAX_BLOCK`'s
/// "8 registers a value"): the kernels' register budget, not a unit fact.
pub const BAND_REGS: i64 = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Hw {
    /// bytes of a memory word (`die.word_bytes`)
    pub word: i64,
    /// f16 values of a memory word
    pub halves: i64,
    /// vector lanes (`vc.lanes`)
    pub lanes: i64,
    /// values of a vector register (`vc.vlmax`)
    pub vlmax: i64,
    /// rows of a GT4 band, the side of an accumulator subtile (`mg.lanes`)
    pub band: i64,
    /// columns of a GT4 block, K of one MXFP7 entry (`mg.kblock`)
    pub kblock: i64,
    /// K blocks a cluster step takes (`mg.kstep`)
    pub kstep: i64,
    /// accumulator subtiles of a cluster (`mg.tiles`)
    pub acc_subtiles: i64,
    /// entries of one cluster L1 bank (`mg.l1_bank_entries`)
    pub bank: i64,
}

impl Hw {
    pub fn of(m: &Machine) -> Hw {
        let f = m.facts();
        let word = f.die.word_bytes as i64;
        Hw {
            word,
            halves: word / 2,
            lanes: f.vc.lanes as i64,
            vlmax: f.vc.vlmax as i64,
            band: f.mg.lanes as i64,
            kblock: f.mg.kblock as i64,
            kstep: f.mg.kstep as i64,
            acc_subtiles: f.mg.tiles as i64,
            bank: f.mg.l1_bank_entries as i64,
        }
    }

    /// The widest column block a band's registers take (`vector.MAX_BLOCK`).
    pub fn max_block(&self) -> i64 {
        BAND_REGS * self.vlmax / self.band
    }

    /// Column blocks from [`Hw::max_block`] halving down to one GT4 block,
    /// widest first (`vector.block`'s candidates, `run.FOLD_WIDTHS`).
    pub fn blocks(&self) -> Vec<i64> {
        let mut out = Vec::new();
        let mut w = self.max_block();
        while w >= self.kblock {
            out.push(w);
            w /= 2;
        }
        out
    }

    /// `n` rounded up to whole f16 words.
    pub fn whole_halves(&self, n: i64) -> i64 {
        (n + self.halves - 1) / self.halves * self.halves
    }

    /// `n` rounded up to whole GT4 blocks.
    pub fn whole_blocks(&self, n: i64) -> i64 {
        (n + self.kblock - 1) / self.kblock * self.kblock
    }

    /// Bytes of an f16 storage of `n` values, at least one word.
    pub fn f16_bytes(&self, n: i64) -> i64 {
        (2 * n).max(self.word)
    }
}
