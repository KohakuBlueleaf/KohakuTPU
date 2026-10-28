//! The checks the backend owes a graph, and the stage: a checked graph
//! built into the `ktpu_l2::Program` it stands for.

use std::collections::HashSet;

use ktpu_l2::Program;
use ktpu_machine::{refuse, Machine, Refusal, UnitKind as MachineUnit};

use crate::graph::*;
use crate::term;

/// Where statements run (DSL.SCOPE.001).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Scope {
    Node,
    Cluster,
    Core,
}

impl Scope {
    fn name(self) -> &'static str {
        match self {
            Scope::Node => "node scope",
            Scope::Cluster => "a cluster",
            Scope::Core => "a vector core",
        }
    }
}

/// The graph's DSL machine against `machine` (DSL.KERN.011, .013): its
/// clusters and cores are one node's, its nodes at most the card's.
pub fn check_machine(g: &Graph, machine: &Machine) -> Result<(), Refusal> {
    let mg = machine.units(MachineUnit::Mg).len() as u32;
    let vc = machine.units(MachineUnit::Vc).len() as u32;
    let m = g.machine;
    if m.mg != mg || m.vc != vc || m.nodes == 0 || m.nodes > machine.nodes() {
        return refuse(
            "DSL.KERN.011",
            format!(
                "{} was traced for mg={} vc={} nodes={}; {} has mg={mg} vc={vc} and {} nodes",
                g.name,
                m.mg,
                m.vc,
                m.nodes,
                machine.name,
                machine.nodes()
            ),
        );
    }
    Ok(())
}

/// The checks of DSL.GRAPH.018: the format, unique buffer names, every
/// buffer, value and variable defined before use, statements in their
/// scope, no empty block, every operand spelt in L2.
pub fn check(g: &Graph) -> Result<(), Refusal> {
    if g.format != FORMAT || g.version != FORMAT_VERSION {
        return refuse(
            "DSL.GRAPH.001",
            format!(
                "a graph is {FORMAT} version {FORMAT_VERSION}, got {} version {}",
                g.format, g.version
            ),
        );
    }
    let mut w = Walker {
        buffers: HashSet::new(),
        values: HashSet::new(),
        vars: Vec::new(),
    };
    let names = g
        .ports
        .iter()
        .map(|p| &p.name)
        .chain(g.scratch.iter().map(|s| &s.name));
    for n in names {
        if !w.buffers.insert(n.clone()) {
            return refuse("DSL.GRAPH.001", format!("the buffer {n} is named twice"));
        }
    }
    let specs = g
        .ports
        .iter()
        .map(|p| &p.spec)
        .chain(g.scratch.iter().map(|s| &s.spec));
    for s in specs {
        for (k, v) in &s.layout {
            if matches!(v, LayoutValue::Str(x) if !is_name(x)) {
                return refuse(
                    "DSL.GRAPH.014",
                    format!("the layout value of {k} ({v:?}) is no name"),
                );
            }
        }
    }
    w.block(&g.body, Scope::Node)
}

/// A dotted name of the text grammar (`ktpu.pest` name).
fn is_name(s: &str) -> bool {
    s.split('.').all(|p| {
        let mut c = p.chars();
        c.next()
            .is_some_and(|f| f.is_ascii_alphabetic() || f == '_')
            && c.all(|x| x.is_ascii_alphanumeric() || x == '_')
    })
}

/// The file name of the program built from a graph.
pub fn file_of(g: &Graph) -> String {
    format!("<graph {}>", g.name)
}

/// The L2 program of `g` for `machine`, built from its statements
/// ([`term::stmts`]) through ktpu-l2's builder (DSL.GRAPH.019); its register
/// file the machine's vector core (`vc.regs`, `vc.rf_copies`).
pub fn program(g: &Graph, machine: &Machine) -> Result<Program, Refusal> {
    check_machine(g, machine)?;
    let t = ktpu_l2::Target::of(machine);
    with_registers(g, t.vregs, t.vcopies)
}

