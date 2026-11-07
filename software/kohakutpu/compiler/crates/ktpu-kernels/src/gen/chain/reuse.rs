//! Same-owner stage allocations with disjoint logical lifetimes (L2.PASS.004).
//!
//! Aliases are represented in the graph before dependency lowering, so reuse
//! carries the WAR/WAW ordering that protects the previous occupant's readers.

use std::collections::BTreeMap;

use ktpu_graph as g;
use ktpu_machine::Machine;

use crate::kernel::Kernel;
use crate::trace::R;

#[derive(Clone, Copy)]
struct Life {
    first: usize,
    last: usize,
    owner: Option<i64>,
}

fn physical(var: &str, lo: &g::Index, hi: &g::Index, unit: &g::UnitRef) -> Option<i64> {
    let (g::Index::Int(lo), g::Index::Int(hi)) = (lo, hi) else {
        return None;
    };
    if hi.checked_sub(*lo) != Some(1) {
        return None;
    }
    match unit.index.as_ref()? {
        g::Index::Int(id) => Some(*id),
        g::Index::Sym(s) if s.konst == 0 && s.terms == [(g::Atom::Var(var.to_string()), 1)] => {
            Some(*lo)
        }
        _ => None,
    }
}

fn nested_node(body: &[g::Stmt]) -> bool {
    body.iter().any(|stmt| {
        matches!(&stmt.node, g::Node::Par { unit, .. } if unit.kind == g::UnitKind::Node)
            || stmt.node.body().is_some_and(nested_node)
    })
}

fn lifetimes(
    body: &mut [g::Stmt],
    owner: Option<i64>,
    clock: &mut usize,
    lives: &mut BTreeMap<String, Life>,
) {
    for stmt in body {
        if let g::Node::Par {
            var,
            lo,
            hi,
            unit,
            body,
            ..
        } = &mut stmt.node
        {
            if unit.kind == g::UnitKind::Node {
                let node = physical(var, lo, hi, unit);
                lifetimes(body, node, clock, lives);
                continue;
            }
        }
        let at = *clock;
        *clock += 1;
        // A loop is one lifetime interval, including every pipeline iteration.
        let owner = if nested_node(std::slice::from_ref(stmt)) {
            None
        } else {
            owner
        };
        super::rows::views(std::slice::from_mut(stmt), &mut |view| {
            lives
                .entry(view.buffer.clone())
                .and_modify(|life| {
                    life.last = at;
                    if life.owner != owner {
                        life.owner = None;
                    }
                })
                .or_insert(Life {
                    first: at,
                    last: at,
                    owner,
                });
        });
    }
}

fn resizable(body: &mut [g::Stmt], bounded: &mut BTreeMap<String, bool>) {
    for stmt in body {
        match &mut stmt.node {
            g::Node::Par { body, .. }
            | g::Node::For { body, .. }
            | g::Node::When { body, .. }
            | g::Node::Sticky { body } => resizable(body, bounded),
            _ => {
                let ordinary = match &stmt.node {
                    g::Node::Value { kw, .. } => kw.is_empty(),
                    g::Node::Mover { op, kw, .. } => {
                        *op == g::MoverOp::Quant
                            && !kw.iter().any(|(key, value)| {
                                key == "read_map" || matches!(value, g::Attr::Walk(_))
                            })
                    }
                    g::Node::Store { .. } | g::Node::Drain { .. } => true,
                    _ => false,
                };
                super::rows::views(std::slice::from_mut(stmt), &mut |view| {
                    let plain =
                        ordinary && !view.axes.iter().any(|axis| matches!(axis, g::Axis::New));
                    bounded
                        .entry(view.buffer.clone())
                        .and_modify(|all| *all &= plain)
                        .or_insert(plain);
                });
            }
        }
    }
}

fn contains(outer: &g::Spec, inner: &g::Spec) -> bool {
    outer.dtype == inner.dtype
        && outer.space == inner.space
        && outer.layout == inner.layout
        && outer.shape.len() == inner.shape.len()
        && outer.shape.iter().zip(&inner.shape).all(|(a, b)| a >= b)
}

/// Reuse compatibly laid-out private stage allocations only when their entire
/// lifetimes are disjoint on one physical sysnode. Ports are never candidates.
/// The complete emitted schedule, including added dependencies, must not cost
/// more than the schedule with separate allocations.
pub(crate) fn kernel(kernel: Kernel, machine: &Machine) -> R<Kernel> {
    mapped(kernel, machine, true).map(|(kernel, _)| kernel)
}

