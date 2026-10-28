//! The tensor API's planner (`api/tensor.py`, API.TEN / OP / FUSE / READ):
//! ops build lazy nodes; realising one runs one generated kernel (rows, a
//! row statistic, or a contraction with its epilogue); with fusion on, an
//! elementwise chain, the statistics it reads within one column block and a
//! product's epilogue fold into that kernel. A [`Session`] replays a `kp` op
//! log, each op's steps recorded as the Python planner takes them.

mod lazy;
mod ops;
mod realize;
mod replay;

use std::collections::{BTreeMap, HashMap};
use std::rc::Rc;

use ktpu_machine::{Machine, Refusal, UnitKind};
use serde_json::{json, Value};

use crate::exec::{flat, Exec, View};
use crate::expr::{self, Expr as X, E};
use crate::input::Digest;
use crate::program::{Addr, Call, Step};
use crate::run::matmul::Mm;
use crate::run::{expr_arg, Operand, Runner};
use crate::uop::Graph;
use lazy::{broadcast, count, leaves, lower, prod_of, reduces};
pub use replay::Replayed;

/// A lazy expression over reads of realised tensors (`Ref`) and at most one
/// product (`Prod`); nodes shared by `Rc` as Python shares objects.
#[derive(Debug)]
pub enum K {
    Ref(usize),
    Prod(usize, usize),
    Const(f64),
    Op(String, Vec<Rc<K>>),
    Reduce(String, Rc<K>),
}

type KE = Rc<K>;

/// What a lazy tensor computes, on the rows view its reads classify against.
#[derive(Clone, Debug)]
pub struct Node {
    pub expr: KE,
    pub view: Vec<i64>,
}

/// A tensor: realised (a storage and its element type) or lazy (a node).
#[derive(Clone, Debug)]
pub struct Tensor {
    pub shape: Vec<i64>,
    pub buf: Option<(String, String)>,
    pub node: Option<Node>,
    pub stat: bool,
    /// the [N, K] tensor this is the transpose view of
    pub t_of: Option<usize>,
}

impl Tensor {
    fn size(&self) -> i64 {
        self.shape.iter().product()
    }

    /// The [.., W] view a kernel reads: a statistic's ends in 1.
    fn rows(&self) -> Vec<i64> {
        rows_view(&self.shape, self.stat)
    }
}

fn rows_view(shape: &[i64], stat: bool) -> Vec<i64> {
    if stat && shape.last() != Some(&1) {
        let mut s = shape.to_vec();
        s.push(1);
        return s;
    }
    if shape.is_empty() {
        vec![1]
    } else {
        shape.to_vec()
    }
}

/// `rows.rows_of`: rows of a [.., W] view.
fn rows_of(shape: &[i64]) -> i64 {
    if shape.len() > 1 {
        shape[..shape.len() - 1].iter().product()
    } else {
        1
    }
}

/// Python's repr of a tuple of ints.
pub fn py_tuple(s: &[i64]) -> String {
    match s {
        [] => "()".into(),
        [a] => format!("({a},)"),
        _ => format!(
            "({})",
            s.iter()
                .map(|d| d.to_string())
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn unsupported<T>(clause: &str, msg: impl Into<String>) -> Result<T, Refusal> {
    Err(Refusal::new(clause, msg))
}

/// The tinygrad name of a numpy element type the runner takes.
fn tg_dtype(np: &str) -> &'static str {
    match np {
        "float32" => "float",
        _ => "half",
    }
}

/// An operand of an elementwise op: a tensor or a number.
#[derive(Clone, Copy, Debug)]
pub enum Arg {
    T(usize),
    N(f64),
}

/// The planner of one `kp` session: the machine, every tensor made, the
/// storages and steps.
pub struct Session<'a> {
    pub r: Runner<'a>,
    pub ts: Vec<Tensor>,
    pub fuse: bool,
}