/// The program the trace's text stands for: [`program`] without the
/// machine, its register file the text's default (a `target` line naming
/// none).
pub fn build(g: &Graph) -> Result<Program, Refusal> {
    let text = ktpu_l1::text::Module::default();
    with_registers(g, text.vregs, text.vcopies)
}

fn with_registers(g: &Graph, vregs: u32, vcopies: u32) -> Result<Program, Refusal> {
    check(g)?;
    Program::build(
        Some(&g.target),
        vregs,
        vcopies,
        &g.name,
        &term::params(g),
        &term::stmts(g),
        &file_of(g),
    )
    .map_err(|e| Refusal::new(e.clause.clone(), e.to_string()))
}

struct Walker {
    buffers: HashSet<String>,
    values: HashSet<String>,
    vars: Vec<String>,
}

fn need(st: &Stmt, what: &str, want: Scope, have: Scope) -> Result<(), Refusal> {
    if want != have {
        return refuse(
            "DSL.SCOPE.003",
            format!(
                "{what} runs on {}, not in {}, at {}",
                want.name(),
                have.name(),
                st.at
            ),
        );
    }
    Ok(())
}

impl Walker {
    fn block(&mut self, stmts: &[Stmt], scope: Scope) -> Result<(), Refusal> {
        stmts.iter().try_for_each(|s| self.stmt(s, scope))
    }

    fn body(&mut self, st: &Stmt, body: &[Stmt], scope: Scope) -> Result<(), Refusal> {
        if body.is_empty() {
            return refuse(
                "DSL.CTRL.009",
                format!("a block statement recorded an empty block, at {}", st.at),
            );
        }
        self.block(body, scope)
    }

    fn stmt(&mut self, st: &Stmt, scope: Scope) -> Result<(), Refusal> {
        match &st.node {
            Node::Par {
                var,
                lo,
                hi,
                unit,
                body,
                ..
            } => {
                if scope != Scope::Node {
                    return refuse(
                        "DSL.CTRL.012",
                        format!("a par inside a unit's instance, at {}", st.at),
                    );
                }
                self.index(st, lo)?;
                self.index(st, hi)?;
                self.bind(st, var)?;
                self.unit(st, unit)?;
                let inner = match unit.kind {
                    UnitKind::Mg => Scope::Cluster,
                    UnitKind::Vc => Scope::Core,
                    UnitKind::Node => Scope::Node,
                };
                self.body(st, body, inner)?;
                self.vars.pop();
            }
            Node::For {
                var, lo, hi, body, ..
            } => {
                self.index(st, lo)?;
                self.index(st, hi)?;
                self.bind(st, var)?;
                self.body(st, body, scope)?;
                self.vars.pop();
            }
            Node::When { cond, body } => {
                self.index(st, &cond.a)?;
                self.index(st, &cond.b)?;
                self.body(st, body, scope)?;
            }
            Node::Sticky { body } => {
                need(st, "if sticky", Scope::Core, scope)?;
                self.body(st, body, scope)?;
            }
            Node::Value {
                op,
                target,
                args,
                kw,
                ..
            } => {
                match op.runs() {
                    Runs::Cluster => need(st, op.name(), Scope::Cluster, scope)?,
                    Runs::Core => need(st, op.name(), Scope::Core, scope)?,
                    Runs::Unit if scope == Scope::Node => {
                        need(st, op.name(), Scope::Cluster, scope)?
                    }
                    Runs::Unit => {}
                }
                for a in args {
                    self.operand(st, a)?;
                }
                for (_, a) in kw {
                    self.attr(st, a)?;
                }
                self.values.insert(target.clone());
            }
            Node::Store { dst, src } => {
                need(st, "store", Scope::Core, scope)?;
                self.value(st, src)?;
                self.view(st, dst)?;
            }
            Node::Drain { src, dst, node, .. } => {
                need(st, "drain", Scope::Cluster, scope)?;
                self.value(st, src)?;
                self.operand(st, dst)?;
                if let Some(n) = node {
                    self.index(st, n)?;
                }
            }
            Node::Send { src, dst, .. } => {
                need(st, "send", Scope::Core, scope)?;
                self.value(st, src)?;
                self.unit(st, dst)?;
            }
            Node::Mover {
                op, dst, src, kw, ..
            } => {
                need(st, op.name(), Scope::Node, scope)?;
                self.view(st, dst)?;
                if let Some(s) = src {
                    self.view(st, s)?;
                }
                for (_, a) in kw {
                    self.attr(st, a)?;
                }
            }
        }
        Ok(())
    }

