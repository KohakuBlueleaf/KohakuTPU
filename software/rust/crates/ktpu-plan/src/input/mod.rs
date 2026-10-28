//! What the host hands the planner (`kohakutpu.application.plan.emit`):
//! a tinygrad kernel ast (`ktpu.uops`) and the bound buffers' records
//! (`ktpu.state`); the machine's unit facts (`hw`).

pub mod hw;

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const UOPS: &str = "ktpu.uops";
pub const STATE: &str = "ktpu.state";
pub const VERSION: u32 = 1;

/// One kernel ast: UOps in topological order, sources by index.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Uops {
    pub format: String,
    pub version: u32,
    pub nodes: Vec<Uop>,
    pub root: usize,
}

/// `{op, dtype, src, arg}`: the op's name (`RANGE`), the dtype's repr
/// without `dtypes.` (`half`, `weakint`), the argument as plain JSON
/// (`emit.plain`: `{"enum"}`, `{"dtype"}`, `{"type", "fields"}`, lists).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Uop {
    pub op: String,
    pub dtype: String,
    pub src: Vec<usize>,
    pub arg: Value,
}

/// A storage form key: `["flat"]`, `["f16"]`, `["rows", n, np]`, `["mx7",
/// side, OPERAND, rows, cols, rp, cp, g, nk, batches]`, held as its JSON.
pub type FormKey = Value;

/// The bound buffers before a plan runs.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct State {
    pub format: String,
    pub version: u32,
    /// param slot (its text) -> where it is
    pub slots: BTreeMap<String, Slot>,
    /// base name -> its record
    pub buffers: BTreeMap<String, Buffer>,
}

/// A slot: its base storage's name, byte offset and size (a sub-buffer).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Slot {
    pub base: String,
    pub offset: i64,
    pub nbytes: i64,
}

/// A base storage's record: row-major size, the element type a kernel last
/// wrote it as, its valid forms, its allocated side forms, its host bytes.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Buffer {
    pub nbytes: i64,
    pub dtype: Option<String>,
    pub valid: Vec<FormKey>,
    pub forms: Vec<(FormKey, i64)>,
    pub host: Option<Digest>,
}

/// Bytes by length and sha256.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Digest {
    pub bytes: i64,
    pub sha256: String,
}
