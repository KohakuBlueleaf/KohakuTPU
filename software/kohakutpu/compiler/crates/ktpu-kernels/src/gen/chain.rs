//! Two kernels as one (`kernel.chain`): the first's statements, then the
//! second's, the second's input port `from` read from the first's output port
//! `to`. With `keep` that output stays a port, written to DRAM for later
//! readers; without it, it is a `@mem` scratch the `space` pass places in
//! staging where it fits (L2.PASS.004), never stored to DRAM.
//!
//! The second kernel's names take the first prefix `c<k>_` (k from 2) under
//! which none meets a name of the first, so chains nest. The graph's
//! `consts.chain.ports` maps each second port to its name in the chain.

mod crop;
mod forward;
mod index;
mod input;
pub(crate) mod link;
mod lookahead;
mod policy;
pub mod quant;
mod reuse;
mod rows;
pub mod sequence;
pub(crate) mod wave;
mod windows;

pub(crate) use crop::apply as crop_boundary;
pub(crate) use input::apply as input_boundary;
pub use lookahead::{advance_compute, hoist_inputs, independent_tail};
pub(crate) use policy::finish;
pub(crate) use reuse::kernel as reuse_stage;

use std::collections::{BTreeMap, HashSet};

use ktpu_graph as g;
use serde_json::{json, Value};

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::{fail, R};

/// The JSON keys whose string values name a buffer, a value or a loop
/// variable (DSL.GRAPH.002-.009).
const NAMED: [&str; 6] = ["buffer", "value", "target", "var", "src", "bcast"];

fn names_in(v: &Value, keys: &[&str], out: &mut HashSet<String>) {
    match v {
        Value::Object(m) => {
            for (k, x) in m {
                match x {
                    Value::String(s) if keys.contains(&k.as_str()) => {
                        out.insert(s.clone());
                    }
                    _ => names_in(x, keys, out),
                }
            }
        }
        Value::Array(xs) => xs.iter().for_each(|x| names_in(x, keys, out)),
        _ => {}
    }
}

/// Definitions owned by a region, excluding references to enclosing binders.
/// JSON `var` keys also encode free index references and are not declarations.
fn bound_names(body: &[g::Stmt], out: &mut HashSet<String>) {
    for stmt in body {
        match &stmt.node {
            g::Node::Par { var, .. } | g::Node::For { var, .. } => {
                out.insert(var.clone());
            }
            g::Node::Value { target, .. } => {
                out.insert(target.clone());
            }
            _ => {}
        }
        if let Some(body) = stmt.node.body() {
            bound_names(body, out);
        }
    }
}

fn renamed(v: &mut Value, names: &HashSet<String>, to: &dyn Fn(&str) -> String) {
    match v {
        Value::Object(m) => {
            for (k, x) in m.iter_mut() {
                match x {
                    Value::String(s) if NAMED.contains(&k.as_str()) && names.contains(s) => {
                        *s = to(s);
                    }
                    _ => renamed(x, names, to),
                }
            }
        }
        Value::Array(xs) => xs.iter_mut().for_each(|x| renamed(x, names, to)),
        _ => {}
    }
}

fn port<'a>(k: &'a Kernel, n: &str) -> R<&'a g::Port> {
    match k.graph.ports.iter().find(|p| p.name == n) {
        Some(p) => Ok(p),
        None => fail(
            "API.OP.001",
            format!("kernel.chain: {} has no port {n}", k.graph.name),
        ),
    }
}

