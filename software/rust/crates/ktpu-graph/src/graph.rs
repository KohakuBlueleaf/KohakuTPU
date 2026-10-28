//! The traced graph as the Python tracer writes it (`dsl/graph.py`,
//! `docs/spec/stack/dsl/graph.md` DSL.GRAPH): one kernel's signature,
//! scratch buffers, the machine it was traced for and its statement tree.

use serde::{Deserialize, Serialize};

/// The `format` tag and `version` this crate reads.
pub const FORMAT: &str = "ktpu.graph";
pub const FORMAT_VERSION: u32 = 1;

/// One trace (DSL.GRAPH.001).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Graph {
    pub format: String,
    pub version: u32,
    pub name: String,
    pub target: String,
    pub machine: DslMachine,
    /// The constants it was traced with, as plain JSON (not read).
    pub consts: serde_json::Value,
    pub ports: Vec<Port>,
    pub scratch: Vec<Scratch>,
    pub body: Vec<Stmt>,
}

/// What the kernel read of the machine (DSL.KERN.013): clusters and vector
/// cores of one node, and the nodes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DslMachine {
    pub mg: u32,
    pub vc: u32,
    pub nodes: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    In,
    Out,
}

/// A buffer port, in port order.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Port {
    pub name: String,
    pub role: Role,
    pub spec: Spec,
}

/// A scratch buffer (`kt.buffer`), in declaration order.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Scratch {
    pub name: String,
    pub spec: Spec,
}

/// Buffer element types (DSL.SPEC.001, .009, .013).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Dtype {
    F16,
    F32,
    Mx7a,
    Mx7b,
    U8,
    U32,
    I32,
    U64,
}

impl Dtype {
    pub fn name(self) -> &'static str {
        match self {
            Dtype::F16 => "f16",
            Dtype::F32 => "f32",
            Dtype::Mx7a => "mx7a",
            Dtype::Mx7b => "mx7b",
            Dtype::U8 => "u8",
            Dtype::U32 => "u32",
            Dtype::I32 => "i32",
            Dtype::U64 => "u64",
        }
    }
}

/// Buffer spaces (DSL.SPEC.002, .010).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BufSpace {
    Dram,
    Stage,
    Mem,
    Any,
}

impl BufSpace {
    pub fn name(self) -> &'static str {
        match self {
            BufSpace::Dram => "dram",
            BufSpace::Stage => "stage",
            BufSpace::Mem => "mem",
            BufSpace::Any => "any",
        }
    }
}

/// A layout value: `gm=64`, `tile=64x8` (`ints`), or another plain value.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LayoutValue {
    Int(i64),
    Str(String),
    Bool(bool),
    Ints(Vec<i64>),
}

/// A buffer's `TensorSpec` (DSL.SPEC.005, .007): layout pairs sorted by key.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Spec {
    pub dtype: Dtype,
    pub shape: Vec<i64>,
    pub layout: Vec<(String, LayoutValue)>,
    pub space: BufSpace,
}

/// Value spaces (DSL.VAL.001).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ValSpace {
    A,
    B,
    #[serde(rename = "acc")]
    Acc,
    #[serde(rename = "l1")]
    L1,
    #[serde(rename = "reg")]
    Reg,
}

impl ValSpace {
    pub fn name(self) -> &'static str {
        match self {
            ValSpace::A => "A",
            ValSpace::B => "B",
            ValSpace::Acc => "acc",
            ValSpace::L1 => "l1",
            ValSpace::Reg => "reg",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Gt4,
}

/// A value's type (DSL.GRAPH.004).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ValueType {
    pub dtype: Dtype,
    pub shape: Vec<i64>,
    pub space: ValSpace,
    pub mode: Option<Mode>,
}

impl ValueType {
    /// A row statistic: rank 1 in registers (DSL.VAL.001).
    pub fn stat(&self) -> bool {
        self.space == ValSpace::Reg && self.shape.len() == 1
    }
}

/// An index: an int or an affine form `const + sum(coef * atom)`, its terms
/// in the tracer's order (DSL.GRAPH.007, DSL.IDX).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Index {
    Int(i64),
    Sym(Sym),
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Sym {
    #[serde(rename = "const")]
    pub konst: i64,
    pub terms: Vec<(Atom, i64)>,
}

