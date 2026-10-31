//! The L2 typed IR (L2.BODY, L2.SPACE): an `fn NAME.l2` body read to a tree
//! of `par` / `for` / `when` / `if` blocks over ops, each value typed
//! `DTYPE[shape] @SPACE[(MODE)]`, every memory buffer's layout read.
//!
//! [`Program`] is what the passes take and give: a `.ktpu` module holding
//! the L2 fn (its text is the spec's serialisation). [`Body`] is the tree a
//! program's fn reads to; [`instances`] walks it with every `par` and `for`
//! variable bound.

pub mod buffer;
pub mod read;
pub mod repr;

use std::fmt;
use std::sync::Arc;

use indexmap::IndexMap;
use ktpu_l1::text::{Module, Reader, Stmt, Term};

pub use buffer::{Buffer, LayoutVal};
pub use read::{const_of, instances, read, span, value, Instance, Item, Stage, StageX};

/// A refusal: the clause id, the message and where (L2 text line, column).
pub type Error = ktpu_l1::Error;
pub type Result<T> = std::result::Result<T, Error>;

/// A value's `DTYPE[shape] @SPACE[(MODE)]`.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Type {
    pub dtype: String,
    pub shape: Vec<i64>,
    pub space: String,
    pub mode: Option<String>,
}

impl fmt::Display for Type {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let dims: Vec<String> = self.shape.iter().map(|d| d.to_string()).collect();
        let mode = self
            .mode
            .as_ref()
            .map(|m| format!("({m})"))
            .unwrap_or_default();
        write!(
            f,
            "{}[{}] @{}{mode}",
            self.dtype,
            dims.join(", "),
            self.space
        )
    }
}

/// A value (`target` set) or an effect: the op, its terms, its type.
#[derive(Clone, Debug)]
pub struct Op {
    pub uid: usize,
    pub st: Stmt,
    pub op: String,
    pub target: Option<String>,
    pub args: Vec<Term>,
    pub ty: Option<Type>,
}

/// `par VAR in LO..HI on KIND[INDEX]`: instances on units; `index` None is
/// `[any]`.
#[derive(Clone, Debug)]
pub struct Par {
    pub uid: usize,
    pub st: Stmt,
    pub var: String,
    pub lo: Term,
    pub hi: Term,
    pub kind: String,
    pub index: Option<Term>,
    pub body: Vec<Arc<Node>>,
}

/// `for VAR in LO..HI [pipe=K]`: ordered iterations.
#[derive(Clone, Debug)]
pub struct For {
    pub uid: usize,
    pub st: Stmt,
    pub var: String,
    pub lo: Term,
    pub hi: Term,
    pub body: Vec<Arc<Node>>,
    /// at node scope, iterations the node may send ahead
    pub pipe: Option<i64>,
}

/// `when COND`: the block where the condition holds for an instance.
#[derive(Clone, Debug)]
pub struct When {
    pub uid: usize,
    pub st: Stmt,
    pub cond: Term,
    pub body: Vec<Arc<Node>>,
}

/// `if sticky`: a block a vector core runs or skips at run time.
#[derive(Clone, Debug)]
pub struct If {
    pub uid: usize,
    pub st: Stmt,
    pub cond: Term,
    pub body: Vec<Arc<Node>>,
}

#[derive(Clone, Debug)]
pub enum Node {
    Op(Op),
    Par(Par),
    For(For),
    When(When),
    If(If),
}

impl Node {
    pub fn st(&self) -> &Stmt {
        match self {
            Node::Op(x) => &x.st,
            Node::Par(x) => &x.st,
            Node::For(x) => &x.st,
            Node::When(x) => &x.st,
            Node::If(x) => &x.st,
        }
    }

    pub fn uid(&self) -> usize {
        match self {
            Node::Op(x) => x.uid,
            Node::Par(x) => x.uid,
            Node::For(x) => x.uid,
            Node::When(x) => x.uid,
            Node::If(x) => x.uid,
        }
    }

    /// The block, empty for an op.
    pub fn body(&self) -> &[Arc<Node>] {
        match self {
            Node::Op(_) => &[],
            Node::Par(x) => &x.body,
            Node::For(x) => &x.body,
            Node::When(x) => &x.body,
            Node::If(x) => &x.body,
        }
    }

    pub fn as_op(&self) -> Option<&Op> {
        match self {
            Node::Op(o) => Some(o),
            _ => None,
        }
    }

    /// The op name (`"par"`, `"for"`, ... for a block).
    pub fn op_name(&self) -> &str {
        match self {
            Node::Op(o) => &o.op,
            _ => &self.st().op,
        }
    }

    /// The value it makes, if an op with a target.
    pub fn target(&self) -> Option<&str> {
        self.as_op().and_then(|o| o.target.as_deref())
    }

    /// Its type, if a typed op.
    pub fn ty(&self) -> Option<&Type> {
        self.as_op().and_then(|o| o.ty.as_ref())
    }
}

/// An `fn NAME.l2` body read to a tree.
#[derive(Clone, Debug)]
pub struct Body {
    pub name: String,
    pub params: Vec<(String, Buffer)>,
    pub scratch: IndexMap<String, Buffer>,
    pub stmts: Vec<Arc<Node>>,
    pub reader: Reader,
    pub macros: IndexMap<String, (Vec<String>, Vec<Stmt>)>,
    /// parameters and scratch buffers by name
    pub buffers: IndexMap<String, Buffer>,
    /// the vector cores' register count and read copies (the module's
    /// `target ... vregs= vcopies=`)
    pub vregs: u32,
    pub vcopies: u32,
    /// a vector core's fill cost, (latency, cycles a word), set by the
    /// lowering for the body's vector stages
    pub fill: (i64, i64),
}

/// An L2 program: a module holding `fn name.l2`, the unit the passes take and
/// give. Its `module.source` is the L2 text.
#[derive(Clone, Debug)]
pub struct Program {
    pub module: Module,
    pub name: String,
}

impl Program {
    /// The program of a `.ktpu` text's `fn name.l2` (the first fn when None).
    pub fn parse(text: &str, file: &str, name: Option<&str>) -> Result<Program> {
        let module = ktpu_l1::text::read(text, file)?;
        let name = match name {
            Some(n) => n.to_string(),
            None => module.names().first().cloned().unwrap_or_default(),
        };
        module.body(&name, "l2")?;
        Ok(Program { module, name })
    }

    /// The program of statements: `target` (with the vector registers and read
    /// copies), `fn name.l2(params)` and its body, printed and read back.
    pub fn build(
        target: Option<&str>,
        vregs: u32,
        vcopies: u32,
        name: &str,
        params: &[(String, Term)],
        stmts: &[Stmt],
        file: &str,
    ) -> Result<Program> {
        let module = Module {
            target: target.map(str::to_string),
            vregs,
            vcopies,
            file: file.to_string(),
            ..Module::default()
        };
        let text = read::write_text(&module, name, params, stmts);
        Program::parse(&text, file, Some(name))
    }

    /// The L2 text.
    pub fn text(&self) -> &str {
        &self.module.source
    }

    /// The fn's body as a tree.
    pub fn body(&self) -> Result<Body> {
        read(&self.module, &self.name)
    }

    /// The program with the module's vector register file set (a pipeline's
    /// first step: the machine's registers become the module's).
    pub fn with_vregs(&self, vregs: u32, vcopies: u32) -> Program {
        let mut p = self.clone();
        p.module.vregs = vregs;
        p.module.vcopies = vcopies;
        p
    }
}
