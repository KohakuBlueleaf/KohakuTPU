//! Where a unit word carries a memory address: the fields a package relocates.

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
