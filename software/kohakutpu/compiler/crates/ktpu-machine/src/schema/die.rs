//! `die`: units by kind with coordinates, system ports, network, staging.

use super::{Address, Coord, Noc, Runtime, Staging};
use serde::{Deserialize, Serialize};

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

impl Die {
    pub fn stage_bytes(&self) -> Option<u64> {
        self.staging.bytes(self.word_bytes)
    }
}
