//! Where a KohakuTPU unit word carries a memory address.

use crate::layout::W256;
use crate::Isa;
pub use ka_isa::reloc::{AddressField, Segment};
use ktpu_machine::UnitKind;

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
