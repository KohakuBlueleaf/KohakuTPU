//! How a contraction pairs its K blocks (NUM.ACC.008, RUL.099).
//!
//! The cluster pump merges a K pair's two block partials at the larger
//! exponent sum with no zero test, so an all-zero block needs the E=0 scale
//! field. Vector quantisation gives it that field; mover QUANT gives E=20. On
//! a machine whose preferred backend is mover QUANT the stack either lowers
//! paired (`nk` >= 2) destinations through the vector cores (`Vector`) or
//! plans its contractions unpaired, `nk` = 1 (`Unpaired`).

use crate::{Machine, PackingBackend};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Pairing {
    Vector,
    Unpaired,
}

/// The stack's pairing: the cheaper of the two on the measured kernels
/// (`ZEROBLK.md`).
pub const PAIRING: Pairing = Pairing::Unpaired;

impl Pairing {
    pub fn name(self) -> &'static str {
        match self {
            Pairing::Vector => "vector",
            Pairing::Unpaired => "unpaired",
        }
    }

    pub fn parse(name: &str) -> Option<Pairing> {
        [Pairing::Vector, Pairing::Unpaired]
            .into_iter()
            .find(|p| p.name() == name)
    }
}

impl Machine {
    /// Whether a contraction planned under `pairing` may pair K blocks.
    pub fn pairs_blocks(&self, pairing: Pairing) -> bool {
        pairing == Pairing::Vector
            || self.conversion.fp16_mx7.preferred_backend != PackingBackend::MoverQuant
            || crate::mutant::on("machine.pairing/mover-paired")
    }
}