/// `first` then `second`, `second`'s input `from` reading `first`'s output
/// `to`: same element type and layout, the same row width, and no more rows
/// than `to` holds.
pub fn kernel(first: Kernel, second: Kernel, from: &str, to: &str, keep: bool) -> R<Kernel> {
    let (src, dst) = (port(&second, from)?, port(&first, to)?);
    let (s, d) = (&src.spec, &dst.spec);
    if src.role != g::Role::In
        || dst.role != g::Role::Out
        || s.dtype != d.dtype
        || s.layout != d.layout
        || s.shape.len() != d.shape.len()
        || s.shape[1..] != d.shape[1..]
        || s.shape[0] > d.shape[0]
    {
        return fail(
            "API.OP.001",
            format!("kernel.chain: {from} {s:?} does not read {to} {d:?}"),
        );
    }
    let mut own: HashSet<String> = second.graph.ports.iter().map(|p| p.name.clone()).collect();
    own.extend(second.graph.scratch.iter().map(|p| p.name.clone()));
    let mut body = match serde_json::to_value(&second.graph.body) {
        Ok(b) => b,
        Err(e) => return fail("API.OP.001", format!("kernel.chain: {e}")),
    };
    bound_names(&second.graph.body, &mut own);
    let mut taken: HashSet<String> = first.graph.ports.iter().map(|p| p.name.clone()).collect();
    taken.extend(first.graph.scratch.iter().map(|p| p.name.clone()));
    match serde_json::to_value(&first.graph.body) {
        Ok(b) => names_in(&b, &NAMED, &mut taken),
        Err(e) => return fail("API.OP.001", format!("kernel.chain: {e}")),
    }
    let prefix = (2..)
        .map(|k| format!("c{k}_"))
        .find(|p| {
            own.iter()
                .all(|n| n == from || !taken.contains(&format!("{p}{n}")))
        })
        .unwrap_or_default();
    let link = to.to_string();
    let to_name = |n: &str| match n == from {
        true => link.clone(),
        false => format!("{prefix}{n}"),
    };
    renamed(&mut body, &own, &to_name);
    let body: Vec<g::Stmt> = match serde_json::from_value(body) {
        Ok(b) => b,
        Err(e) => return fail("API.OP.001", format!("kernel.chain: {e}")),
    };

    let mut graph = first.graph.clone();
    let mut given = first.given.clone();
    if !keep {
        graph.ports.retain(|p| p.name != to);
        given.retain(|(n, _)| n != to);
        graph.scratch.push(g::Scratch {
            name: to.to_string(),
            spec: g::Spec {
                space: g::BufSpace::Mem,
                ..d.clone()
            },
        });
    }
    for p in second.graph.ports.iter().filter(|p| p.name != from) {
        graph.ports.push(g::Port {
            name: to_name(&p.name),
            ..p.clone()
        });
    }
    for (n, s) in second.given.iter().filter(|(n, _)| n != from) {
        given.push((to_name(n), s.clone()));
    }
    for sc in &second.graph.scratch {
        graph.scratch.push(g::Scratch {
            name: to_name(&sc.name),
            ..sc.clone()
        });
    }
    graph.body.extend(body);
    let ports: BTreeMap<String, String> = second
        .graph
        .ports
        .iter()
        .filter(|p| p.name != from)
        .map(|p| (p.name.clone(), to_name(&p.name)))
        .collect();
    let first_ports: BTreeMap<_, _> = first
        .graph
        .ports
        .iter()
        .filter(|p| keep || p.name != to)
        .map(|p| (p.name.clone(), p.name.clone()))
        .collect();
    graph.consts = json!({"chain": {
        "first_ports": first_ports,
        "first": first.graph.consts,
        "second": second.graph.consts,
        "ports": ports,
    }});
    graph.name = name(
        "chain",
        vec![
            Py::str(&first.graph.name),
            Py::str(&second.graph.name),
            Py::str(from),
            Py::str(to),
            Py::Bool(keep),
        ],
    );
    Ok(Kernel { graph, given })
}

/// Compose typed stages and retain a materialized pointwise consumer within
/// producer waves where the shared scheduling proof applies.
#[allow(clippy::too_many_arguments)]
pub(crate) fn scheduled(
    first: Kernel,
    second: Kernel,
    from: &str,
    to: &str,
    keep: bool,
    consumer: Option<&wave::Consumer>,
    view: link::View,
    machine: &ktpu_machine::Machine,
) -> R<Kernel> {
    let producer_len = first.graph.body.len();
    let same_domain = port(&first, to)?.spec == port(&second, from)?.spec;
    let second = if let Some(binding) = link::Binding::of(&first, &second, from, to, view, machine)?
    {
        binding.apply(second)?
    } else {
        second
    };
    let mut kernel = kernel(first, second, from, to, keep)?;
    if !keep && same_domain {
        if let Some(consumer) = consumer {
            wave::apply(&mut kernel, producer_len, to, consumer, machine)?;
        }
    }
    Ok(kernel)
}

