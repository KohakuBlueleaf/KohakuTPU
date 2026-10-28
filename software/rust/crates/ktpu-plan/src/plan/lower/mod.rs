//! One tinygrad kernel ast as a plan for this machine, or a refusal by name
//! (`ktpugrad/lower.py`, TG.PLAN): `const` (reads no buffer: folded and
//! written from the host), `copy` (one read stored at its own index),
//! `matmul` (one sum of a product of A [.., M, K] and B [.., N, K] under an
//! elementwise epilogue) or `vector` (an expression over reads).

mod json;
mod kernel;
mod matmul;
mod reader;
mod unfold;
mod uops;

use std::collections::HashMap;
use std::rc::Rc;

use ktpu_machine::Refusal;
use sha2::{Digest, Sha256};

use crate::domain;
use crate::expr::{self, Expr, E};
use crate::fold::{self, Arr, Ty};
use crate::input::Uops;
use crate::mutant;
use crate::program::{self, Array};
use crate::uop::{Arg, Graph, Id};
pub use json::{arr_digest, plan_json};
use kernel::param_slot;
pub use kernel::plan;
use matmul::matmul;
pub use matmul::mnk;
pub use reader::Reader;
pub use unfold::build;
use unfold::unfold;
pub use uops::{affine, extent, gated, param_of};
use uops::{const_int, envelope, range_id, summarise};

/// Where a read reads: a param slot, a folded table, a runner temporary.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Slot {
    Param(i64),
    Table,
    Temp(String),
}

/// A folded f16 table: its shape and values (f16 bits).
#[derive(Clone, Debug, PartialEq)]
pub struct Table {
    pub shape: Vec<usize>,
    pub bits: Vec<u16>,
}

impl Table {
    pub fn digest(&self) -> Array {
        let mut h = Sha256::new();
        for b in &self.bits {
            h.update(b.to_le_bytes());
        }
        Array {
            dtype: "float16".into(),
            shape: self.shape.iter().map(|d| *d as i64).collect(),
            sha256: hex(&h.finalize()),
        }
    }
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// One read (`Access`): its slot, element type, stride on each range (the
/// order the index walk gave), element offset; a table's values; a gate.
#[derive(Clone, Debug, PartialEq)]
pub struct Access {
    pub slot: Slot,
    pub dtype: String,
    pub coef: Vec<(Id, i64)>,
    pub off: i64,
    pub table: Option<Table>,
    pub gate: bool,
}

impl Access {
    pub fn c(&self, r: Id) -> i64 {
        self.coef
            .iter()
            .find(|(x, _)| *x == r)
            .map(|(_, c)| *c)
            .unwrap_or(0)
    }
}

/// A plan (`lower.Plan`).
#[derive(Clone, Debug)]
pub struct Plan {
    pub kind: String,
    pub name: String,
    pub out_slot: Slot,
    pub out_dtype: String,
    pub out_shape: Vec<i64>,
    pub out_ranges: Vec<Id>,
    pub expr: Option<E>,
    pub accesses: Vec<Access>,
    pub reduce_ranges: Vec<Id>,
    /// each reduction's ranges, by its node
    pub reductions: Vec<(E, Vec<Id>)>,
    pub value: Option<Arr>,
    pub a: Option<Access>,
    pub b: Option<Access>,
    pub mnk: Option<(Id, Id, Vec<Id>)>,
    pub batch: Vec<Id>,
    pub out_strides: Option<Vec<i64>>,
}

/// The ast and its graph, planned.
pub struct Lowered {
    pub graph: Graph,
    pub plan: Plan,
}

fn refuse<T>(clause: &str, msg: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal::new(clause, msg))
}
