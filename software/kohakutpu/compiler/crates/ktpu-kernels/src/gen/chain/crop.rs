//! Stream complete producer bands directly through a logical prefix boundary.
//!
//! A capacity producer with a single row-band loop can write all complete
//! logical bands to the public destination. Only its last band is staged and
//! copied by the boundary mover (DSL.MOV.001, API.ROW.002).

use super::index::interval;
use std::collections::{BTreeMap, HashSet};

use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::Value;

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::R;

fn body_mut(node: &mut g::Node) -> Option<&mut Vec<g::Stmt>> {
    match node {
        g::Node::Par { body, .. }
        | g::Node::For { body, .. }
        | g::Node::When { body, .. }
        | g::Node::Sticky { body } => Some(body),
        _ => None,
    }
}

fn stores(body: &[g::Stmt], buffer: &str) -> usize {
    body.iter()
        .map(|stmt| match &stmt.node {
            g::Node::Store { dst, .. } => usize::from(dst.buffer == buffer),
            node => node.body().map_or(0, |body| stores(body, buffer)),
        })
        .sum()
}

fn trips_mut(region: &mut g::Stmt) -> Option<&mut g::Stmt> {
    let last = body_mut(&mut region.node)?.last_mut()?;
    if matches!(last.node, g::Node::When { .. }) {
        body_mut(&mut last.node)?.last_mut()
    } else {
        Some(last)
    }
}

struct RowBands {
    origin: g::Index,
    count: i64,
    single: bool,
}

fn row_bands(
    stmt: &g::Stmt,
    edge: &str,
    capacity: i64,
    height: i64,
    width: i64,
    row_var: &str,
    mut bounds: BTreeMap<String, (i64, i64)>,
) -> Option<RowBands> {
    match &stmt.node {
        g::Node::Par {
            var, lo, hi, body, ..
        }
        | g::Node::For {
            var, lo, hi, body, ..
        } => {
            let (lo, _) = interval(lo, &bounds)?;
            let (_, hi) = interval(hi, &bounds)?;
            if hi <= lo {
                return None;
            }
            bounds.insert(var.clone(), (lo, hi - 1));
            let nested = body
                .iter()
                .find(|s| stores(std::slice::from_ref(s), edge) != 0)?;
            row_bands(nested, edge, capacity, height, width, row_var, bounds)
        }
        g::Node::When { body, .. } => {
            let nested = body
                .iter()
                .find(|s| stores(std::slice::from_ref(s), edge) != 0)?;
            row_bands(nested, edge, capacity, height, width, row_var, bounds)
        }
        g::Node::Store { dst, .. } if dst.buffer == edge => {
            let g::Axis::Run(g::Index::Sym(index), _) = dst.axes.first()? else {
                return None;
            };
            let (g::Atom::Var(var), coefficient) = index.terms.last()? else {
                return None;
            };
            let &(lo, hi) = bounds.get(var)?;
            if var != row_var || *coefficient != height || lo != 0 {
                return None;
            }
            let count = hi.checked_add(1)?;
            let tile = count.checked_mul(height)?;
            let mut origin = index.clone();
            origin.terms.pop();
            bounds.remove(var);
            let range = interval(&g::Index::Sym(origin.clone()), &bounds)?;
            if range.0 != 0 || range.1.checked_add(tile)? != capacity {
                return None;
            }
            if range != (0, 0)
                && (origin.konst % tile != 0 || origin.terms.iter().any(|(_, n)| n % tile != 0))
            {
                return None;
            }
            let [_, g::Axis::Run(column, columns)] = &dst.axes[..] else {
                return None;
            };
            let column_range = interval(column, &bounds)?;
            if *columns <= 0
                || column_range.0 != 0
                || column_range.1.checked_add(*columns)? != width
            {
                return None;
            }
            Some(RowBands {
                origin: g::Index::Sym(origin),
                count,
                single: range == (0, 0),
            })
        }
        _ => None,
    }
}

