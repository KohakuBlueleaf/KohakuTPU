//! Where a unit word carries a memory address: the fields a package
//! relocates (`software/compiler/kohakutpu/compiler/isa/fields.py`).

use crate::layout::W256;
use crate::Isa;
use ktpu_machine::UnitKind;
use serde::{Deserialize, Serialize};

/// `width` bits at `bit` of a word hold bits `[shift + width - 1 : shift]`
/// of a value.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Segment {
    pub bit: u32,
    pub width: u32,
    pub shift: u32,
}

/// One address a word carries: its segments and the address they hold.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct AddressField {
    pub segments: Vec<Segment>,
    pub value: u64,
}

/// The unit a word is a payload for.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Target {
    Unit(UnitKind),
    /// The node's mover: a MOVER-step payload of two register writes.
    Mover,
}

/// Every address field of `word`, a payload for `target`.
pub fn addresses(isa: &Isa, word: W256, target: Target) -> Vec<AddressField> {
    match target {
        Target::Unit(UnitKind::Mg) => isa.cluster.addresses(word),
        Target::Unit(UnitKind::Vc) => isa.vector.addresses(word),
        Target::Unit(UnitKind::Sr) => isa.sru.addresses(word),
        Target::Unit(UnitKind::Simt) => Vec::new(),
        Target::Mover => isa.mover.addresses(word),
    }
}