/// A variable, or a floor division / modulo / product the affine form
/// cannot hold (DSL.IDX.007).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Atom {
    Var(String),
    Floor(Box<Index>, Box<Index>),
    Mod(Box<Index>, Box<Index>),
    Mul(Box<Index>, Box<Index>),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum CmpOp {
    #[serde(rename = "==")]
    Eq,
    #[serde(rename = "!=")]
    Ne,
    #[serde(rename = "<")]
    Lt,
    #[serde(rename = "<=")]
    Le,
    #[serde(rename = ">")]
    Gt,
    #[serde(rename = ">=")]
    Ge,
}

impl CmpOp {
    pub fn text(self) -> &'static str {
        match self {
            CmpOp::Eq => "==",
            CmpOp::Ne => "!=",
            CmpOp::Lt => "<",
            CmpOp::Le => "<=",
            CmpOp::Gt => ">",
            CmpOp::Ge => ">=",
        }
    }
}

/// A `kt.when` condition (DSL.IDX.004, .008).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Cond {
    pub op: CmpOp,
    pub a: Index,
    pub b: Index,
}

/// One view entry (DSL.VIEW.001, .007).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Axis {
    All,
    New,
    Run(Index, i64),
    Idx(Index),
}

/// A region of a port or scratch buffer.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct View {
    pub buffer: String,
    pub axes: Vec<Axis>,
}

/// Rows of a value (DSL.VAL.005).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValueView {
    pub value: String,
    pub axes: Vec<Axis>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UnitKind {
    Mg,
    Vc,
    Node,
}

impl UnitKind {
    pub fn name(self) -> &'static str {
        match self {
            UnitKind::Mg => "mg",
            UnitKind::Vc => "vc",
            UnitKind::Node => "node",
        }
    }
}

/// `KIND[index]`, index None for `KIND[any]` (DSL.GRAPH.008).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnitRef {
    pub kind: UnitKind,
    pub index: Option<Index>,
}

/// A float operand: a number, or `inf` / `-inf` / `nan`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Number {
    F(f64),
    Named(String),
}

impl Number {
    pub fn value(&self) -> Option<f64> {
        match self {
            Number::F(v) => Some(*v),
            Number::Named(s) => match s.as_str() {
                "inf" => Some(f64::INFINITY),
                "-inf" => Some(f64::NEG_INFINITY),
                "nan" => Some(f64::NAN),
                _ => None,
            },
        }
    }
}

/// A statement operand (DSL.GRAPH.006).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Operand {
    Value(String),
    /// A statistic broadcast across a row: `NAME[:, *]`.
    Bcast(String),
    Float(Number),
    Vview(ValueView),
    View(View),
    Unit(UnitRef),
}

/// A keyword attribute (DSL.GRAPH.009, DSL.MOV.008).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Attr {
    Int(i64),
    Str(String),
    /// `(count, signed byte stride)` levels, outermost first.
    Walk(Vec<(i64, i64)>),
    Perm(Vec<i64>),
    View(View),
    Index(Index),
}

/// Ops that define a value (DSL.GRAPH.003).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ValueOp {
    // cluster
    Fill,
    Zero,
    Gemm,
    Recv,
    // vector
    Load,
    Unpack,
    Pack,
    Exp2,
    Inv,
    Rsqrt,
    Log2,
    Neg,
    Abs,
    Mov,
    Add,
    Sub,
    Mul,
    Max,
    Min,
    Exp2d,
    Xlane,
    Xrot,
    Fma,
    Fnma,
    Sel,
    Cmplt,
    Cmpgt,
    Cmpeq,
    Rmax,
    Rsum,
}

/// Where a value op runs (DSL.SCOPE.001, .006).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Runs {
    Cluster,
    Core,
    /// `recv`: a core or a cluster.
    Unit,
}

