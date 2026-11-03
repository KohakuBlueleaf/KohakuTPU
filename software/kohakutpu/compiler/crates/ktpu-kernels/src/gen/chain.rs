//! Two kernels as one (`kernel.chain`): the first's statements, then the
//! second's, the second's input port `from` read from the first's output port
//! `to`. With `keep` that output stays a port, written to DRAM for later
//! readers; without it, it is a `@mem` scratch the `space` pass places in
//! staging where it fits (L2.PASS.004), never stored to DRAM.

use std::collections::HashSet;

use ktpu_graph as g;
use serde_json::Value;

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::{fail, R};

/// The prefix every name of the second kernel takes.
const SECOND: &str = "c2_";

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
    names_in(&body, &["target", "var"], &mut own);
    let link = to.to_string();
    let to_name = |n: &str| match n == from {
        true => link.clone(),
        false => format!("{SECOND}{n}"),
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