    fn bind(&mut self, st: &Stmt, var: &str) -> Result<(), Refusal> {
        if self.vars.iter().any(|v| v == var) {
            return refuse(
                "DSL.CTRL.013",
                format!("the variable {var} is bound twice, at {}", st.at),
            );
        }
        self.vars.push(var.to_string());
        Ok(())
    }

    fn index(&self, st: &Stmt, x: &Index) -> Result<(), Refusal> {
        let Index::Sym(s) = x else { return Ok(()) };
        for (a, _) in &s.terms {
            match a {
                Atom::Var(v) if !self.vars.iter().any(|b| b == v) => {
                    return refuse(
                        "DSL.GRAPH.007",
                        format!("{v} is bound by no enclosing par or for, at {}", st.at),
                    )
                }
                Atom::Var(_) => {}
                Atom::Floor(p, q) | Atom::Mod(p, q) | Atom::Mul(p, q) => {
                    self.index(st, p)?;
                    self.index(st, q)?;
                }
            }
        }
        Ok(())
    }

    fn axes(&self, st: &Stmt, axes: &[Axis]) -> Result<(), Refusal> {
        for a in axes {
            if let Axis::Run(e, _) | Axis::Idx(e) = a {
                self.index(st, e)?;
            }
        }
        Ok(())
    }

    fn unit(&self, st: &Stmt, u: &UnitRef) -> Result<(), Refusal> {
        match &u.index {
            Some(e) => self.index(st, e),
            None => Ok(()),
        }
    }

    fn value(&self, st: &Stmt, name: &str) -> Result<(), Refusal> {
        if !self.values.contains(name) {
            return refuse(
                "DSL.GRAPH.006",
                format!("{name} is used before a statement defines it, at {}", st.at),
            );
        }
        Ok(())
    }

    fn view(&self, st: &Stmt, v: &View) -> Result<(), Refusal> {
        if !self.buffers.contains(&v.buffer) {
            return refuse(
                "DSL.GRAPH.006",
                format!("{} is no port or scratch buffer, at {}", v.buffer, st.at),
            );
        }
        self.axes(st, &v.axes)
    }

    fn attr(&self, st: &Stmt, a: &Attr) -> Result<(), Refusal> {
        match a {
            Attr::View(v) => self.view(st, v),
            Attr::Index(e) => self.index(st, e),
            Attr::Str(s) if !is_name(s) => refuse(
                "DSL.GRAPH.017",
                format!("a keyword value {s:?} is no name, at {}", st.at),
            ),
            _ => Ok(()),
        }
    }

    fn operand(&self, st: &Stmt, o: &Operand) -> Result<(), Refusal> {
        let unspelt = |what: &str| {
            refuse(
                "DSL.GRAPH.006",
                format!("{what} has no L2 spelling, at {}", st.at),
            )
        };
        match o {
            Operand::Value(n) | Operand::Bcast(n) => self.value(st, n),
            Operand::Vview(v) => {
                if v.axes.contains(&Axis::New) {
                    return unspelt("a new axis of a value");
                }
                self.value(st, &v.value)?;
                self.axes(st, &v.axes)
            }
            Operand::View(v) => self.view(st, v),
            Operand::Unit(u) => self.unit(st, u),
            Operand::Float(x) if x.value().is_none() => {
                unspelt("a float named other than inf, -inf, nan")
            }
            Operand::Float(_) => Ok(()),
        }
    }
}
