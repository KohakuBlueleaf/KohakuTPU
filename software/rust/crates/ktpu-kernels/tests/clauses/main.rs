//! One test per spec clause or ruling a generator embodies
//! (`docs/spec/stack/api/{tensor,tinygrad}.md`, `rulings.md`), named after
//! it; the spec vectors that call a generator directly, with their
//! expected facts; under the `mutants` feature, each such vector's mutant
//! breaking it.

#[path = "../common/mod.rs"]
mod common;

mod expr;
mod facts;
mod matmul;
mod moves;
#[cfg(feature = "mutants")]
mod mutants;
mod ring;
mod rows;
mod rulings;

use ktpu_graph as g;
use ktpu_machine::Machine;

/// The spec vectors' machine: `multimesh_v9t6` traced on one node.
pub fn card() -> Machine {
    common::machine("multimesh_v9t6", 1)
}

/// A kernel's L2 text, as the Python tracer prints it.
pub fn text(k: &ktpu_kernels::Kernel) -> String {
    ktpu_graph::lower(&k.graph).expect("a checked graph").text()
}

/// The statement lines of a kernel's text starting with `op`.
pub fn lines(k: &ktpu_kernels::Kernel, op: &str) -> Vec<String> {
    text(k)
        .lines()
        .map(|l| l.trim().to_string())
        .filter(|l| l.split(' ').next() == Some(op))
        .collect()
}

/// Every statement of a body, depth first.
pub fn stmts(body: &[g::Stmt]) -> Vec<&g::Stmt> {
    let mut out = vec![];
    for s in body {
        out.push(s);
        if let Some(b) = s.node.body() {
            out.extend(stmts(b));
        }
    }
    out
}

/// The value ops a kernel records, in order.
pub fn value_ops(k: &ktpu_kernels::Kernel) -> Vec<&'static str> {
    stmts(&k.graph.body)
        .into_iter()
        .filter_map(|s| match &s.node {
            g::Node::Value { op, .. } => Some(op.name()),
            _ => None,
        })
        .collect()
}

/// The float operands a kernel's value statements hold.
pub fn floats(k: &ktpu_kernels::Kernel) -> Vec<f64> {
    stmts(&k.graph.body)
        .into_iter()
        .flat_map(|s| match &s.node {
            g::Node::Value { args, .. } => args
                .iter()
                .filter_map(|a| match a {
                    g::Operand::Float(n) => n.value(),
                    _ => None,
                })
                .collect(),
            _ => vec![],
        })
        .collect()
}