fn pure_region(body: &[g::Stmt], edge: &str) -> bool {
    body.iter().all(|stmt| match &stmt.node {
        g::Node::Value { op, .. } => *op != g::ValueOp::Recv,
        g::Node::Store { dst, .. } => dst.buffer == edge,
        g::Node::For { body, .. } | g::Node::When { body, .. } => pure_region(body, edge),
        _ => false,
    })
}

fn holder(region: &mut g::Stmt) -> Option<&mut Vec<g::Stmt>> {
    let body = body_mut(&mut region.node)?;
    if matches!(body.last()?.node, g::Node::When { .. }) {
        body_mut(&mut body.last_mut()?.node)
    } else {
        Some(body)
    }
}

fn guarded(stmt: g::Stmt, origin: &g::Index, op: g::CmpOp, boundary: i64) -> g::Stmt {
    g::Stmt {
        at: stmt.at.clone(),
        node: g::Node::When {
            cond: g::Cond {
                a: origin.clone(),
                op,
                b: g::Index::Int(boundary),
            },
            body: vec![stmt],
        },
    }
}

fn band(region: &g::Stmt, edge: &str) -> Option<(i64, String)> {
    let mut region = region.clone();
    let trips = trips_mut(&mut region)?;
    let g::Node::For {
        var,
        lo: g::Index::Int(0),
        hi: g::Index::Int(_),
        body,
        ..
    } = &trips.node
    else {
        return None;
    };
    let g::Node::Store { dst, .. } = &body.last()?.node else {
        return None;
    };
    if dst.buffer != edge || stores(body, edge) != 1 {
        return None;
    }
    match dst.axes.first()? {
        g::Axis::Run(_, height) if *height > 0 => Some((*height, var.clone())),
        _ => None,
    }
}

fn duplicate(stmt: &g::Stmt, taken: &mut HashSet<String>) -> R<g::Stmt> {
    let error = |e| ktpu_machine::Refusal::new("API.OP.001", format!("chain row boundary: {e}"));
    let mut value = serde_json::to_value(stmt).map_err(error)?;
    let mut own = HashSet::new();
    super::bound_names(std::slice::from_ref(stmt), &mut own);
    let prefix = (0..)
        .map(|i| format!("boundary{i}_"))
        .find(|p| own.iter().all(|n| !taken.contains(&format!("{p}{n}"))))
        .expect("unused generated names");
    super::renamed(&mut value, &own, &|n| format!("{prefix}{n}"));
    taken.extend(own.into_iter().map(|n| format!("{prefix}{n}")));
    serde_json::from_value(value).map_err(error)
}

