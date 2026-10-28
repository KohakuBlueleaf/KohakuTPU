//! What a compile returns.

use crate::pipeline::stage::Stage;
use ktpu_machine::Refusal;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A compile's input: the tracer's graph (JSON), L2 text, or L1 text (a
/// hand-written body, built as written: no passes, no image scheduler).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Input {
    Graph(String),
    L2(String),
    L1(String),
}

impl Input {
    /// Graph JSON (an object, first non-blank character `{`) or L2 text.
    pub fn detect(text: &str) -> Input {
        if text.trim_start().starts_with('{') {
            Input::Graph(text.to_string())
        } else {
            Input::L2(text.to_string())
        }
    }
}

/// One node's package as the pipeline returns it: relocatable bytes and the
/// fn parameter each of its buffers binds, in buffer order.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PackageOut {
    #[serde(with = "hex_bytes")]
    pub bytes: Vec<u8>,
    pub params: Vec<String>,
    #[serde(default)]
    pub images: serde_json::Value,
}

/// One arena: the scratch parameters of a memory space (and node) in one
/// block of `bytes`, each at its offset; the packages name its buffer
/// `scratch:<space>[:<node>]`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Scratch {
    pub space: String,
    pub node: Option<i64>,
    pub bytes: u64,
    pub members: Vec<(String, u64)>,
}

/// One static-checker finding.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Finding {
    pub kind: String,
    pub clause: String,
    pub at: String,
    pub text: String,
}

/// The static checker's verdict: refused when a finding is of a refused kind.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Verdict {
    pub refused: bool,
    pub findings: Vec<Finding>,
}

/// One kernel through the stack.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Compiled {
    /// The kernel (the L2 fn) compiled.
    pub name: String,
    /// L2 as given or traced, before the passes.
    pub l2: String,
    /// L1 after the image scheduler.
    pub l1: String,
    /// Each node's package (key: mesh); empty when `bound` is set.
    pub packages: BTreeMap<u32, PackageOut>,
    /// The arenas the packages hold scratch in, one block each to allocate.
    pub scratch: Vec<Scratch>,
    pub verdict: Verdict,
    /// Why the build is not relocatable (STACK.RELOC's message: a word that
    /// does not follow its buffer, as past L0.BIND.005's 256 buffers), when
    /// it is not: the card's packages are then built at each binding.
    pub bound: Option<String>,
}

/// A refusal and the stage that made it.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
#[error("{}: {refusal}", stage.name())]
pub struct Refused {
    pub stage: Stage,
    pub refusal: Refusal,
}

mod hex_bytes {
    use serde::{Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(v: &[u8], s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&crate::pipeline::cache::hex(v))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<u8>, D::Error> {
        let s = String::deserialize(d)?;
        crate::pipeline::cache::unhex(&s)
            .ok_or_else(|| serde::de::Error::custom("not an even-length hex string"))
    }
}
