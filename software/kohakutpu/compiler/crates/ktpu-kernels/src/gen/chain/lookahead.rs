//! Hoist independent input movers before a producer pipeline's final iteration.
//!
//! The proof uses actual allocation names after lifetime pooling. It is
//! deliberately buffer-conservative: only a tail's statically inactive regions
//! disappear. Shared reads may overlap, but RAW, WAR and WAW conflicts on any
//! remaining allocation are dependency barriers.

use std::collections::{BTreeMap, HashSet};

use ktpu_graph as g;
use ktpu_machine::Machine;

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::R;

fn integer(index: &g::Index, known: &BTreeMap<String, i64>) -> Option<i64> {
    fn atom(term: &g::Atom, known: &BTreeMap<String, i64>) -> Option<i64> {
        match term {
            g::Atom::Var(var) => known.get(var).copied(),
            g::Atom::Floor(a, b) => integer(a, known)?.checked_div_euclid(integer(b, known)?),
            g::Atom::Mod(a, b) => integer(a, known)?.checked_rem_euclid(integer(b, known)?),
            g::Atom::Mul(a, b) => integer(a, known)?.checked_mul(integer(b, known)?),
        }
    }
    match index {
        g::Index::Int(value) => Some(*value),
        g::Index::Sym(sym) => sym
            .terms
            .iter()
            .try_fold(sym.konst, |sum, (term, coefficient)| {
                sum.checked_add(atom(term, known)?.checked_mul(*coefficient)?)
            }),
    }
}

fn condition(cond: &g::Cond, known: &BTreeMap<String, i64>) -> Option<bool> {
    let a = integer(&cond.a, known)?;
    let b = integer(&cond.b, known)?;
    Some(match cond.op {
        g::CmpOp::Eq => a == b,
        g::CmpOp::Ne => a != b,
        g::CmpOp::Lt => a < b,
        g::CmpOp::Le => a <= b,
        g::CmpOp::Gt => a > b,
        g::CmpOp::Ge => a >= b,
    })
}

#[derive(Default)]
struct Access {
    reads: HashSet<String>,
    writes: HashSet<String>,
}

impl Access {
    fn independent(&self, later: &Self) -> bool {
        self.writes.is_disjoint(&later.reads)
            && self.reads.is_disjoint(&later.writes)
            && self.writes.is_disjoint(&later.writes)
    }
}

fn accesses(body: &[g::Stmt], known: &BTreeMap<String, i64>, access: &mut Access) -> bool {
    for stmt in body {
        match &stmt.node {
            g::Node::When { cond, body } => {
                if condition(cond, known) != Some(false) && !accesses(body, known, access) {
                    return false;
                }
            }
            g::Node::For {
                var, lo, hi, body, ..
            }
            | g::Node::Par {
                var, lo, hi, body, ..
            } => {
                let low = integer(lo, known);
                let high = integer(hi, known);
                if matches!((low, high), (Some(lo), Some(hi)) if lo >= hi) {
                    continue;
                }
                let mut nested = known.clone();
                nested.remove(var);
                if let (Some(lo), Some(hi)) = (low, high) {
                    if hi.checked_sub(lo) == Some(1) {
                        nested.insert(var.clone(), lo);
                    }
                }
                if !accesses(body, &nested, access) {
                    return false;
                }
            }
            g::Node::Sticky { .. } | g::Node::Send { .. } => return false,
            g::Node::Value {
                op: g::ValueOp::Recv,
                ..
            } => return false,
            g::Node::Value { args, kw, .. } => {
                // Opaque mappings may access more than an apparent source view.
                if !kw.is_empty() {
                    return false;
                }
                for arg in args {
                    if let g::Operand::View(view) = arg {
                        access.reads.insert(view.buffer.clone());
                    }
                }
            }
            g::Node::Store { dst, .. } => {
                access.writes.insert(dst.buffer.clone());
            }
            g::Node::Drain {
                dst: g::Operand::View(dst),
                add,
                node: None,
                ..
            } => {
                access.writes.insert(dst.buffer.clone());
                if *add {
                    access.reads.insert(dst.buffer.clone());
                }
            }
            g::Node::Drain { .. } => return false,
            g::Node::Mover {
                op, src, dst, kw, ..
            } => {
                if !matches!(
                    op,
                    g::MoverOp::Copy | g::MoverOp::Fill | g::MoverOp::Quant | g::MoverOp::Gt4
                ) {
                    return false;
                }
                access.writes.insert(dst.buffer.clone());
                if let Some(src) = src {
                    access.reads.insert(src.buffer.clone());
                }
                for (key, value) in kw {
                    match (key.as_str(), value) {
                        ("status", g::Attr::View(view)) => {
                            access.writes.insert(view.buffer.clone());
                        }
                        (_, g::Attr::View(_) | g::Attr::Tuple(_)) => return false,
                        ("read_map", _) => return false,
                        _ => {}
                    }
                }
            }
        }
    }
    true
}