/// Apply the band/copy contraction only to the producer schedule the rule
/// describes. Other copy compositions retain the ordinary typed-chain emitter.
pub(crate) fn apply(kernel: &mut Kernel, arguments: &Value, machine: &Machine) -> R<()> {
    if arguments.get("keep").and_then(Value::as_bool) != Some(false) {
        return Ok(());
    }
    let Some(boundary) = super::super::convert::prefix::recorded(&arguments["second"], machine)?
    else {
        return Ok(());
    };
    if boundary.initialize
        || boundary.classed
        || boundary.rows > boundary.src_rows
        || boundary.src_first != 0
        || boundary.src_pitch != boundary.width
        || boundary.dst_pitch != boundary.width
    {
        return Ok(());
    }
    let Some(edge) = arguments.get("to").and_then(Value::as_str) else {
        return Ok(());
    };
    let Some(destination) = kernel.graph.consts["chain"]["ports"]["dst"].as_str() else {
        return Ok(());
    };
    let destination = destination.to_string();
    let Some(last) = kernel.graph.body.last() else {
        return Ok(());
    };
    let g::Node::Mover {
        op: g::MoverOp::Copy,
        dst,
        src: Some(src),
        ..
    } = &last.node
    else {
        return Ok(());
    };
    if src.buffer != edge || dst.buffer != destination {
        return Ok(());
    }
    // A public capacity edge must not also feed another computation.
    let mut uses = 0;
    super::rows::views(&mut kernel.graph.body, &mut |v| {
        uses += usize::from(v.buffer == edge);
    });
    if uses != 2 || stores(&kernel.graph.body, edge) != 1 {
        return Ok(());
    }
    let candidate = kernel
        .graph
        .body
        .iter()
        .enumerate()
        .find_map(|(index, stmt)| {
            let g::Node::For {
                pipe: Some(_),
                body,
                ..
            } = &stmt.node
            else {
                return None;
            };
            let candidates: Vec<_> = body
                .iter()
                .enumerate()
                .filter_map(|(at, region)| match &region.node {
                    g::Node::Par { unit, body, .. }
                        if unit.kind == g::UnitKind::Vc && pure_region(body, edge) =>
                    {
                        band(region, edge).map(|(height, var)| (at, height, var))
                    }
                    _ => None,
                })
                .collect();
            match candidates.as_slice() {
                [(at, height, var)] => Some((index, *at, *height, var.clone())),
                _ => None,
            }
        });
    let Some((outer, region, height, row_var)) = candidate else {
        return Ok(());
    };
    let g::Node::For {
        var, lo, hi, body, ..
    } = &kernel.graph.body[outer].node
    else {
        unreachable!()
    };
    let (g::Index::Int(lo), g::Index::Int(hi)) = (lo, hi) else {
        return Ok(());
    };
    let Some(bands) = row_bands(
        &body[region],
        edge,
        boundary.src_rows,
        height,
        boundary.width,
        &row_var,
        BTreeMap::from([(var.clone(), (*lo, *hi - 1))]),
    ) else {
        return Ok(());
    };
    let rows = boundary.rows;
    let first = rows / height * height;
    fn translated_store(body: &[g::Stmt], edge: &str, offset: i64) -> Option<i64> {
        body.iter().find_map(|stmt| match &stmt.node {
            g::Node::Store { dst, .. } if dst.buffer == edge => {
                let g::Axis::Run(g::Index::Sym(index), _) = dst.axes.first()? else {
                    return None;
                };
                index.konst.checked_add(offset)
            }
            node => node
                .body()
                .and_then(|body| translated_store(body, edge, offset)),
        })
    }
    let Some(output_constant) = translated_store(&kernel.graph.body, edge, boundary.dst_first)
    else {
        return Ok(());
    };
    if rows == boundary.src_rows {
        let mut body = kernel.graph.body[..kernel.graph.body.len() - 1].to_vec();
        super::rows::views(&mut body, &mut |view| {
            if view.buffer == edge {
                let Some(g::Axis::Run(g::Index::Sym(index), _)) = view.axes.first_mut() else {
                    unreachable!("proved producer row store")
                };
                index.konst = output_constant;
                view.buffer = destination.clone();
            }
        });
        kernel.graph.body = body;
        kernel.graph.scratch.retain(|scratch| scratch.name != edge);
        kernel.graph.name = name("direct_boundary", vec![Py::str(&kernel.graph.name)]);
        return Ok(());
    }
    if rows == first || boundary.src_rows - first < height {
        return Ok(());
    }
    let tile = bands.count * height;
    let last_origin = first / tile * tile;
    let local_main = (first - last_origin) / height;
    let error = |e| ktpu_machine::Refusal::new("API.OP.001", format!("chain row boundary: {e}"));
    let mut taken = HashSet::new();
    taken.extend(kernel.graph.ports.iter().map(|p| p.name.clone()));
    taken.extend(kernel.graph.scratch.iter().map(|p| p.name.clone()));
    super::names_in(
        &serde_json::to_value(&kernel.graph.body).map_err(error)?,
        &super::NAMED,
        &mut taken,
    );
    let tail = (0..)
        .map(|i| format!("boundary_tail_{i}"))
        .find(|n| taken.insert(n.clone()))
        .expect("an unused tail buffer");
    let g::Node::For { body, .. } = &mut kernel.graph.body[outer].node else {
        unreachable!()
    };
    let mut tail_region = duplicate(&body[region], &mut taken)?;
    let tail_body = holder(&mut tail_region).expect("proved row loop holder");
    let mut tail_trip = tail_body.pop().expect("proved row loop");
    let g::Node::For {
        lo, hi, body: trip, ..
    } = &mut tail_trip.node
    else {
        unreachable!()
    };
    *lo = g::Index::Int(local_main);
    *hi = g::Index::Int(local_main + 1);
    let g::Node::Store { dst, .. } = &mut trip.last_mut().expect("band store").node else {
        unreachable!()
    };
    let Some(g::Axis::Run(g::Index::Sym(index), _)) = dst.axes.first_mut() else {
        unreachable!()
    };
    let mut tail_origin = index.clone();
    let row_term = tail_origin.terms.pop().expect("proved row term");
    *index = g::Sym {
        konst: -local_main * height,
        terms: vec![row_term],
    };
    dst.buffer = tail.clone();
    tail_body.push(if bands.single {
        tail_trip
    } else {
        guarded(
            tail_trip,
            &g::Index::Sym(tail_origin),
            g::CmpOp::Eq,
            last_origin,
        )
    });
    let main_body = holder(&mut body[region]).expect("proved row loop holder");
    let mut main_trip = main_body.pop().expect("proved row loop");
    let g::Node::For { hi, body: trip, .. } = &mut main_trip.node else {
        unreachable!()
    };
    if bands.single {
        *hi = g::Index::Int(local_main);
    }
    let g::Node::Store { dst, .. } = &mut trip.last_mut().expect("band store").node else {
        unreachable!()
    };
    dst.buffer = destination.clone();
    let Some(g::Axis::Run(g::Index::Sym(index), _)) = dst.axes.first_mut() else {
        unreachable!("proved product row store")
    };
    index.konst = output_constant;
    if bands.single {
        main_body.push(main_trip);
    } else {
        let mut last_trip = duplicate(&main_trip, &mut taken)?;
        let g::Node::For { hi, .. } = &mut last_trip.node else {
            unreachable!()
        };
        *hi = g::Index::Int(local_main);
        if last_origin > 0 {
            main_body.push(guarded(main_trip, &bands.origin, g::CmpOp::Lt, last_origin));
        }
        if local_main > 0 {
            main_body.push(guarded(last_trip, &bands.origin, g::CmpOp::Eq, last_origin));
        }
    }
    body.insert(region + 1, tail_region);
    kernel.graph.scratch.retain(|s| s.name != edge);
    kernel.graph.scratch.push(g::Scratch {
        name: tail.clone(),
        spec: g::Spec {
            shape: vec![height, boundary.width],
            dtype: g::Dtype::F16,
            layout: vec![],
            space: g::BufSpace::Stage,
        },
    });
    let word = machine.die.word_bytes as i64;
    let walk = vec![((rows - first) * boundary.width * 2 / word, word)];
    let copy = kernel.graph.body.last_mut().expect("boundary copy");
    copy.node = g::Node::Mover {
        op: g::MoverOp::Copy,
        dst: g::View {
            buffer: destination,
            axes: vec![g::Axis::All],
        },
        src: Some(g::View {
            buffer: tail,
            axes: vec![g::Axis::All],
        }),
        flags: vec![],
        kw: vec![
            (
                "dat".into(),
                g::Attr::Int((boundary.dst_first + first) * boundary.width * 2),
            ),
            ("walk".into(), g::Attr::Walk(walk.clone())),
            ("dwalk".into(), g::Attr::Walk(walk)),
        ],
    };
    kernel.graph.name = name("streamed_boundary", vec![Py::str(&kernel.graph.name)]);
    Ok(())
}
