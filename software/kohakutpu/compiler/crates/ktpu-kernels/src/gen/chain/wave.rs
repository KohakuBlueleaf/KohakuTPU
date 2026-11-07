//! A materialized pointwise consumer in its producer's pipeline wave.
//!
//! The producer packs and stores FP16. The consumer reads that stored pack
//! within the same stream or reloads it in a separate VC phase. Complete legal
//! schedules compete without substituting an unrounded producer expression.

use std::collections::HashSet;

use ktpu_graph as g;
use ktpu_machine::Machine;

use crate::expr::{self, E};
use crate::gen::{vector as v, Hw};
use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::buffer::f16;
use crate::trace::control::At;
use crate::trace::{Facts, Idx, Trace, R};

/// The descriptor planner proves identical full-row input/output coordinates
/// and one elementwise input before constructing this scheduling request.
pub(crate) struct Consumer {
    pub expression: E,
    pub output: String,
    pub rows: i64,
    pub width: i64,
    pub bands: Option<i64>,
}

fn count_stores(body: &[g::Stmt], edge: &str) -> usize {
    body.iter()
        .map(|stmt| match &stmt.node {
            g::Node::Store { dst, .. } => usize::from(dst.buffer == edge),
            node => node.body().map_or(0, |body| count_stores(body, edge)),
        })
        .sum()
}

pub(super) fn pure(body: &[g::Stmt], edge: &str) -> bool {
    body.iter().all(|stmt| match &stmt.node {
        g::Node::Value { op, .. } => *op != g::ValueOp::Recv,
        g::Node::Store { dst, .. } => dst.buffer == edge,
        g::Node::For { body, .. } | g::Node::When { body, .. } => pure(body, edge),
        _ => false,
    })
}

fn has_var(index: &g::Index, var: &str) -> bool {
    fn atom(term: &g::Atom, var: &str) -> bool {
        match term {
            g::Atom::Var(name) => name == var,
            g::Atom::Floor(a, b) | g::Atom::Mod(a, b) | g::Atom::Mul(a, b) => {
                has_var(a, var) || has_var(b, var)
            }
        }
    }
    match index {
        g::Index::Int(_) => false,
        g::Index::Sym(sym) => sym.terms.iter().any(|(term, _)| atom(term, var)),
    }
}

fn plus(a: &g::Index, b: &g::Index) -> Option<g::Index> {
    let parts = |index: &g::Index| match index {
        g::Index::Int(n) => (*n, vec![]),
        g::Index::Sym(sym) => (sym.konst, sym.terms.clone()),
    };
    let ((a, mut terms), (b, rest)) = (parts(a), parts(b));
    terms.extend(rest);
    let konst = a.checked_add(b)?;
    Some(if terms.is_empty() {
        g::Index::Int(konst)
    } else {
        g::Index::Sym(g::Sym { konst, terms })
    })
}

struct Tile {
    rows: i64,
    width: i64,
    row: g::Index,
    col: g::Index,
}

fn tile(region: &g::Stmt, edge: &str) -> Option<Tile> {
    let g::Node::Par { body, .. } = &region.node else {
        return None;
    };
    let mut last = body.last()?;
    if let g::Node::When { body, .. } = &last.node {
        last = body.last()?;
    }
    let g::Node::For {
        var,
        lo: g::Index::Int(0),
        hi: g::Index::Int(trips),
        body,
        ..
    } = &last.node
    else {
        return None;
    };
    let g::Node::Store { dst, .. } = &body.last()?.node else {
        return None;
    };
    let [g::Axis::Run(g::Index::Sym(row), height), g::Axis::Run(col, width)] = &dst.axes[..] else {
        return None;
    };
    if dst.buffer != edge || *trips <= 0 || *height <= 0 || *width <= 0 || has_var(col, var) {
        return None;
    }
    let mut row = row.clone();
    let (g::Atom::Var(term), coefficient) = row.terms.pop()? else {
        return None;
    };
    if term != *var || coefficient != *height || has_var(&g::Index::Sym(row.clone()), var) {
        return None;
    }
    Some(Tile {
        rows: trips.checked_mul(*height)?,
        width: *width,
        row: g::Index::Sym(row),
        col: col.clone(),
    })
}

