//! The tracer's graph and its analysis into L2 (`docs/spec/stack/dsl/graph.md`).
//!
//! The Python tracer (`kohakutpu.language.dsl`) writes each trace as graph
//! JSON (`Traced.graph`); [`parse`] reads it, [`check`] holds it to
//! DSL.GRAPH.018, and [`to_l2`] builds the `ktpu_l2::Program` from its
//! statements ([`term`]). [`text::lower`] dumps the tracer's L2 text.

pub mod analyse;
pub mod graph;
pub mod mutant;
pub mod term;
pub mod text;

pub use analyse::{build, check, check_machine, file_of, program};
pub use graph::*;
pub use text::{lower, L2Module, Line};

use ktpu_machine::{Machine, Refusal};

/// The stage's version, a key of its cache entries.
pub const VERSION: u32 = 1;

/// A graph from its JSON text; a malformed graph is refused (DSL.GRAPH.001).
pub fn parse(json: &str) -> Result<Graph, Refusal> {
    serde_json::from_str(json).map_err(|e| Refusal::new("DSL.GRAPH.001", format!("{e}")))
}

/// The cycles `graph` compiles to on `machine` (`ktpu_l2::cost`), or the
/// reason it does not lower.
pub fn cycles(graph: &Graph, machine: &Machine) -> Result<i64, Refusal> {
    ktpu_l2::cost::of_program(&program(graph, machine)?, machine)
        .map_err(|e| Refusal::new("L2.COST.001", e.to_string()))
}

/// The stage: graph JSON to the L2 program, for `machine`.
pub fn to_l2(graph_json: &str, machine: &Machine) -> Result<ktpu_l2::Program, Refusal> {
    program(&parse(graph_json)?, machine)
}