/// Expose selected aliases to a planner that retains private access manifests.
/// Equal-capacity pooling preserves every region's byte coordinate; resizing
/// is reserved for callers rebuilding views and manifests together.
pub(crate) fn mapped(
    kernel: Kernel,
    machine: &Machine,
    resize_capacities: bool,
) -> R<(Kernel, BTreeMap<String, String>)> {
    let (candidate, aliases) = candidate(&kernel, resize_capacities)?;
    if aliases.is_empty() {
        return Ok((kernel, aliases));
    }
    let before = ktpu_graph::cycles(&kernel.graph, machine)?;
    match ktpu_graph::cycles(&candidate.graph, machine) {
        Ok(after) if after <= before => Ok((candidate, aliases)),
        _ => Ok((kernel, BTreeMap::new())),
    }
}

/// Construct legal pooled storage independently of schedule selection.
pub(super) fn candidate(
    kernel: &Kernel,
    resize_capacities: bool,
) -> R<(Kernel, BTreeMap<String, String>)> {
    let mut candidate = kernel.clone();
    let mut lives = BTreeMap::new();
    let mut clock = 0;
    lifetimes(&mut candidate.graph.body, Some(0), &mut clock, &mut lives);
    let mut bounded = BTreeMap::new();
    resizable(&mut candidate.graph.body, &mut bounded);
    let mut order: Vec<_> = candidate
        .graph
        .scratch
        .iter()
        .filter_map(|scratch| {
            let life = *lives.get(&scratch.name)?;
            (scratch.spec.space == g::BufSpace::Stage && life.owner.is_some())
                .then_some((scratch.clone(), life))
        })
        .collect();
    order.sort_by_key(|(_, life)| life.first);
    let mut slots: Vec<(g::Scratch, Life, bool)> = Vec::new();
    let mut aliases = BTreeMap::new();
    for (scratch, life) in order {
        let can_resize = resize_capacities && bounded.get(&scratch.name) == Some(&true);
        if let Some((slot, occupied, resize)) = slots.iter_mut().find(|(slot, occupied, resize)| {
            (slot.spec == scratch.spec
                || *resize
                    && can_resize
                    && (contains(&slot.spec, &scratch.spec) || contains(&scratch.spec, &slot.spec)))
                && occupied.owner == life.owner
                && occupied.last < life.first
        }) {
            if contains(&scratch.spec, &slot.spec) {
                slot.spec = scratch.spec.clone();
            }
            aliases.insert(scratch.name, slot.name.clone());
            occupied.last = life.last;
            *resize &= can_resize;
        } else {
            slots.push((scratch, life, can_resize));
        }
    }
    if aliases.is_empty() {
        return Ok((kernel.clone(), BTreeMap::new()));
    }
    let allocations: BTreeMap<_, _> = slots
        .iter()
        .map(|(slot, _, _)| (slot.name.clone(), slot.spec.clone()))
        .collect();
    let original: BTreeMap<_, _> = kernel
        .graph
        .scratch
        .iter()
        .map(|scratch| (scratch.name.clone(), scratch.spec.clone()))
        .collect();
    super::rows::views(&mut candidate.graph.body, &mut |view| {
        let name = aliases.get(&view.buffer).unwrap_or(&view.buffer);
        if let (Some(before), Some(after)) = (original.get(&view.buffer), allocations.get(name)) {
            if before.shape != after.shape {
                for (axis, &extent) in before.shape.iter().enumerate() {
                    if axis == view.axes.len() {
                        view.axes.push(g::Axis::Run(g::Index::Int(0), extent));
                    } else if view.axes[axis] == g::Axis::All {
                        view.axes[axis] = g::Axis::Run(g::Index::Int(0), extent);
                    }
                }
            }
        }
        view.buffer = name.clone();
    });
    for scratch in &mut candidate.graph.scratch {
        if let Some(spec) = allocations.get(&scratch.name) {
            scratch.spec = spec.clone();
        }
    }
    candidate
        .graph
        .scratch
        .retain(|s| !aliases.contains_key(&s.name));
    candidate.graph.name = crate::pyrepr::name(
        if resize_capacities {
            "shared_stage"
        } else {
            "shared_stage_exact"
        },
        vec![crate::pyrepr::Py::str(&kernel.graph.name)],
    );
    Ok((candidate, aliases))
}