fn tail_access(loop_stmt: &g::Stmt) -> Option<Access> {
    let g::Node::For {
        var,
        lo: g::Index::Int(lo),
        hi: g::Index::Int(hi),
        pipe: Some(_),
        body,
    } = &loop_stmt.node
    else {
        return None;
    };
    if hi.checked_sub(*lo).is_none_or(|trips| trips < 2) {
        return None;
    }
    let mut tail = Access::default();
    accesses(body, &BTreeMap::from([(var.clone(), *hi - 1)]), &mut tail).then_some(tail)
}

/// Dependency proof for already-valid graph regions; no schedule or cost is
/// changed here. Shared reads are allowed, but writes to a live packed input,
/// finite-status alias, remote traffic and opaque effects prevent motion.
pub fn independent_tail(loop_stmt: &g::Stmt, later: &[g::Stmt]) -> bool {
    let Some(tail) = tail_access(loop_stmt) else {
        return false;
    };
    let mut next = Access::default();
    accesses(later, &BTreeMap::new(), &mut next) && tail.independent(&next)
}

fn mover(stmt: &g::Stmt) -> bool {
    matches!(
        &stmt.node,
        g::Node::Mover {
            op: g::MoverOp::Copy | g::MoverOp::Fill | g::MoverOp::Quant,
            ..
        }
    )
}

pub(super) fn fresh(stmt: &g::Stmt, taken: &mut HashSet<String>) -> R<g::Stmt> {
    let mut value = serde_json::to_value(stmt)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    let mut own = HashSet::new();
    super::bound_names(std::slice::from_ref(stmt), &mut own);
    let prefix = (0..)
        .map(|i| format!("lookahead{i}_"))
        .find(|p| own.iter().all(|n| !taken.contains(&format!("{p}{n}"))))
        .expect("fresh pipeline tail scope");
    super::renamed(&mut value, &own, &|n| format!("{prefix}{n}"));
    taken.extend(own.iter().map(|n| format!("{prefix}{n}")));
    serde_json::from_value(value)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))
}

fn schedule(body: &mut Vec<g::Stmt>, taken: &mut HashSet<String>) -> R<bool> {
    let mut changed = false;
    for stmt in body.iter_mut() {
        if let g::Node::Par { unit, body, .. } = &mut stmt.node {
            if unit.kind == g::UnitKind::Node {
                changed |= schedule(body, taken)?;
            }
        }
    }
    let mut at = 0;
    while at < body.len() {
        let g::Node::For {
            lo: g::Index::Int(lo),
            hi: g::Index::Int(hi),
            pipe: Some(_),
            ..
        } = &body[at].node
        else {
            at += 1;
            continue;
        };
        if hi.checked_sub(*lo).is_none_or(|trips| trips < 2) {
            at += 1;
            continue;
        }
        let last = *hi - 1;
        let Some(tail) = tail_access(&body[at]) else {
            at += 1;
            continue;
        };
        let mut next = Access::default();
        let mut end = at + 1;
        while end < body.len() && mover(&body[end]) {
            if !accesses(
                std::slice::from_ref(&body[end]),
                &BTreeMap::new(),
                &mut next,
            ) || !tail.independent(&next)
            {
                break;
            }
            end += 1;
        }
        if end == at + 1 {
            at += 1;
            continue;
        }
        let mut suffix = fresh(&body[at], taken)?;
        let g::Node::For { lo, .. } = &mut suffix.node else {
            unreachable!()
        };
        *lo = g::Index::Int(last);
        let g::Node::For { hi, .. } = &mut body[at].node else {
            unreachable!()
        };
        *hi = g::Index::Int(last);
        body.insert(end, suffix);
        changed = true;
        at = end + 1;
    }
    Ok(changed)
}

/// Move independent input transfers across compute regions using actual pooled
/// allocations. Output transfers are never candidates; shared public output
/// ordering remains visible. Unknown effects and status checks are barriers.
pub fn hoist_inputs(body: &mut Vec<g::Stmt>, outputs: &HashSet<String>) -> bool {
    let mut changed = false;
    for stmt in body.iter_mut() {
        if let g::Node::Par { unit, body, .. } = &mut stmt.node {
            if unit.kind == g::UnitKind::Node {
                changed |= hoist_inputs(body, outputs);
            }
        }
    }
    for index in 1..body.len() {
        let g::Node::Mover { dst, op, .. } = &body[index].node else {
            continue;
        };
        if outputs.contains(&dst.buffer)
            || !matches!(
                op,
                g::MoverOp::Copy | g::MoverOp::Fill | g::MoverOp::Quant | g::MoverOp::Gt4
            )
        {
            continue;
        }
        let mut moving = Access::default();
        if !accesses(
            std::slice::from_ref(&body[index]),
            &BTreeMap::new(),
            &mut moving,
        ) {
            continue;
        }
        let mut first = index;
        while first > 0 {
            let mut previous = Access::default();
            if !accesses(
                std::slice::from_ref(&body[first - 1]),
                &BTreeMap::new(),
                &mut previous,
            ) || !previous.independent(&moving)
            {
                break;
            }
            first -= 1;
        }
        // Pure mover permutation cannot overlap units; avoid gratuitous churn.
        if first < index
            && body[first..index]
                .iter()
                .any(|stmt| !matches!(stmt.node, g::Node::Mover { .. }))
        {
            let stmt = body.remove(index);
            body.insert(first, stmt);
            changed = true;
        }
    }
    changed
}

