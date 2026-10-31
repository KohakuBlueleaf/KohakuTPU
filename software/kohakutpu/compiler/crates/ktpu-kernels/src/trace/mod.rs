//! The recorder the generators build their graphs with: the `kt` tracer's
//! statements (`docs/spec/stack/dsl/graph.md`) as Rust calls, recorded into
//! the statement tree `ktpu_graph::Graph` holds.
//!
//! A [`Trace`] holds one kernel's ports, scratch buffers and statement tree;
//! control statements take a closure run once with the variable
//! ([`control`]), value statements return a [`V`] handle ([`vector`],
//! [`cluster`]), mover statements record at node scope ([`mover`]). Each
//! statement's `where` is the Rust line that recorded it.

pub mod buffer;
pub mod cluster;
pub mod control;
pub mod index;
pub mod mover;
pub mod vector;

pub use buffer::{Ax, Buf, View};
pub use index::{var, Cond, Idx};
pub use vector::Arg;

use ktpu_graph as g;
use ktpu_machine::{Machine, Refusal};
use std::collections::{HashMap, HashSet};
use std::panic::Location;

pub type R<T> = Result<T, Refusal>;

/// `Err(Refusal)` naming `clause`.
pub fn fail<T>(clause: &str, message: impl Into<String>) -> R<T> {
    Err(Refusal::new(clause, message))
}

/// What the statements being recorded run on (DSL.SCOPE).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Scope {
    /// None: node scope; else the unit instance's kind.
    pub kind: Option<g::UnitKind>,
    pub in_loop: bool,
    pub sticky: bool,
    /// Inside a par over nodes: one node's program.
    pub node: bool,
}

/// One traced value: an L2 name, its type and where its statement is.
#[derive(Clone, Debug)]
pub struct ValueData {
    pub name: String,
    pub dtype: g::Dtype,
    pub shape: Vec<i64>,
    pub space: g::ValSpace,
    pub mode: Option<g::Mode>,
    /// An @l1 value's unpacked register value, made on first use.
    pub unpacked: Option<V>,
    /// The block and statement that define it.
    pub block: usize,
    pub stmt: usize,
    /// Loaded from a `tile=` buffer: its bytes are sub-tiles.
    pub tiled: bool,
    /// One lane broadcast to every lane (`unpack lane=`): rank 1, but one
    /// value for all rows, not one a row.
    pub lane: bool,
    /// A one-row view read `gt4` (or math over such rows alone): its rows
    /// are the band's broadcast, not the data's rows.
    pub row_bcast: bool,
}

impl ValueData {
    /// A row statistic: rank 1 in registers (DSL.VAL.001).
    pub fn stat(&self) -> bool {
        self.space == g::ValSpace::Reg && self.shape.len() == 1
    }

    pub fn graph_type(&self) -> g::ValueType {
        g::ValueType {
            dtype: self.dtype,
            shape: self.shape.clone(),
            space: self.space,
            mode: self.mode,
        }
    }
}

/// A traced value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct V(pub usize);

struct Slot {
    node: g::Node,
    body: Option<usize>,
    at: String,
}

/// What the tracer reads of the machine (DSL.KERN.013, RUL.018).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Facts {
    pub mg: i64,
    pub vc: i64,
    pub nodes: i64,
    /// f16 values a 32-byte word holds (`vc.lanes`).
    pub lanes: i64,
    /// Significand bits of a lane value (`vc.sig_bits`, RUL.060).
    pub sig_bits: i64,
    /// Rows of a GT4 band (`mg.lanes`) and columns of a GT4 block
    /// (`mg.kblock`).
    pub band: i64,
    pub kblock: i64,
}

impl Facts {
    pub fn of(m: &Machine) -> Facts {
        Facts {
            mg: m.mg.count as i64,
            vc: m.vc.count as i64,
            nodes: m.nodes() as i64,
            lanes: m.vc.lanes as i64,
            sig_bits: m.vc.sig_bits as i64,
            band: m.mg.lanes as i64,
            kblock: m.mg.kblock as i64,
        }
    }
}

