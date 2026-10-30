//! The rows path (`run._rows`, `runtime/rows.py`, `runtime/tree.py`,
//! TG.RUN.012-.018, API.ROW): the plan's view, each read bound to it, the
//! result placed; a rows kernel over [R, W]; a reduction across column
//! blocks as per-block partials folded; a reduction down the rows as a
//! halving tree; reductions over different range sets materialised one at a
//! time.

mod bind;
mod launch;
mod materialise;
mod place;
mod rewrite;
mod sizing;
mod tree;
mod view;

use std::collections::{BTreeMap, HashMap};
use std::rc::Rc;

use ktpu_kernels::gen::{prepared, relayout};
use ktpu_machine::Refusal;
use serde_json::{json, Value};
use sha2::{Digest as _, Sha256};

use super::pending::{bound, form_error, Bound, Step};
use super::{costs, expr_arg, is_float, item, np_name, Bufs, Operand, Runner};
use crate::exec::{f16, flat, rows_key, View};
use crate::expr::{self, Expr, E};
use crate::lower::{self, hex, Access, Plan, Slot, Table};
use crate::mutant;
use crate::program::{Addr, Call};
use crate::uop::{Arg, Id};
pub use rewrite::{all_reduces, prune, rebuild, reductions_of};
use rewrite::{
    leaf_reduce, permutations, place_head, put, py_operand, renumber, shift, substitute, swap,
};

/// L1 words a rows kernel keeps free beside its operands (`L1_MARGIN`).
const L1_MARGIN: i64 = 16;
/// Bands a rows kernel's trip takes at most (`vector.plan`).
const MAX_BANDS: i64 = 4;
/// Ranges up to which every order of a leaf's temporary is tried.
const ORDER_TRIES: usize = 6;

/// What a padding lane holds so a reduction ignores it.
fn identity(op: &str) -> f64 {
    match op {
        "sum" => 0.0,
        "max" if crate::spec::mutant::on("num.reduce/max-identity-zero") => 0.0,
        "max" => -65504.0,
        _ => 1.0,
    }
}

/// What a padding lane of reduction `op` holds (NUM.STAT.002).
pub fn reduce_identity(op: &str) -> f64 {
    identity(op)
}

/// How two partials of a reduction combine.
fn fold_op(op: &str) -> &'static str {
    match op {
        "sum" => "add",
        "max" => "max",
        _ => "mul",
    }
}

fn ptr(e: &E) -> usize {
    Rc::as_ptr(e) as usize
}

fn row_err<T>(msg: &str) -> Result<T, Refusal> {
    Err(Refusal::new("TG.RUN.014", msg))
}

/// One rows kernel's sizing (`vector.Plan`): column block, blocks, trip
/// rows, trips, rows with the tail.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Sizing {
    pub wc: i64,
    pub nblk: i64,
    pub rows: i64,
    pub nb: i64,
    pub rp: i64,
}

/// The f16 form's storage and element type `_half_target` records after.
pub(super) type Half = Option<(String, String)>;

/// How a placed result is finished (`_place`'s `done`).
enum Done {
    /// the output's padded-rows form written: storage, key, element type
    Form(String, Value, String),
    /// the f16 output written in place
    Direct(String, Plan, Half),
    /// a temporary at strides over the output's ranges moved in
    Moved(String, Plan, Bufs, Vec<(Id, i64)>, Half),
    /// a statistic temporary moved in
    Window(String, Plan, Bufs, Vec<(Id, i64)>, Half),
}
