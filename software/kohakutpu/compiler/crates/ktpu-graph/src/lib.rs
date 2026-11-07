//! The author's kernel trace, not yet a schedule (DSL.GRAPH).
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
use std::cell::RefCell;
use std::collections::BTreeMap;

/// The stage's version, a key of its cache entries.
pub const VERSION: u32 = 1;

/// A graph from its JSON text; a malformed graph is refused (DSL.GRAPH.001).
pub fn parse(json: &str) -> Result<Graph, Refusal> {
    serde_json::from_str(json).map_err(|e| Refusal::new("DSL.GRAPH.001", format!("{e}")))
}

/// The cycles `graph` compiles to on `machine`, including its launch intercept.
pub fn cycles(graph: &Graph, machine: &Machine) -> Result<i64, Refusal> {
    let local = node_cycles(graph, machine)?;
    Ok(machine.vc.launch_front as i64 + local.values().copied().max().unwrap_or(0))
}

/// Complete emitted node budgets, excluding launch. All nodes retain the
/// complete module's memory timing policy. Scalar and split prices share this
/// compilation within the caller's mutation context.
pub fn node_cycles(graph: &Graph, machine: &Machine) -> Result<BTreeMap<i64, i64>, Refusal> {
    let program = program(graph, machine)?;
    let machine_key = machine.digest_bytes();
    let mut key = graph.clone();
    // Graph statements have no calls. The entry name is diagnostic, not an
    // executable reference; every other field remains part of the cache key.
    key.name.clear();
    let cached = CYCLES.with(|cache| {
        cache.borrow().as_ref().and_then(|entries| {
            entries
                .iter()
                .find(|(m, previous, _)| *m == machine_key && previous == &key)
                .map(|(_, _, cycles)| cycles.clone())
        })
    });
    if let Some(cycles) = cached {
        return Ok(cycles);
    }
    let compiled = ktpu_l2::compile_l1(
        &program,
        machine,
        &ktpu_l2::PASSES,
        true,
        &ktpu_l2::Search::default(),
    )
    .map_err(|e| Refusal::new("L2.COST.001", e.to_string()))?;
    let cycles = module_node_cycles(&compiled.l1, &program.name, machine)?;
    CYCLES.with(|cache| {
        if let Some(entries) = cache.borrow_mut().as_mut() {
            if entries.len() == 32 {
                entries.remove(0);
            }
            entries.push((machine_key, key, cycles.clone()));
        }
    });
    Ok(cycles)
}

/// Physical-node prices from an already scheduled complete L1 module.
pub fn module_node_cycles(
    module: &ktpu_l1::text::Module,
    name: &str,
    machine: &Machine,
) -> Result<BTreeMap<i64, i64>, Refusal> {
    let body = module
        .body(name, "l1")
        .map_err(|e| Refusal::new(&e.clause, e.message))?;
    let local = ktpu_l2::cost::local_cycles(module, name, &ktpu_l2::Target::of(machine))
        .map_err(|e| Refusal::new(&e.clause, e.message))?;
    let nodes: Vec<_> = body.stmts.iter().filter(|stmt| stmt.op == "node").collect();
    if nodes.is_empty() {
        return Ok(BTreeMap::from([(0, local.first().copied().unwrap_or(0))]));
    }
    if nodes.len() != local.len() {
        return Err(Refusal::new(
            "L2.COST.001",
            "emitted node timing count differs from node bodies",
        ));
    }
    let mut result = BTreeMap::<i64, i64>::new();
    for (node, cycles) in nodes.into_iter().zip(local) {
        let physical = node
            .positional()
            .first()
            .and_then(|n| n.as_int())
            .and_then(|n| i64::try_from(n).ok())
            .ok_or_else(|| {
                Refusal::new("L2.COST.001", "emitted node needs a physical integer id")
            })?;
        result
            .entry(physical)
            .and_modify(|old| *old = (*old).max(cycles))
            .or_insert(cycles);
    }
    Ok(result)
}

type CycleEntries = Vec<([u8; 32], Graph, BTreeMap<i64, i64>)>;
thread_local! {
    static CYCLES: RefCell<Option<CycleEntries>> = const { RefCell::new(None) };
}

/// Share complete prices during one compiler invocation. Nested constructors
/// retain the same scope; no result survives a caller's mutation/test context.
pub fn with_cycle_cache<T>(f: impl FnOnce() -> T) -> T {
    let owner = CYCLES.with(|cache| {
        let mut cache = cache.borrow_mut();
        if cache.is_some() {
            false
        } else {
            *cache = Some(Vec::new());
            true
        }
    });
    struct Reset(bool);
    impl Drop for Reset {
        fn drop(&mut self) {
            if self.0 {
                CYCLES.with(|cache| *cache.borrow_mut() = None);
            }
        }
    }
    let _reset = Reset(owner);
    f()
}

/// The stage: graph JSON to the L2 program, for `machine`.
pub fn to_l2(graph_json: &str, machine: &Machine) -> Result<ktpu_l2::Program, Refusal> {
    program(&parse(graph_json)?, machine)
}
