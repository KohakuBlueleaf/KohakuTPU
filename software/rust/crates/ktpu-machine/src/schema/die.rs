//! `die`: units by kind with coordinates, system ports, network widths,
//! staging; and `unit`, the depths every unit shares.

use super::Runtime;
use serde::{Deserialize, Serialize};
use std::fmt;

/// A unit coordinate `(x, y)` in its mesh, as the file lists it (`[x, y]`).
#[derive(Clone, Copy, Debug, Default, Deserialize, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Coord(pub u32, pub u32);

impl Coord {
    pub fn x(self) -> u32 {
        self.0
    }
    pub fn y(self) -> u32 {
        self.1
    }
}

impl fmt::Display for Coord {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "({}, {})", self.0, self.1)
    }
}

impl serde::Serialize for Coord {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        (self.0, self.1).serialize(s)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Die {
    /// The IR target the die's programs are written for (the `target` line
    /// of L2 / L1 text, RUL.010).
    pub target: String,
    pub units: Units,
    pub sys_ports: Vec<Coord>,
    pub fetch_port: Coord,
    pub noc: Noc,
    pub word_bytes: u32,
    pub staging: Staging,
    pub address: Runtime<Address>,
}

/// Every unit of each kind, in unit-index order (`mg[J]` is `mg[J]`).
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Units {
    pub mg: Vec<Coord>,
    pub vc: Vec<Coord>,
    pub sr: Vec<Coord>,
    pub simt: Vec<Coord>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Noc {
    pub flit_bits: Runtime<u32>,
    pub data_bits: u32,
    pub addr_bits: u32,
    pub pos_bits: u32,
    pub router_depth: Runtime<u32>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Staging {
    pub entry_words: u32,
    pub aperture: Runtime<u32>,
    pub implemented: Runtime<u64>,
    pub banks: u32,
    pub entries: u32,
    pub flits: Runtime<u32>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Address {
    pub special_bit: u32,
    pub reserved_bit: u32,
    pub mesh_shift: u32,
    pub mesh_bits: u32,
    pub aperture_shift: u32,
    pub aperture_bits: u32,
}

impl Staging {
    pub fn bytes(&self, word_bytes: u32) -> Option<u64> {
        u64::from(self.entries)
            .checked_mul(u64::from(self.entry_words))?
            .checked_mul(u64::from(word_bytes))
    }
}

impl Die {
    pub fn stage_bytes(&self) -> Option<u64> {
        self.staging.bytes(self.word_bytes)
    }
}

/// `unit`: instruction and receive FIFO depths, the CU_DATA burst limit.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct UnitDepths {
    pub inst_depth: u32,
    pub recv_depth: Runtime<u32>,
    pub burst_words: u32,
}
