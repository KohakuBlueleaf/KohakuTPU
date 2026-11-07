//! Quantize completed producer columns before its final pipeline wave.
//!
//! A packed activation buffer may be reused by two products. The final producer
//! wave still reads its input prefix, so only columns beyond that prefix and
//! before the final output band can be quantized early. No status-bearing quant
//! is split: its explicit lifetime remains with the original emitter.

use std::collections::{BTreeMap, HashSet};

use crate::kernel::Kernel;
use crate::trace::R;
use ktpu_graph as g;

use super::index::interval;

type Bounds = BTreeMap<String, (i64, i64)>;

#[derive(Default)]
struct Access {
    writes: Vec<(i64, i64, i64, i64)>,
    input_end: i64,
}

fn rectangle(view: &g::View, shape: &[i64], bounds: &Bounds) -> Option<(i64, i64, i64, i64)> {
    if shape.len() != 2 || view.axes.len() > 2 {
        return None;
    }
    let dimension = |axis: Option<&g::Axis>, length: i64| {
        let (lo, hi) = match axis {
            None | Some(g::Axis::All) => (0, length),
            Some(g::Axis::Run(index, size)) if *size > 0 => {
                let (lo, hi) = interval(index, bounds)?;
                (lo, hi.checked_add(*size)?)
            }
            _ => return None,
        };
        (lo >= 0 && hi <= length).then_some((lo, hi))
    };
    let (r0, r1) = dimension(view.axes.first(), shape[0])?;
    let (c0, c1) = dimension(view.axes.get(1), shape[1])?;
    Some((r0, r1, c0, c1))
}

fn dense_rectangle(view: &g::View, shape: &[i64], bounds: &Bounds) -> Option<(i64, i64, i64, i64)> {
    let [g::Axis::Run(row, rows), g::Axis::Run(col, columns)] = &view.axes[..] else {
        return None;
    };
    let (r0, r1, row_vars) = super::index::dense(row, *rows, bounds)?;
    let (c0, c1, col_vars) = super::index::dense(col, *columns, bounds)?;
    (row_vars.is_disjoint(&col_vars) && r0 >= 0 && r1 <= shape[0] && c0 >= 0 && c1 <= shape[1])
        .then_some((r0, r1, c0, c1))
}

fn accesses(
    body: &[g::Stmt],
    source: &str,
    destination: &str,
    shape: &[i64],
    bounds: &Bounds,
    result: &mut Access,
) -> bool {
    for stmt in body {
        match &stmt.node {
            g::Node::For {
                var, lo, hi, body, ..
            }
            | g::Node::Par {
                var, lo, hi, body, ..
            } => {
                let (g::Index::Int(lo), g::Index::Int(hi)) = (lo, hi) else {
                    return false;
                };
                let (lo, hi) = (*lo, *hi);
                if hi <= lo {
                    continue;
                }
                let mut nested = bounds.clone();
                nested.insert(var.clone(), (lo, hi - 1));
                if !accesses(body, source, destination, shape, &nested, result) {
                    return false;
                }
            }
            // An unresolved guard could leave holes in the producer domain.
            g::Node::When { .. } | g::Node::Sticky { .. } | g::Node::Send { .. } => return false,
            g::Node::Value {
                op: g::ValueOp::Recv,
                ..
            } => return false,
            g::Node::Value { kw, .. } if !kw.is_empty() => return false,
            g::Node::Mover { .. } => return false,
            _ => {
                let written = match &stmt.node {
                    g::Node::Store { dst, .. } => Some(dst),
                    g::Node::Drain {
                        dst: g::Operand::View(dst),
                        ..
                    } => Some(dst),
                    _ => None,
                };
                if written.is_some_and(|v| v.buffer == destination) {
                    return false;
                }
                if let Some(view) = written.filter(|v| v.buffer == source) {
                    let Some(rect) = dense_rectangle(view, shape, bounds) else {
                        return false;
                    };
                    result.writes.push(rect);
                }
                let mut valid = true;
                let mut copy = stmt.clone();
                super::rows::views(std::slice::from_mut(&mut copy), &mut |view| {
                    if view.buffer == destination {
                        match rectangle(view, shape, bounds) {
                            Some((_, _, _, end)) => result.input_end = result.input_end.max(end),
                            None => valid = false,
                        }
                    }
                });
                if !valid {
                    return false;
                }
            }
        }
    }
    true
}

fn quant_part(stmt: &g::Stmt, first: i64, end: i64) -> g::Stmt {
    let mut part = stmt.clone();
    let g::Node::Mover {
        src: Some(src),
        dst,
        ..
    } = &mut part.node
    else {
        unreachable!()
    };
    for view in [src, dst] {
        view.axes.resize(2, g::Axis::All);
        view.axes[1] = g::Axis::Run(g::Index::Int(first), end - first);
    }
    part
}