impl ValueOp {
    pub fn name(self) -> &'static str {
        match self {
            ValueOp::Fill => "fill",
            ValueOp::Zero => "zero",
            ValueOp::Gemm => "gemm",
            ValueOp::Recv => "recv",
            ValueOp::Load => "load",
            ValueOp::Unpack => "unpack",
            ValueOp::Pack => "pack",
            ValueOp::Exp2 => "exp2",
            ValueOp::Inv => "inv",
            ValueOp::Rsqrt => "rsqrt",
            ValueOp::Log2 => "log2",
            ValueOp::Neg => "neg",
            ValueOp::Abs => "abs",
            ValueOp::Mov => "mov",
            ValueOp::Add => "add",
            ValueOp::Sub => "sub",
            ValueOp::Mul => "mul",
            ValueOp::Max => "max",
            ValueOp::Min => "min",
            ValueOp::Exp2d => "exp2d",
            ValueOp::Xlane => "xlane",
            ValueOp::Xrot => "xrot",
            ValueOp::Fma => "fma",
            ValueOp::Fnma => "fnma",
            ValueOp::Sel => "sel",
            ValueOp::Cmplt => "cmplt",
            ValueOp::Cmpgt => "cmpgt",
            ValueOp::Cmpeq => "cmpeq",
            ValueOp::Rmax => "rmax",
            ValueOp::Rsum => "rsum",
        }
    }

    pub fn runs(self) -> Runs {
        match self {
            ValueOp::Fill | ValueOp::Zero | ValueOp::Gemm => Runs::Cluster,
            ValueOp::Recv => Runs::Unit,
            _ => Runs::Core,
        }
    }
}

/// Node-scope mover ops (DSL.MOV).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MoverOp {
    Copy,
    Quant,
    Gt4,
    Fill,
    Gen,
    Gather,
    Scatter,
    #[serde(rename = "gemm_tiles")]
    GemmTiles,
}

impl MoverOp {
    pub fn name(self) -> &'static str {
        match self {
            MoverOp::Copy => "copy",
            MoverOp::Quant => "quant",
            MoverOp::Gt4 => "gt4",
            MoverOp::Fill => "fill",
            MoverOp::Gen => "gen",
            MoverOp::Gather => "gather",
            MoverOp::Scatter => "scatter",
            MoverOp::GemmTiles => "gemm_tiles",
        }
    }
}

/// One statement and the author's `file:line` (DSL.GRAPH.002, .010).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Stmt {
    #[serde(flatten)]
    pub node: Node,
    #[serde(rename = "where")]
    pub at: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Node {
    /// `par VAR in LO..HI on UNIT` (DSL.CTRL.001).
    Par {
        var: String,
        lo: Index,
        hi: Index,
        unit: UnitRef,
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        classed: bool,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        class_band: Option<i64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        repeats: Option<i64>,
        #[serde(default, skip_serializing_if = "Vec::is_empty")]
        advances: Vec<i64>,
        body: Vec<Stmt>,
    },
    /// `for VAR in LO..HI [pipe=K]` (DSL.CTRL.004).
    For {
        var: String,
        lo: Index,
        hi: Index,
        pipe: Option<i64>,
        body: Vec<Stmt>,
    },
    /// `when COND` (DSL.CTRL.006).
    When { cond: Cond, body: Vec<Stmt> },
    /// `if sticky` (DSL.CTRL.007).
    Sticky { body: Vec<Stmt> },
    /// `TARGET = OP ARGS [k=v] : TYPE`.
    Value {
        op: ValueOp,
        target: String,
        #[serde(rename = "type")]
        ty: ValueType,
        args: Vec<Operand>,
        kw: Vec<(String, Attr)>,
    },
    /// `store VIEW <- SRC` (DSL.VEC.017).
    Store { dst: View, src: String },
    /// `drain SRC -> DST [add] [node=N]` (DSL.CL.004, .008, DSL.NODE.004).
    Drain {
        src: String,
        dst: Operand,
        add: bool,
        node: Option<Index>,
    },
    /// `send SRC -> UNIT side=S` (DSL.VEC.020).
    Send {
        src: String,
        dst: UnitRef,
        side: String,
    },
    /// `OP DST [<- SRC] [FLAGS] [k=v]` (DSL.MOV).
    Mover {
        op: MoverOp,
        dst: View,
        src: Option<View>,
        flags: Vec<String>,
        kw: Vec<(String, Attr)>,
    },
}

impl Node {
    /// The block a control statement holds.
    pub fn body(&self) -> Option<&[Stmt]> {
        match self {
            Node::Par { body, .. }
            | Node::For { body, .. }
            | Node::When { body, .. }
            | Node::Sticky { body } => Some(body),
            _ => None,
        }
    }
}
