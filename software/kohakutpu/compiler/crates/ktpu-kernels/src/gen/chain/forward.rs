//! Pointwise consumers of a producer's stored FP16 value within its VC stream.
//!
//! The stored pack remains the readback destination. Unpacking that exact pack
//! preserves the materialization boundary without a second memory load or launch.

use std::collections::{HashMap, HashSet};

use ktpu_graph as g;
use ktpu_machine::Machine;

use crate::expr;
use crate::trace::buffer::f16;
use crate::trace::control::At;
use crate::trace::{Arg, Facts, Trace, R};

use super::wave::Consumer;

fn block(
    source: &str,
    ty: &g::ValueType,
    destination: &g::View,
    consumer: &Consumer,
    taken: &mut HashSet<String>,
    machine: &Machine,
) -> R<Option<Vec<g::Stmt>>> {
    let [g::Axis::Run(_, rows), g::Axis::Run(_, columns)] = destination.axes.as_slice() else {
        return Ok(None);
    };
    if ty.dtype != g::Dtype::F16
        || ty.space != g::ValSpace::L1
        || ty.mode != Some(g::Mode::Gt4)
        || ty.shape != [*rows, *columns]
    {
        return Ok(None);
    }
    let mut trace = Trace::new("rounded_consumer", Facts::of(machine), &machine.die.target);
    let spec = f16(&[*rows, *columns])?;
    let input = trace.port("src", g::Role::In, spec.clone())?;
    let output = trace.port("dst", g::Role::Out, spec)?;
    trace.par(1, g::UnitKind::Vc, At::Var, |trace, _| {
        let packed = trace.load(&input.whole(), true)?;
        let value = expr::emit(
            trace,
            &consumer.expression,
            &HashMap::from([(0, Arg::Val(packed))]),
        )?;
        trace.store(&output.whole(), value)
    })?;
    let graph = trace.graph()?;
    let Some(g::Stmt {
        node: g::Node::Par { body, .. },
        ..
    }) = graph.body.first()
    else {
        unreachable!("one consumer VC region")
    };
    let mut body = body.clone();
    let Some(g::Stmt {
        node:
            g::Node::Value {
                target,
                ty: loaded,
                op: g::ValueOp::Load,
                ..
            },
        ..
    }) = body.first()
    else {
        unreachable!("the consumer input load")
    };
    if loaded != ty {
        return Ok(None);
    }
    let load = target.clone();
    body.remove(0);
    let mut names = HashSet::new();
    super::bound_names(&body, &mut names);
    let prefix = (0..)
        .map(|i| format!("rounded{i}_"))
        .find(|prefix| {
            names
                .iter()
                .all(|name| !taken.contains(&format!("{prefix}{name}")))
        })
        .expect("fresh consumer values");
    let mut value = serde_json::to_value(&body)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    super::renamed(&mut value, &names, &|name| format!("{prefix}{name}"));
    super::renamed(&mut value, &HashSet::from([load]), &|_| source.to_string());
    taken.extend(names.into_iter().map(|name| format!("{prefix}{name}")));
    body = serde_json::from_value(value)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    super::rows::views(&mut body, &mut |view| *view = destination.clone());
    Ok(Some(body))
}

/// Only pure producer regions participate. A complete-store proof at the chain
/// boundary ensures that no consumer or other reader observes an incomplete wave.
pub(super) fn embed(
    body: &mut [g::Stmt],
    edge: &str,
    output: &str,
    consumer: &Consumer,
    taken: &mut HashSet<String>,
    machine: &Machine,
) -> R<usize> {
    fn region(
        body: &mut Vec<g::Stmt>,
        edge: &str,
        output: &str,
        consumer: &Consumer,
        taken: &mut HashSet<String>,
        machine: &Machine,
    ) -> R<usize> {
        let mut matched = 0;
        for stmt in body.iter_mut() {
            match &mut stmt.node {
                g::Node::For { body, .. } | g::Node::When { body, .. } => {
                    matched += region(body, edge, output, consumer, taken, machine)?;
                }
                _ => {}
            }
        }
        let Some(g::Stmt {
            node: g::Node::Store { dst, src },
            ..
        }) = body.last()
        else {
            return Ok(matched);
        };
        if dst.buffer != edge {
            return Ok(matched);
        }
        let Some(ty) = body.iter().find_map(|stmt| match &stmt.node {
            g::Node::Value {
                op: g::ValueOp::Pack,
                target,
                ty,
                ..
            } if target == src => Some(ty),
            _ => None,
        }) else {
            return Ok(matched);
        };
        let mut destination = dst.clone();
        destination.buffer = output.into();
        if let Some(consumer_body) = block(src, ty, &destination, consumer, taken, machine)? {
            body.extend(consumer_body);
            matched += 1;
        }
        Ok(matched)
    }

    let mut matched = 0;
    for stmt in body {
        match &mut stmt.node {
            g::Node::Par { unit, body, .. } if unit.kind == g::UnitKind::Node => {
                matched += embed(body, edge, output, consumer, taken, machine)?;
            }
            g::Node::For {
                pipe: Some(_),
                body,
                ..
            } => {
                for stmt in body {
                    if let g::Node::Par { unit, body, .. } = &mut stmt.node {
                        if unit.kind == g::UnitKind::Vc && super::wave::pure(body, edge) {
                            matched += region(body, edge, output, consumer, taken, machine)?;
                        }
                    }
                }
            }
            _ => {}
        }
    }
    Ok(matched)
}