fn transform(
    body: &mut Vec<g::Stmt>,
    specs: &BTreeMap<String, g::Spec>,
    taken: &mut HashSet<String>,
) -> R<bool> {
    let mut changed = false;
    for stmt in body.iter_mut() {
        if let g::Node::Par { unit, body, .. } = &mut stmt.node {
            if unit.kind == g::UnitKind::Node {
                changed |= transform(body, specs, taken)?;
            }
        }
    }
    let mut at = 0;
    while at + 1 < body.len() {
        let g::Node::For {
            var,
            lo: g::Index::Int(0),
            hi: g::Index::Int(hi),
            pipe: Some(1),
            body: producer,
        } = &body[at].node
        else {
            at += 1;
            continue;
        };
        let g::Node::Mover {
            op: g::MoverOp::Quant,
            src: Some(src),
            dst,
            flags,
            kw,
        } = &body[at + 1].node
        else {
            at += 1;
            continue;
        };
        let (Some(source), Some(destination)) = (specs.get(&src.buffer), specs.get(&dst.buffer))
        else {
            at += 1;
            continue;
        };
        if *hi <= 3
            || source.dtype != g::Dtype::F16
            || destination.dtype != g::Dtype::Mx7a
            || source.shape != destination.shape
            || source.shape.len() != 2
            || source.shape[0] % 4 != 0
            || !source.layout.is_empty()
            || src.buffer == dst.buffer
            || !flags.is_empty()
            || kw.iter().any(|(key, value)| {
                key == "status" || key == "read_map" || matches!(value, g::Attr::Walk(_))
            })
        {
            at += 1;
            continue;
        }
        if destination.layout.len() != 2
            || !["gm", "nk"].iter().all(|expected| {
                destination.layout.iter().any(|(key, value)| {
                    key.as_str() == *expected && matches!(value, g::LayoutValue::Int(n) if *n > 0)
                })
            })
        {
            at += 1;
            continue;
        }
        let packing =
            destination
                .layout
                .iter()
                .find_map(|(key, value)| match (key.as_str(), value) {
                    ("nk", g::LayoutValue::Int(nk)) if *nk > 0 => nk.checked_mul(32),
                    _ => None,
                });
        let Some(packing) = packing else {
            at += 1;
            continue;
        };
        let shape = &source.shape;
        let full = Some((0, shape[0], 0, shape[1]));
        if rectangle(src, shape, &Bounds::new()) != full
            || rectangle(dst, shape, &Bounds::new()) != full
        {
            at += 1;
            continue;
        }
        let last = *hi - 1;
        let mut tail = Access::default();
        let mut prefix = Access::default();
        if !accesses(
            producer,
            &src.buffer,
            &dst.buffer,
            shape,
            &Bounds::from([(var.clone(), (last, last))]),
            &mut tail,
        ) || !accesses(
            producer,
            &src.buffer,
            &dst.buffer,
            shape,
            &Bounds::from([(var.clone(), (0, last - 1))]),
            &mut prefix,
        ) {
            at += 1;
            continue;
        }
        // Multiple materialized pointwise stores may cover the same rectangle;
        // they all finish before the early quant. Distinct rectangles retain
        // the ordinary schedule rather than merging uncertain coverage.
        tail.writes.sort_unstable();
        tail.writes.dedup();
        prefix.writes.sort_unstable();
        prefix.writes.dedup();
        // One dense rectangle spanning all rows and a contiguous prefix/tail
        // column envelope. Packed cuts are complete MX7 K blocks.
        let ([(r0, r1, mid, end)], [(p0, p1, begin, past)]) =
            (tail.writes.as_slice(), prefix.writes.as_slice())
        else {
            at += 1;
            continue;
        };
        let low = tail.input_end;
        if *r0 != 0
            || *r1 != shape[0]
            || *p0 != 0
            || *p1 != shape[0]
            || *begin != 0
            || *past != *mid
            || *end != shape[1]
            || low <= 0
            || low >= *mid
            || *mid >= *end
            || low % packing != 0
            || mid % packing != 0
            || end % packing != 0
        {
            at += 1;
            continue;
        }
        let mut final_wave = super::lookahead::fresh(&body[at], taken)?;
        let g::Node::For { lo, .. } = &mut final_wave.node else {
            unreachable!()
        };
        *lo = g::Index::Int(last);
        let middle = quant_part(&body[at + 1], low, *mid);
        let bottom = quant_part(&body[at + 1], 0, low);
        let top = quant_part(&body[at + 1], *mid, *end);
        let g::Node::For { hi, .. } = &mut body[at].node else {
            unreachable!()
        };
        *hi = g::Index::Int(last);
        body.splice(at + 1..at + 2, [middle, final_wave, bottom, top]);
        changed = true;
        at += 5;
    }
    Ok(changed)
}

pub(super) fn apply(kernel: &mut Kernel, taken: &mut HashSet<String>) -> R<bool> {
    let specs = kernel
        .graph
        .ports
        .iter()
        .map(|p| (p.name.clone(), p.spec.clone()))
        .chain(
            kernel
                .graph
                .scratch
                .iter()
                .map(|p| (p.name.clone(), p.spec.clone())),
        )
        .collect();
    transform(&mut kernel.graph.body, &specs, taken)
}
