//! The unit kinds' profiles: `mg` (matmul cluster), `vc` (vector core),
//! `sr` (Streaming Row Unit), `simt`.

use serde::Deserialize;

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Mg {
    pub count: u32,
    pub l1_a_entries: u32,
    pub l1_b_entries: u32,
    pub l1_bank_entries: u32,
    pub tiles: u32,
    pub dq_depth: u32,
    /// Drain-queue sub-tiles held before a sweep waits (`DQ_LIMIT`).
    pub dq_limit: u32,
    pub acc_mantissa_bits: u32,
    /// Bits of one accumulator element, as a peer sub-tile carries it.
    pub acc_bits: u32,
    pub shared_b_max: u32,
    pub lanes: u32,
    pub kblock: u32,
    pub entry_bits: u32,
    pub kstep: u32,
    pub max_fill: u32,
    pub max_peers: u32,
    pub reuse_min: u32,
    pub wburst: u32,
    /// MACs a cycle: `lanes^2 x kblock x kstep` (RUL.007).
    pub macs_per_cycle: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Vc {
    pub count: u32,
    pub build: String,
    pub regs: u32,
    pub rf_copies: u32,
    /// Register-file banks (a chunk's bank is its index mod this).
    pub rf_banks: u32,
    pub vlmax: u32,
    pub lanes: u32,
    pub chunks: u32,
    pub l1_words: u32,
    pub l1_banks: u32,
    pub imem_words: u32,
    pub run_reach: u32,
    pub queue_depth: u32,
    pub dma_queue_depth: u32,
    /// Fills the completion queue holds taken and unretired (ISA.VC.193).
    pub fill_track_depth: u32,
    pub dualq: u32,
    pub has_mx: u32,
    pub xnet: u32,
    pub tau: u32,
    pub sregs: u32,
    pub kregs: u32,
    pub preds: u32,
    pub marks: u32,
    pub sig_bits: u32,
    pub descriptors: u32,
    pub descriptor_dims: u32,
    pub ainc_bits: u32,
    pub walk_max: u32,
    pub crossbar_modes: Vec<String>,
    pub pack_modes: Vec<String>,
    /// (cycles before the first word, cycles a word) of a fill alone.
    pub fill_alone: (u32, u32),
    /// The same beside a four-cluster matmul.
    pub fill_shared: (u32, u32),
    /// The fault codes the core build raises (RUL.053).
    pub faults: Vec<u32>,
    /// The raised fault codes by their `F_*` name in v2_core.v.
    pub fault_names: std::collections::BTreeMap<String, u32>,
    pub timing: VcTiming,
}

/// The vector core's tier-2 measured timing facts (RUL.063).
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct VcTiming {
    /// A math result reaches a dependent math op this long after the
    /// producer's last beat.
    pub math_math: u32,
    /// Extra when the dependent reads it through the crossbar.
    pub xbar: u32,
    /// An unpacked register reaches math this long after the unpack's last word.
    pub unpack_math: u32,
    /// A math result reaches a pack this long after its last beat.
    pub math_pack: u32,
    /// Front-end cycles of an instruction that evaluates a condition.
    pub cond: u32,
    /// SETI cycles, including the immediate word.
    pub seti: u32,
    /// Inter-instruction cycles beyond MX7 active reads.
    pub mx7_issue_gap: u32,
    /// Words an MX7 pack moves (its occupancy).
    pub mx7_pack_words: u32,
}

/// `sr`: every field but `count` is null on a die without SR units.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Sr {
    pub count: u32,
    pub profile: Option<SrProfile>,
    pub buf_words: Option<u32>,
    pub fifo_depth: Option<u32>,
    pub op_depth: Option<u32>,
    pub fin_depth: Option<u32>,
    pub npass: Option<u32>,
    pub rows: Option<u32>,
    pub ldep: Option<u32>,
    pub wlead: Option<u32>,
    pub taps: Option<u32>,
    /// Words of an output burst.
    pub burst: Option<u32>,
    /// Sub-rows held past the output that has left (the buffer's halves).
    pub held: Option<u32>,
    /// The FP16 form the die's SR units are built with: 1 IEEE, 0 the
    /// clusters' accumulator form (RUL.012).
    pub f16_ieee: Option<u32>,
}

/// The SRU build knobs (`sru_cu.v` parameters).
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SrProfile {
    #[serde(rename = "F_LANES")]
    pub f_lanes: u32,
    #[serde(rename = "H_TAPS")]
    pub h_taps: u32,
    #[serde(rename = "HAS_FN")]
    pub has_fn: u32,
    /// `ram` (loadable table) or `rom` (exp2 only).
    #[serde(rename = "TAB")]
    pub tab: String,
    #[serde(rename = "HAS_MANT")]
    pub has_mant: u32,
    #[serde(rename = "FIN_RCP")]
    pub fin_rcp: u32,
    #[serde(rename = "F16_IEEE")]
    pub f16_ieee: u32,
    /// MXFP7 output (RUL.013).
    #[serde(rename = "HAS_MX7")]
    pub has_mx7: u32,
}

/// The SRU facts of a die that has SR units, every one present.
#[derive(Clone, Debug, PartialEq)]
pub struct SrFacts {
    pub count: u32,
    pub profile: SrProfile,
    pub buf_words: u32,
    pub fifo_depth: u32,
    pub op_depth: u32,
    pub fin_depth: u32,
    pub npass: u32,
    pub rows: u32,
    pub ldep: u32,
    pub wlead: u32,
    pub taps: u32,
    pub f16_ieee: u32,
}

impl Sr {
    /// Every SRU fact, or None on a die without SR units.
    pub fn facts(&self) -> Option<SrFacts> {
        Some(SrFacts {
            count: self.count,
            profile: self.profile.clone()?,
            buf_words: self.buf_words?,
            fifo_depth: self.fifo_depth?,
            op_depth: self.op_depth?,
            fin_depth: self.fin_depth?,
            npass: self.npass?,
            rows: self.rows?,
            ldep: self.ldep?,
            wlead: self.wlead?,
            taps: self.taps?,
            f16_ieee: self.f16_ieee?,
        })
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Simt {
    pub count: u32,
    /// Float lanes and seed-capable float units of the build, 0 on a die
    /// without SIMT units (RUL.015).
    pub flanes: u32,
    pub fsfu_units: u32,
}
