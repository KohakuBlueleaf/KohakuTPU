//! Deferred checked vector quantization within a generated node schedule.
//!
//! A straight-line quant owns one status slice until the schedule's final
//! check. Quant operations nested in loops retain their emitter's status and
//! pipeline policy (L2.MOV.010, L2.MOV.018).

use std::collections::HashSet;

use ktpu_graph as g;
use ktpu_machine::Machine;

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::{fail, R};

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Backend {
    #[default]
    Generator,
    Vector,
}

impl Backend {
    pub fn read(arguments: &serde_json::Value) -> R<Self> {
        match arguments.get("quant_backend") {
            None => Ok(Self::Generator),
            Some(serde_json::Value::String(s)) if s == "mover_quant" => Ok(Self::Generator),
            Some(serde_json::Value::String(s)) if s == "vector_mx7" => Ok(Self::Vector),
            Some(value) => fail("API.OP.001", format!("unknown quant backend {value}")),
        }
    }
}

const SCHEDULE: &[(&str, i64)] = &[
    ("finite_batch", 4),
    ("overlap", 1),
    ("pipeline", 1),
    ("zero_classifier", 1),
    ("stream_axes", 1),
    ("fill_overlap", 1),
    ("zero_reuse", 1),
];

fn pending(stmt: &g::Stmt) -> bool {
    matches!(&stmt.node, g::Node::Mover { op: g::MoverOp::Quant, kw, .. }
        if !kw.iter().any(|(key, _)| key == "status"))
}

fn scope(
    body: &mut Vec<g::Stmt>,
    scratch: &mut Vec<g::Scratch>,
    taken: &mut HashSet<String>,
    record: i64,
) -> bool {
    let mut changed = false;
    // Node regions are distinct schedules with private status storage. Other
    // control regions keep the quant emitter's lifetime and finite checks.
    for stmt in body.iter_mut() {
        if let g::Node::Par { unit, body, .. } = &mut stmt.node {
            if unit.kind == g::UnitKind::Node {
                changed |= scope(body, scratch, taken, record);
            }
        }
    }
    let count = body.iter().filter(|s| pending(s)).count() as i64;
    if count == 0 {
        return changed;
    }
    let status = (0..)
        .map(|i| format!("quant_status_{i}"))
        .find(|n| taken.insert(n.clone()))
        .expect("an unused generated buffer name");
    scratch.push(g::Scratch {
        name: status.clone(),
        spec: g::Spec {
            shape: vec![count * record],
            dtype: g::Dtype::F16,
            layout: vec![],
            space: g::BufSpace::Stage,
        },
    });
    for (index, stmt) in body.iter_mut().filter(|s| pending(s)).enumerate() {
        if let g::Node::Mover { kw, .. } = &mut stmt.node {
            kw.retain(|(key, _)| key != "backend");
            kw.push(("backend".into(), g::Attr::Str("vector_mx7".into())));
            kw.push((
                "status".into(),
                g::Attr::View(g::View {
                    buffer: status.clone(),
                    axes: vec![g::Axis::Run(g::Index::Int(index as i64 * record), record)],
                }),
            ));
            for &(key, value) in SCHEDULE {
                if !kw.iter().any(|(present, _)| present == key) {
                    kw.push((key.into(), g::Attr::Int(value)));
                }
            }
        }
    }
    body.push(g::Stmt {
        node: g::Node::Mover {
            op: g::MoverOp::QuantCheck,
            dst: g::View {
                buffer: status,
                axes: vec![g::Axis::All],
            },
            src: None,
            flags: vec![],
            kw: vec![],
        },
        at: "<generated quant schedule>".into(),
    });
    true
}

/// Apply the selected straight-line quant schedule before complete-schedule
/// pricing. An absent policy preserves the emitted graph and its cache key.
pub fn kernel(mut kernel: Kernel, backend: Backend, machine: &Machine) -> R<Kernel> {
    if backend == Backend::Generator {
        return Ok(kernel);
    }
    let mut taken: HashSet<_> = kernel.graph.ports.iter().map(|p| p.name.clone()).collect();
    taken.extend(kernel.graph.scratch.iter().map(|s| s.name.clone()));
    let record = ktpu_machine::packing::quant_status_bytes(machine.quant_cores()) / 2;
    if scope(
        &mut kernel.graph.body,
        &mut kernel.graph.scratch,
        &mut taken,
        record,
    ) {
        kernel.graph.name = name(
            "quant_schedule",
            vec![Py::str(&kernel.graph.name), Py::str("vector_mx7")],
        );
    }
    Ok(kernel)
}
