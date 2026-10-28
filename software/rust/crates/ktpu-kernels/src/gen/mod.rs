//! The generators, one module a Python one (`kernels/*.py`).

pub mod convert;
pub mod lanes;
pub mod matmul;
pub mod transpose;
pub mod vector;

use crate::mutants;
use ktpu_graph as g;
use ktpu_machine::Machine;

/// Registers a value of a rows kernel's band takes ("8 registers a
/// value"): the kernels' register budget, stack policy, not a unit fact.
pub const BAND_REGS: i64 = 8;

/// The machine facts the generators read.
#[derive(Clone, Copy, Debug)]
pub struct Hw {
    /// Vector cores and clusters of a node.
    pub vc: i64,
    pub mg: i64,
    /// A core's L1 words (`vc.l1_words`).
    pub l1_words: i64,
    /// Bytes of a memory word (`die.word_bytes`).
    pub word_bytes: i64,
    /// f16 values a word holds, and a statistic word's rows (`vc.lanes`).
    pub lanes: i64,
    /// Values a vector register holds (`vc.vlmax`).
    pub vlmax: i64,
    /// Memory views a vector-core kernel may hold (`vc.descriptors`).
    pub descriptors: i64,
    /// A cluster bank's entries (`mg.l1_bank_entries`) and its
    /// accumulator's 4 x 4 sub-tiles (`mg.tiles`).
    pub bank: i64,
    pub acc_subtiles: i64,
    /// Rows of a GT4 band, the side of an accumulator sub-tile (`mg.lanes`).
    pub band: i64,
    /// Columns of a GT4 block, K of one MXFP7 entry (`mg.kblock`).
    pub kblock: i64,
    /// K blocks a cluster step takes (`mg.kstep`).
    pub kstep: i64,
}

impl Hw {
    pub fn of(m: &Machine) -> Hw {
        let (band, kblock, kstep) = if mutants::on("kernels.hw/unit_constants") {
            (4, 32, 2)
        } else {
            (m.mg.lanes as i64, m.mg.kblock as i64, m.mg.kstep as i64)
        };
        Hw {
            band,
            kblock,
            kstep,
            vc: m.vc.count as i64,
            mg: m.mg.count as i64,
            l1_words: if mutants::on("api.vector/l1_small") {
                256
            } else {
                m.vc.l1_words as i64
            },
            word_bytes: m.die.word_bytes as i64,
            lanes: m.vc.lanes as i64,
            vlmax: m.vc.vlmax as i64,
            descriptors: m.vc.descriptors as i64,
            bank: if mutants::on("isa.cluster/plan") {
                32
            } else {
                m.mg.l1_bank_entries as i64
            },
            acc_subtiles: if mutants::on("isa.cluster/plan") {
                2048
            } else {
                m.mg.tiles as i64
            },
        }
    }

    /// The widest column block: a band's value in [`BAND_REGS`] registers.
    pub fn max_block(&self) -> i64 {
        BAND_REGS * self.vlmax / self.band
    }

    /// Rows a statistic word holds, and a trip's rows when one is read or
    /// written.
    pub fn stat_rows(&self) -> i64 {
        if mutants::on("api.vector/stat_rows") {
            8
        } else if mutants::on("num.stat/32-a-word") {
            32
        } else {
            self.lanes
        }
    }

    /// Bytes of an element.
    pub fn item(dtype: g::Dtype) -> i64 {
        match dtype {
            g::Dtype::F16 => 2,
            g::Dtype::F32 | g::Dtype::U32 | g::Dtype::I32 => 4,
            g::Dtype::U64 => 8,
            g::Dtype::U8 | g::Dtype::Mx7a | g::Dtype::Mx7b => 1,
        }
    }
}

/// `ceil(a / b)` for positive b.
pub(crate) fn cdiv(a: i64, b: i64) -> i64 {
    -(-a).div_euclid(b)
}