/// One kernel being recorded.
pub struct Trace {
    pub name: String,
    /// The module's `target` (`die.target`, RUL.010).
    pub target: String,
    pub facts: Facts,
    pub params: Vec<(String, g::Role, g::Spec)>,
    pub scratch: Vec<(String, g::Spec)>,
    slots: Vec<Slot>,
    blocks: Vec<Vec<usize>>,
    stack: Vec<usize>,
    scopes: Vec<Scope>,
    ids: HashMap<String, u64>,
    taken: HashSet<String>,
    values: Vec<ValueData>,
}

#[track_caller]
pub(crate) fn here() -> String {
    let loc = Location::caller();
    let file = loc.file().rsplit(['/', '\\']).next().unwrap_or(loc.file());
    format!("{file}:{}", loc.line())
}

impl Trace {
    pub fn new(name: &str, facts: Facts, target: &str) -> Trace {
        Trace {
            name: name.to_string(),
            target: target.to_string(),
            facts,
            params: vec![],
            scratch: vec![],
            slots: vec![],
            blocks: vec![vec![]],
            stack: vec![0],
            scopes: vec![Scope::default()],
            ids: HashMap::new(),
            taken: HashSet::new(),
            values: vec![],
        }
    }

    pub fn scope(&self) -> Scope {
        *self.scopes.last().expect("a scope")
    }

    /// `PREFIXn`, the first n past the last one of `prefix` whose name no
    /// port, buffer or value took (DSL.CTRL.003).
    pub fn fresh(&mut self, prefix: &str) -> String {
        let mut n = self.ids.get(prefix).copied().unwrap_or(0);
        let name = loop {
            n += 1;
            let name = format!("{prefix}{n}");
            if !self.taken.contains(&name) {
                break name;
            }
        };
        self.ids.insert(prefix.to_string(), n);
        self.taken.insert(name.clone());
        name
    }

    pub fn claim(&mut self, name: &str) -> R<String> {
        if !self.taken.insert(name.to_string()) {
            return fail("DSL.CTRL.013", format!("the name {name:?} is used twice"));
        }
        Ok(name.to_string())
    }

    /// A port of the kernel, bound as a buffer (DSL.KERN.012).
    pub fn port(&mut self, name: &str, role: g::Role, spec: g::Spec) -> R<Buf> {
        self.claim(name)?;
        self.params.push((name.to_string(), role, spec.clone()));
        Ok(Buf::new(name, spec))
    }

    pub fn value(&self, v: V) -> &ValueData {
        &self.values[v.0]
    }

    /// Records `node` in the current block.
    pub(crate) fn emit(&mut self, node: g::Node, at: String) -> usize {
        let id = self.slots.len();
        self.slots.push(Slot {
            node,
            body: None,
            at,
        });
        let top = *self.stack.last().expect("a block");
        self.blocks[top].push(id);
        id
    }

    /// Records `node` with a block that `body` fills, in `scope`; an empty
    /// block is refused (DSL.CTRL.009).
    pub(crate) fn block<T>(
        &mut self,
        node: g::Node,
        at: String,
        scope: Scope,
        body: impl FnOnce(&mut Trace) -> R<T>,
    ) -> R<T> {
        let id = self.emit(node, at.clone());
        let blk = self.blocks.len();
        self.blocks.push(vec![]);
        self.slots[id].body = Some(blk);
        self.stack.push(blk);
        self.scopes.push(scope);
        let got = body(self);
        self.stack.pop();
        self.scopes.pop();
        let got = got?;
        if self.blocks[blk].is_empty() {
            return fail("DSL.CTRL.009", format!("a block recorded nothing, at {at}"));
        }
        Ok(got)
    }

    /// The statement the current scope needs (DSL.SCOPE.001, .003).
    pub(crate) fn need(&self, kind: Option<g::UnitKind>, what: &str) -> R<()> {
        let have = self.scope().kind;
        if have == kind {
            return Ok(());
        }
        let name = |k: Option<g::UnitKind>| match k {
            None => "node scope",
            Some(g::UnitKind::Mg) => "a cluster",
            Some(g::UnitKind::Vc) => "a vector core",
            Some(g::UnitKind::Node) => "a node",
        };
        fail(
            "DSL.SCOPE.003",
            format!("{what} runs on {}, not in {}", name(kind), name(have)),
        )
    }