/// Start ready compute before transfers used only by later compute. Memory
/// effects use the same pooled-allocation proof as input hoisting; public
/// output transfers and opaque effects remain ordering barriers.
pub fn advance_compute(body: &mut Vec<g::Stmt>, outputs: &HashSet<String>) -> bool {
    let mut changed = false;
    for stmt in body.iter_mut() {
        if let g::Node::Par { unit, body, .. } = &mut stmt.node {
            if unit.kind == g::UnitKind::Node {
                changed |= advance_compute(body, outputs);
            }
        }
    }
    for index in 1..body.len() {
        if !matches!(body[index].node, g::Node::Par { .. } | g::Node::For { .. }) {
            continue;
        }
        let mut compute = Access::default();
        if !accesses(
            std::slice::from_ref(&body[index]),
            &BTreeMap::new(),
            &mut compute,
        ) {
            continue;
        }
        let mut first = index;
        while first > 0 {
            let g::Node::Mover { dst, .. } = &body[first - 1].node else {
                break;
            };
            if outputs.contains(&dst.buffer) {
                break;
            }
            let mut transfer = Access::default();
            if !accesses(
                std::slice::from_ref(&body[first - 1]),
                &BTreeMap::new(),
                &mut transfer,
            ) || !transfer.independent(&compute)
            {
                break;
            }
            first -= 1;
        }
        if first < index {
            let stmt = body.remove(index);
            body.insert(first, stmt);
            changed = true;
        }
    }
    changed
}

/// Price both complete schedules. Dependencies and storage are already fixed;
/// no per-value guard or extra mover is introduced by this optional ordering.
pub(crate) fn kernel(kernel: Kernel, machine: &Machine) -> R<Kernel> {
    finalized(kernel, super::quant::Backend::Generator, machine)
}

/// Schedule pending quant operations before assigning private finite-status
/// slices. Both alternatives are decorated before complete-schedule pricing;
/// explicit statuses already present in the input are never rewritten.
pub(super) fn finalized(
    kernel: Kernel,
    backend: super::quant::Backend,
    machine: &Machine,
) -> R<Kernel> {
    let candidate = candidate(&kernel, backend, machine)?;
    let kernel = super::quant::kernel(kernel, backend, machine)?;
    let before = ktpu_graph::cycles(&kernel.graph, machine)?;
    match ktpu_graph::cycles(&candidate.graph, machine) {
        Ok(after) if after <= before => Ok(candidate),
        _ => Ok(kernel),
    }
}

/// Construct the complete ordered schedule before comparing storage alternatives.
pub(super) fn candidate(
    kernel: &Kernel,
    backend: super::quant::Backend,
    machine: &Machine,
) -> R<Kernel> {
    candidate_priced(kernel, backend, machine, &mut |graph| {
        ktpu_graph::cycles(graph, machine)
    })
}

pub(super) fn candidate_priced(
    kernel: &Kernel,
    backend: super::quant::Backend,
    machine: &Machine,
    price: &mut impl FnMut(&ktpu_graph::Graph) -> R<i64>,
) -> R<Kernel> {
    let mut candidate = kernel.clone();
    let mut taken: HashSet<_> = kernel.graph.ports.iter().map(|p| p.name.clone()).collect();
    taken.extend(kernel.graph.scratch.iter().map(|p| p.name.clone()));
    let body = serde_json::to_value(&kernel.graph.body)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    super::names_in(&body, &super::NAMED, &mut taken);
    let windows = super::windows::apply(&mut candidate, &mut taken)?;
    let lookahead = schedule(&mut candidate.graph.body, &mut taken)?;
    let outputs = kernel
        .graph
        .ports
        .iter()
        .filter(|port| port.role == g::Role::Out)
        .map(|port| port.name.clone())
        .collect();
    let transfers = hoist_inputs(&mut candidate.graph.body, &outputs);
    if windows || lookahead || transfers {
        candidate.graph.name = name("pipeline_lookahead", vec![Py::str(&kernel.graph.name)]);
    }
    let mut ready = candidate.clone();
    if !advance_compute(&mut ready.graph.body, &outputs) {
        return super::quant::kernel(candidate, backend, machine);
    }
    ready.graph.name = name("ready_compute", vec![Py::str(&candidate.graph.name)]);
    let candidate = super::quant::kernel(candidate, backend, machine)?;
    let ready = super::quant::kernel(ready, backend, machine)?;
    match (price(&candidate.graph), price(&ready.graph)) {
        (Ok(before), Ok(after)) if after <= before => Ok(ready),
        _ => Ok(candidate),
    }
}
