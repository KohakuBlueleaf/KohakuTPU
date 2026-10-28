//! The planner's output: a step program the driver runs in order, every
//! address symbolic (`{buf, form, offset}`), and the plan of a kernel ast.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::input::{Buffer, FormKey, State, Uops};

/// An address: `offset` bytes into form `form` of storage `buf` (a slot's
/// base `bufN`, a temporary `tmpN`, a table `table:KEY`).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Addr {
    pub buf: String,
    pub form: FormKey,
    pub offset: i64,
}

/// An array by element type, shape and the sha256 of its bytes.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Array {
    pub dtype: String,
    pub shape: Vec<i64>,
    pub sha256: String,
}

/// A generator call: `{gen: "vector.rows_kernel", args: {...}}` (the
/// generators' own encoding, ktpu-kernels).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Call {
    pub gen: String,
    pub args: Value,
}

/// One step.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Step {
    /// A temporary of `nbytes`, or (`alias`) a storage over a form's bytes.
    Alloc {
        buf: String,
        nbytes: i64,
        alias: Option<Addr>,
    },
    Free {
        buf: String,
    },
    /// A side form allocated (its contents not valid).
    Form {
        buf: String,
        form: FormKey,
        nbytes: i64,
    },
    /// A constant uploaded once by content, at `at`.
    Table {
        at: Addr,
        array: Array,
    },
    /// Host bytes written at `at`.
    Write {
        at: Addr,
        bytes: i64,
        sha256: String,
    },
    /// `n` values of `buf`'s host bytes (`dtype`) cast to f16 at `at`.
    Cast {
        at: Addr,
        buf: String,
        dtype: String,
        n: i64,
    },
    Launch(Box<Launch>),
    /// A kernel wrote form `form`: it alone is valid.
    Wrote {
        buf: String,
        form: FormKey,
        keep_host: bool,
    },
    /// Form `form` was made from a valid one.
    Made {
        buf: String,
        form: FormKey,
    },
}

/// A kernel run: the generator call that made it, its name, run kind and
/// info, its ports' addresses, its graph.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Launch {
    pub call: Option<Call>,
    pub name: String,
    pub kind: String,
    pub info: Value,
    pub consts: Value,
    pub binds: BTreeMap<String, Addr>,
    /// the kernel's graph
    pub graph: Option<ktpu_graph::Graph>,
}

impl Launch {
    /// The kernel of its generator call (`ktpu_kernels::call`): its name
    /// and graph filled in.
    pub fn compile(&mut self, m: &ktpu_machine::Machine) -> Result<(), ktpu_machine::Refusal> {
        if self.graph.is_some() {
            return Ok(());
        }
        let Some(call) = &self.call else {
            return Ok(());
        };
        let k = ktpu_kernels::call(&call.gen, &call.args, m)?;
        self.name = k.graph.name.clone();
        self.graph = Some(k.graph);
        Ok(())
    }
}

/// Every launch of `steps` compiled ([`Launch::compile`]).
pub fn compile(steps: &mut [Step], m: &ktpu_machine::Machine) -> Result<(), ktpu_machine::Refusal> {
    for s in steps {
        if let Step::Launch(l) = s {
            l.compile(m)?;
        }
    }
    Ok(())
}

/// A loop range: tinygrad's range id, its extent, its axis type.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Range {
    pub range: i64,
    pub extent: i64,
    pub axis: String,
}

/// One read: a param slot (or a temporary's key, or null for a folded
/// table), its element type, its stride on each range, its offset.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Access {
    pub slot: Value,
    pub dtype: String,
    pub coef: Vec<(Range, i64)>,
    pub off: i64,
    pub table: Option<Array>,
    pub gate: bool,
}

/// An expression as a node table (`emit.expr`), a node reached twice once.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Expr {
    pub nodes: Vec<ExprNode>,
    pub root: usize,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum ExprNode {
    Load { load: usize },
    Const { r#const: Value },
    Op { op: String, args: Vec<usize> },
    Reduce { reduce: String, arg: usize },
}

/// The plan of one kernel ast (TG.PLAN): its kind, output, expression and
/// reads; a product's A, B, M / N / K and batch ranges.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Plan {
    pub kind: String,
    pub name: String,
    pub out_slot: i64,
    pub out_dtype: String,
    pub out_shape: Vec<i64>,
    pub out_ranges: Vec<Range>,
    pub expr: Option<Expr>,
    pub accesses: Vec<Access>,
    pub reduce_ranges: Vec<Range>,
    /// each reduction's ranges, by its expression node
    pub reductions: Vec<(usize, Vec<Range>)>,
    pub value: Option<Array>,
    pub a: Option<Access>,
    pub b: Option<Access>,
    pub out_strides: Option<Vec<i64>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mnk: Option<(Range, Range, Vec<Range>)>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub batch: Option<Vec<Range>>,
}

/// A lowered ast: its plan, or the refusal.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Lowered {
    pub uops: Uops,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub plan: Option<Plan>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refused: Option<String>,
    /// the Python exception class it was refused with
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refused_class: Option<String>,
}

/// `rows.run` called directly (`plan_dump.SYNTHETIC`): its arguments as
/// the generators encode them, the steps it took, its error.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RowsCall {
    pub args: Value,
    pub steps: Vec<Step>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// One plan run: the plan (index into the case's), the state before, the
/// steps, the bases' records after, the error that ended it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Run {
    pub plan: usize,
    pub state: State,
    pub steps: Vec<Step>,
    pub end: BTreeMap<String, Buffer>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// One `kp` entry point called (the op log, `ktpu.tensor`): the op
/// (`tensor`, `ew`, `reduce`, `matmul`, `T`, `reshape`, `realize`, `numpy`,
/// `set_fusion`), its arguments (a tensor by name `tN`, a number, an
/// uploaded array by digest), the steps it took, the tensor it made.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct KpEntry {
    pub op: String,
    pub args: Vec<Value>,
    pub kwargs: BTreeMap<String, Value>,
    pub steps: Vec<Step>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub out: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shape: Option<Vec<i64>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stat: Option<bool>,
    /// a realised result's storage
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub buf: Option<String>,
    /// `numpy()`'s array
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub read: Option<Array>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// A golden case (`software/rust/golden/plan/*.json`).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Case {
    pub case: String,
    pub from: String,
    /// the machine file planned for, as a card of `nodes` nodes
    pub machine: String,
    pub nodes: u32,
    /// the planner paths taken (`plan_dump.PATHS`)
    pub paths: Vec<String>,
    pub plans: Vec<Lowered>,
    pub runs: Vec<Run>,
    /// each outermost tinygrad `Tensor.realize`: the number of runs before it
    pub realized: Vec<usize>,
    pub kp: Vec<KpEntry>,
    pub rows: Vec<RowsCall>,
    pub error: Option<String>,
}

impl Case {
    /// The machine the case planned for.
    pub fn machine(&self) -> ktpu_machine::Machine {
        ktpu_machine::Machine::named(&self.machine)
            .and_then(|m| m.with_nodes(self.nodes))
            .expect("a machine file and node count")
    }
}