/// Compose one independent row owner's stages. The caller supplies the
/// producer's physical row quantum; private edges retain whole statistic words.
#[allow(clippy::too_many_arguments)]
pub(crate) fn row_kernel(
    first: Kernel,
    second: Kernel,
    from: &str,
    to: &str,
    keep: bool,
    origin: i64,
    count: i64,
    quantum: Option<i64>,
    consumer: Option<&wave::Consumer>,
    view: link::View,
    machine: &ktpu_machine::Machine,
) -> R<Kernel> {
    let mut result = scheduled(first, second, from, to, keep, consumer, view, machine)?;
    if !keep {
        if let Some(quantum) = quantum {
            rows::compact(
                &mut result,
                to,
                origin,
                super::cdiv(count, quantum) * quantum,
            )?;
        }
    }
    Ok(result)
}

/// Independently node-scoped schedules sharing the invocation's global ports.
/// Values, loops and scratch remain private to each schedule.
pub(crate) fn shares(signature: &Kernel, kernels: Vec<Kernel>) -> R<Kernel> {
    sequence::scoped(signature, kernels)
}

/// Place an already composed local schedule on its granted physical sysnode.
pub(crate) fn placed(mut kernel: Kernel, node: i64) -> R<Kernel> {
    if node == 0 {
        return Ok(kernel);
    }
    let mut taken: HashSet<_> = kernel.graph.ports.iter().map(|p| p.name.clone()).collect();
    taken.extend(kernel.graph.scratch.iter().map(|s| s.name.clone()));
    let value = serde_json::to_value(&kernel.graph.body)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    names_in(&value, &NAMED, &mut taken);
    let var = (0..)
        .map(|i| format!("chain_node_{i}"))
        .find(|n| taken.insert(n.clone()))
        .expect("an unused node variable");
    let body = std::mem::take(&mut kernel.graph.body);
    kernel.graph.body.push(g::Stmt {
        at: "<chain node>".into(),
        node: g::Node::Par {
            var,
            lo: g::Index::Int(0),
            hi: g::Index::Int(1),
            unit: g::UnitRef {
                kind: g::UnitKind::Node,
                index: Some(g::Index::Int(node)),
            },
            classed: false,
            class_band: None,
            repeats: None,
            advances: vec![],
            body,
        },
    });
    Ok(kernel)
}

/// Optional phase ordering is shared by typed chains and typed sequences.
pub(crate) fn ordered(kernel: Kernel, args: &Value, machine: &ktpu_machine::Machine) -> R<Kernel> {
    match args.get("pipeline_lookahead") {
        None | Some(Value::Bool(false)) => Ok(kernel),
        Some(Value::Bool(true)) => lookahead::kernel(kernel, machine),
        _ => fail("API.OP.001", "pipeline_lookahead must be boolean"),
    }
}

/// Joint storage/ordering choice for a chain whose quant policy is already set.
pub(crate) fn pooled_ordered(
    kernel: Kernel,
    args: &Value,
    machine: &ktpu_machine::Machine,
) -> R<Kernel> {
    let enabled = match args.get("pipeline_lookahead") {
        None | Some(Value::Bool(false)) => false,
        Some(Value::Bool(true)) => true,
        _ => return fail("API.OP.001", "pipeline_lookahead must be boolean"),
    };
    sequence::select(kernel, quant::Backend::Generator, enabled, true, machine)
        .map(|(kernel, _)| kernel)
}

/// Complete legal pooled/windowed alternative, before cost selection.
/// Callers comparing schedules must price this and the unmodified schedule.
pub fn pipeline_candidate(kernel: &Kernel, machine: &ktpu_machine::Machine) -> R<Kernel> {
    let (pooled, _) = reuse::candidate(kernel, true)?;
    lookahead::candidate(&pooled, quant::Backend::Generator, machine)
}
