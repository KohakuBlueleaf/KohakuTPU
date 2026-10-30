//! `fw`: the node firmware image, its limits and its formats.

use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Fw {
    pub image: String,
    /// Firmware parts built into the image (`kohakuaccel/dispatch`, ...).
    pub parts: Vec<String>,
    /// Unit classes the image drives (`matmul`, `vector`, ...).
    pub unit_classes: Vec<String>,
    pub memory: FwMemory,
    /// Section name -> bytes the build uses.
    pub sections: BTreeMap<String, u64>,
    pub max_units: u32,
    pub max_pkg_units: u32,
    pub engine_units: u32,
    pub fetch_depth: u32,
    pub max_buffers: u32,
    pub enum_wait: u32,
    pub heap: Heap,
    pub boot: Boot,
    pub debug: Debug,
    pub queue: Queue,
    pub package: Package,
    /// `KA_ST_*` name -> code.
    pub status: BTreeMap<String, u32>,
    /// `KA_WAIT_*` name -> code.
    pub waits: BTreeMap<String, u32>,
    /// `KA_SIG_*` name -> code.
    pub signals: BTreeMap<String, u32>,
}

impl Fw {
    /// Whether the image carries firmware part `part` (`"kohakuaccel/dispatch"`).
    pub fn has_part(&self, part: &str) -> bool {
        self.parts.iter().any(|p| p == part)
    }

    /// Whether the image drives unit class `class` (`"vector"`).
    pub fn has_unit_class(&self, class: &str) -> bool {
        self.unit_classes.iter().any(|c| c == class)
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct FwMemory {
    pub imem_bytes: u32,
    pub spad_bytes: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Heap {
    pub regions: u32,
    pub blocks: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Boot {
    pub version: u32,
    /// `KA_BOOT_F_*` name (without the prefix) -> bit.
    pub flags: BTreeMap<String, u32>,
    /// Entries of the boot block's region table (RUL.024).
    pub regions: u32,
    /// `KA_REGION_*` name (without the prefix) -> use code.
    pub region_uses: BTreeMap<String, u32>,
}

/// The debug image (RUL.073): its name, the features it carries
/// (`KA_DEBUG_F_*` name -> bit) and the completion words it keeps per unit.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Debug {
    pub image: String,
    pub features: BTreeMap<String, u32>,
    pub last: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Queue {
    pub magic: u64,
    pub min_version: u32,
    pub version: u32,
    pub list_version: u32,
    pub line: u32,
    pub sq_bytes: u32,
    pub cq_bytes: u32,
    pub sq_words: u32,
    pub nbind_shift: u32,
    pub stdio_mask: u64,
    pub op_mask: u32,
    pub cq_status_mask: u32,
    pub offsets: BTreeMap<String, u32>,
    pub cq_offsets: BTreeMap<String, u32>,
    pub info_words: BTreeMap<String, u32>,
    pub fw_words: BTreeMap<String, u32>,
    pub states: BTreeMap<String, u32>,
    pub opcodes: BTreeMap<String, u32>,
    pub run_words: BTreeMap<String, u32>,
    pub list_words: BTreeMap<String, u32>,
    pub status: BTreeMap<String, u32>,
    pub record: QueueRecord,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct QueueRecord {
    pub version: u32,
    pub bytes: u32,
    pub word: u32,
    pub fields: BTreeMap<String, RecordField>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RecordField {
    pub offset: u32,
    pub width: u32,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Package {
    pub magic: u32,
    pub version: u32,
    pub header_bytes: u32,
    pub regs: u32,
    pub copy_bytes: u32,
    pub unbounded: u64,
}
