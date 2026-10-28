//! `card`: nodes, chain, die module, clock profiles, DRAM, the host's map.

use serde::{Deserialize, Deserializer};
use std::collections::BTreeMap;

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Card {
    pub name: String,
    pub nodes: u32,
    pub chain: Vec<u32>,
    /// The die module the card's image is built from.
    pub die: String,
    /// Profile name -> clock domain -> MHz.
    pub profiles: BTreeMap<String, BTreeMap<String, u32>>,
    pub idle_profile: String,
    pub clocks: BTreeMap<String, Clock>,
    pub dram: Dram,
    pub host: Host,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Clock {
    pub index: u32,
    pub nominal_mhz: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Dram {
    pub flat_gb: u32,
    pub interleave_kb: u32,
    /// Usable flat DRAM capacity; flat_gb describes the address decoder span.
    pub bytes: u64,
    /// Xache/DRAM write-line bytes, distinct from the NoC transfer word.
    pub line_bytes: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Host {
    #[serde(deserialize_with = "hex")]
    pub ctrl_base: u64,
    #[serde(deserialize_with = "hex")]
    pub ctrl_stride: u64,
    #[serde(deserialize_with = "hex")]
    pub wizard_base: u64,
    pub vco_mhz: u32,
    pub mem_window_shift: u32,
    pub mem_window_offset: u32,
    pub dram_mesh_shift: u32,
    pub write_granule: u32,
    pub write_replicates: bool,
    pub burst_read: bool,
    pub max_burst_beats: u32,
}

/// A `"0x..."` string (the board file's form) or a plain integer.
fn hex<'de, D: Deserializer<'de>>(d: D) -> Result<u64, D::Error> {
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Raw {
        Int(u64),
        Text(String),
    }
    match Raw::deserialize(d)? {
        Raw::Int(v) => Ok(v),
        Raw::Text(s) => {
            let digits = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X"));
            match digits {
                Some(h) => u64::from_str_radix(h, 16),
                None => s.parse(),
            }
            .map_err(|e| serde::de::Error::custom(format!("{s:?}: {e}")))
        }
    }
}