/// One VC's phase, independent of the caller's VC count. The enclosing region
/// supplies that producer's assignment and its row/column origins.
fn phase(consumer: &Consumer, tile: &Tile, m: &Machine) -> R<Option<Kernel>> {
    let hw = Hw::of(m);
    let operand = v::Operand::full(consumer.width, consumer.rows);
    let shape = v::rows_shape(
        &consumer.expression,
        &[operand],
        tile.rows,
        tile.width,
        &operand,
        consumer.bands,
        &hw,
    )?;
    if shape.pl.nblk != 1 || tile.rows % shape.quantum != 0 {
        return Ok(None);
    }
    let dimensions = [consumer.rows, consumer.width];
    let spec = f16(&dimensions)?;
    let mut t = Trace::new("materialized_phase", Facts::of(m), &m.die.target);
    let src = t.port("src", g::Role::In, spec.clone())?;
    let dst = t.port("dst", g::Role::Out, spec.clone())?;
    let bufs = v::Bufs::plain(vec![src], dst);
    for (plan, first, _) in v::stages(&shape.pl, tile.rows, shape.quantum) {
        t.par(1, g::UnitKind::Vc, At::Var, |t, _| {
            let pro = v::prologue(t, &bufs, &[operand], &plan, 0, &Idx::Int(0))?;
            t.range(plan.nb, |t, row| {
                let origin = v::Origin::rows(&row.mul(plan.rows).add(first), 1);
                v::rows_block(
                    t,
                    &bufs,
                    &consumer.expression,
                    &[operand],
                    &operand,
                    &plan,
                    &origin,
                    0,
                    &pro,
                    &hw,
                )
            })
        })?;
    }
    let kernel = Kernel {
        graph: t.graph()?,
        given: vec![("src".into(), spec.clone()), ("dst".into(), spec)],
    };
    Ok(Some(kernel))
}

fn install_body(region: &mut g::Stmt, replacement: Vec<g::Stmt>) {
    let g::Node::Par { body, .. } = &mut region.node else {
        unreachable!("a producer VC region")
    };
    if matches!(body.last().map(|s| &s.node), Some(g::Node::When { .. })) {
        let mut guard = body.pop().expect("producer guard");
        let g::Node::When { body: guarded, .. } = &mut guard.node else {
            unreachable!()
        };
        *guarded = replacement;
        *body = vec![guard];
    } else {
        *body = replacement;
    }
}

fn phase_regions(
    producer: &g::Stmt,
    edge: &str,
    consumer: &Consumer,
    taken: &mut HashSet<String>,
    m: &Machine,
) -> R<Option<Vec<g::Stmt>>> {
    let Some(tile) = tile(producer, edge) else {
        return Ok(None);
    };
    let Some(mut generated) = phase(consumer, &tile, m)? else {
        return Ok(None);
    };
    if !generated.graph.scratch.is_empty() {
        return Ok(None);
    }
    let mut generated_value = serde_json::to_value(&generated.graph.body)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    let mut generated_names = HashSet::new();
    super::bound_names(&generated.graph.body, &mut generated_names);
    let prefix = (0..)
        .map(|i| format!("phase{i}_"))
        .find(|p| {
            generated_names
                .iter()
                .all(|n| !taken.contains(&format!("{p}{n}")))
        })
        .expect("fresh phase names");
    super::renamed(&mut generated_value, &generated_names, &|n| {
        format!("{prefix}{n}")
    });
    taken.extend(generated_names.into_iter().map(|n| format!("{prefix}{n}")));
    generated.graph.body = serde_json::from_value(generated_value)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    let mut overflow = false;
    super::rows::views(&mut generated.graph.body, &mut |view| {
        view.buffer = edge.to_string();
        for (axis, offset) in view.axes.iter_mut().zip([&tile.row, &tile.col]) {
            if let g::Axis::Run(index, _) = axis {
                match plus(index, offset) {
                    Some(sum) => *index = sum,
                    None => overflow = true,
                }
            }
        }
    });
    if overflow {
        return Ok(None);
    }
    let mut regions = Vec::new();
    for phase in generated.graph.body {
        let g::Node::Par { body, .. } = phase.node else {
            unreachable!("a generated VC phase")
        };
        let mut replacement = producer.clone();
        install_body(&mut replacement, body);
        let mut value = serde_json::to_value(&replacement)
            .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
        let mut own = HashSet::new();
        super::bound_names(std::slice::from_ref(&replacement), &mut own);
        let prefix = (0..)
            .map(|i| format!("pointwise{i}_"))
            .find(|p| own.iter().all(|n| !taken.contains(&format!("{p}{n}"))))
            .expect("fresh pointwise names");
        super::renamed(&mut value, &own, &|n| format!("{prefix}{n}"));
        taken.extend(own.into_iter().map(|n| format!("{prefix}{n}")));
        regions.push(
            serde_json::from_value(value)
                .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?,
        );
    }
    Ok(Some(regions))
}