    /// A new value made by `node` (its target `prefix`N); with `after`, the
    /// statement goes just behind that value's own when it is in another
    /// block (DSL.VEC.023).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_value(
        &mut self,
        op: g::ValueOp,
        args: Vec<g::Operand>,
        kw: Vec<(String, g::Attr)>,
        ty: (g::Dtype, Vec<i64>, g::ValSpace, Option<g::Mode>),
        prefix: &str,
        after: Option<V>,
        at: String,
    ) -> V {
        let name = self.fresh(prefix);
        let (dtype, shape, space, mode) = ty;
        let data = ValueData {
            name: name.clone(),
            dtype,
            shape,
            space,
            mode,
            unpacked: None,
            block: 0,
            stmt: 0,
            tiled: false,
            lane: false,
            row_bcast: false,
        };
        let node = g::Node::Value {
            op,
            target: name,
            ty: data.graph_type(),
            args,
            kw,
        };
        let id = self.emit(node, at);
        let top = *self.stack.last().expect("a block");
        let mut block = top;
        if let Some(a) = after {
            let (ab, astmt) = (self.values[a.0].block, self.values[a.0].stmt);
            if ab != top {
                self.blocks[top].pop();
                let pos = self.blocks[ab]
                    .iter()
                    .position(|s| *s == astmt)
                    .expect("the value's statement");
                self.blocks[ab].insert(pos + 1, id);
                block = ab;
            }
        }
        self.values.push(ValueData {
            block,
            stmt: id,
            ..data
        });
        V(self.values.len() - 1)
    }

    /// A value named as an earlier one (a chained gemm), its statement here.
    pub(crate) fn rebound(&mut self, data: ValueData, node: g::Node, at: String) -> V {
        let id = self.emit(node, at);
        let top = *self.stack.last().expect("a block");
        self.values.push(ValueData {
            block: top,
            stmt: id,
            ..data
        });
        V(self.values.len() - 1)
    }

    pub(crate) fn set_unpacked(&mut self, x: V, u: V) {
        self.values[x.0].unpacked = Some(u);
    }

    pub(crate) fn set_tiled(&mut self, x: V, tiled: bool) {
        self.values[x.0].tiled = tiled;
    }

    fn stmts(&self, block: usize) -> Vec<g::Stmt> {
        self.blocks[block]
            .iter()
            .map(|&id| {
                let s = &self.slots[id];
                let mut node = s.node.clone();
                if let Some(b) = s.body {
                    let body = self.stmts(b);
                    match &mut node {
                        g::Node::Par { body: slot, .. }
                        | g::Node::For { body: slot, .. }
                        | g::Node::When { body: slot, .. }
                        | g::Node::Sticky { body: slot } => *slot = body,
                        _ => unreachable!("a block on a plain statement"),
                    }
                }
                g::Stmt {
                    node,
                    at: s.at.clone(),
                }
            })
            .collect()
    }

    /// The trace as graph data (DSL.GRAPH.001); a body that recorded nothing
    /// is refused (DSL.KERN.025).
    pub fn graph(&self) -> R<g::Graph> {
        if self.blocks[0].is_empty() {
            return fail(
                "DSL.KERN.025",
                format!("{} recorded no statement", self.name),
            );
        }
        Ok(g::Graph {
            format: g::FORMAT.to_string(),
            version: g::FORMAT_VERSION,
            name: self.name.clone(),
            target: self.target.clone(),
            machine: g::DslMachine {
                mg: self.facts.mg as u32,
                vc: self.facts.vc as u32,
                nodes: self.facts.nodes as u32,
            },
            consts: serde_json::Value::Object(Default::default()),
            ports: self
                .params
                .iter()
                .map(|(name, role, spec)| g::Port {
                    name: name.clone(),
                    role: *role,
                    spec: spec.clone(),
                })
                .collect(),
            scratch: self
                .scratch
                .iter()
                .map(|(name, spec)| g::Scratch {
                    name: name.clone(),
                    spec: spec.clone(),
                })
                .collect(),
            body: self.stmts(0),
        })
    }
}
