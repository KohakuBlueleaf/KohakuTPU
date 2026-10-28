//! The system node: `mover`, `node` (processor, dispatch engine, queues),
//! `il` (interlink).

use serde::Deserialize;

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Mover {
    pub index_words: u32,
    pub max_index: u32,
    pub levels: u32,
    pub burst_max: u32,
    pub fifo_depth: u32,
    /// Whether a move whose read and write sides both step runs of one
    /// 32-byte word is safe (measured, `machines/measured.json`).
    pub one_word_runs: bool,
    pub cfgq_depth: u32,
    pub cfgq_margin: u32,
    pub fault_bits: u32,
    /// Fault codes by their `F_*` name in mm_mover.v.
    pub faults: std::collections::BTreeMap<String, u32>,
    pub row_words: u32,
    pub modes: MoverModes,
    pub xform: Xform,
}

/// The transform slot's occupants (`xform_bank.v`, `mx_quant.v`).
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Xform {
    /// A quantiser entry: lanes of `quant_values` FP16 values in, one
    /// MXFP7 word a lane out.
    pub quant_lanes: u32,
    pub quant_values: u32,
    /// A GT4 group: `gt4_rows` words of `gt4_cols` FP16 values, transposed
    /// as a rows x rows array of `gt4_granule_bits` granules.
    pub gt4_rows: u32,
    pub gt4_cols: u32,
    pub gt4_granule_bits: u32,
}

/// CTRL mode codes (`mm_mover.v` mode decode).
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct MoverModes {
    pub copy: u32,
    pub transpose: u32,
    pub gather: u32,
    pub generate: u32,
    pub fill: u32,
    pub transform: u32,
    pub scatter: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub imem_bytes: u32,
    pub spad_bytes: u32,
    pub engine: Engine,
    pub spad_base: u64,
    pub uncached_bit: u32,
    pub ctrl_bytes: u32,
    pub console_fifo_bytes: u32,
    pub rx_depth: u32,
    pub cq_depth: u32,
    pub ring_depth: u32,
    pub mover_span: u32,
    pub mover_room_writes: u32,
}

/// The dispatch engine.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Engine {
    pub depth: u32,
    pub counters: u32,
    pub counter_bits: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Il {
    pub slots: u32,
    pub meshes: u32,
}