fn embed(
    body: &mut [g::Stmt],
    edge: &str,
    consumer: &Consumer,
    taken: &mut HashSet<String>,
    m: &Machine,
) -> R<usize> {
    let mut matched = 0;
    for stmt in body {
        match &mut stmt.node {
            g::Node::Par { unit, body, .. } if unit.kind == g::UnitKind::Node => {
                matched += embed(body, edge, consumer, taken, m)?;
            }
            g::Node::For {
                pipe: Some(_),
                body,
                ..
            } => {
                let mut phases = Vec::new();
                for region in body.iter() {
                    let g::Node::Par { unit, body, .. } = &region.node else {
                        continue;
                    };
                    if unit.kind != g::UnitKind::Vc
                        || !pure(body, edge)
                        || count_stores(body, edge) != 1
                    {
                        continue;
                    }
                    if let Some(regions) = phase_regions(region, edge, consumer, taken, m)? {
                        matched += 1;
                        phases.extend(regions);
                    }
                }
                body.extend(phases);
            }
            _ => {}
        }
    }
    Ok(matched)
}

/// Schedule an unkept one-input pointwise stage after each complete producer
/// wave. The ordinary chain remains available for non-wave producer schedules.
pub(crate) fn apply(
    kernel: &mut Kernel,
    producer_len: usize,
    edge: &str,
    consumer: &Consumer,
    m: &Machine,
) -> R<()> {
    if !expr::reduces(&consumer.expression).is_empty()
        || expr::loads(&consumer.expression).iter().any(|i| *i != 0)
    {
        return Ok(());
    }
    let Some(output) = kernel.graph.consts["chain"]["ports"][&consumer.output].as_str() else {
        return Ok(());
    };
    let output = output.to_string();
    let producer = &kernel.graph.body[..producer_len];
    let stores = count_stores(producer, edge);
    if stores == 0 {
        return Ok(());
    }
    let mut uses = 0;
    let mut candidate = kernel.clone();
    super::rows::views(&mut candidate.graph.body[..producer_len], &mut |view| {
        uses += usize::from(view.buffer == edge);
    });
    if uses != stores {
        return Ok(());
    }
    let mut taken: HashSet<_> = kernel.graph.ports.iter().map(|p| p.name.clone()).collect();
    taken.extend(kernel.graph.scratch.iter().map(|s| s.name.clone()));
    let value = serde_json::to_value(&kernel.graph.body)
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    super::names_in(&value, &super::NAMED, &mut taken);
    let mut forwarded = kernel.clone();
    let forwarded_stores = super::forward::embed(
        &mut forwarded.graph.body[..producer_len],
        edge,
        &output,
        consumer,
        &mut taken,
        m,
    )?;
    let matched = embed(
        &mut candidate.graph.body[..producer_len],
        edge,
        consumer,
        &mut taken,
        m,
    )?;
    if matched == stores {
        candidate.graph.body.truncate(producer_len);
    } else {
        candidate = kernel.clone();
    }
    super::rows::views(&mut candidate.graph.body, &mut |view| {
        if view.buffer == edge {
            view.buffer = output.clone();
        }
    });
    candidate
        .graph
        .scratch
        .retain(|scratch| scratch.name != edge);
    candidate.graph.name = name(
        if matched == stores {
            "materialized_waves"
        } else {
            "materialized_in_place"
        },
        vec![Py::str(&kernel.graph.name)],
    );
    // Keep the complete materialized schedule in the comparison. Storage
    // reuse is selected independently by the enclosing composition.
    let mut best = g::cycles(&kernel.graph, m)?;
    let forwarded = (forwarded_stores == stores).then(|| {
        forwarded.graph.body.truncate(producer_len);
        forwarded.graph.name = name("rounded_stream", vec![Py::str(&kernel.graph.name)]);
        forwarded
    });
    for alternative in [Some(candidate), forwarded].into_iter().flatten() {
        if let Ok(cycles) = g::cycles(&alternative.graph, m) {
            if cycles <= best {
                best = cycles;
                *kernel = alternative;
            }
        }
    }
    Ok(())
}
