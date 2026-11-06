//! Native accumulator reduction over an ordered sysnode grant.

use super::*;
use crate::split::{NodeSet, ShardRole, SplitOption};
use crate::trace::buffer::Ax;

pub(super) struct Plan<'a> {
    pub nodes: &'a NodeSet,
    pub edges: &'a [(usize, usize)],
    pub root: usize,
    pub chunks: i64,
    pub active: Vec<usize>,
}

impl<'a> Plan<'a> {
    pub fn new(nodes: &'a NodeSet, option: &'a SplitOption) -> Self {
        let root = option
            .shards
            .iter()
            .find(|s| s.role == ShardRole::ReductionRoot)
            .expect("a reduction root")
            .logical_node;
        Self {
            nodes,
            edges: &option.legality.numerics.reduction_order,
            root,
            chunks: option
                .shards
                .iter()
                .map(|s| s.tile_ranges[2].end)
                .max()
                .expect("K shares"),
            active: option
                .shards
                .iter()
                .filter(|s| s.role != ShardRole::Empty)
                .map(|s| s.logical_node)
                .collect(),
        }
    }
}

pub(super) fn reduction(p: usize, tree: bool, active: &[usize]) -> (Vec<(usize, usize)>, usize) {
    let mut edges = Vec::new();
    let mut join = |a, b| match (a, b) {
        (Some(a), Some(b)) => {
            edges.push((a, b));
            Some(b)
        }
        (a, None) => a,
        (None, b) => b,
    };
    let live = |rank| active.contains(&rank).then_some(rank);
    let root = if p == 4 && tree {
        let left = join(live(0), live(1));
        let right = join(live(3), live(2));
        join(left, right).expect("a nonempty K partition")
    } else {
        let root = *active.last().expect("a nonempty K partition");
        for &rank in active.iter().filter(|&&rank| rank != root) {
            join(Some(rank), Some(root));
        }
        root
    };
    (edges, root)
}

/// Each node runs the same cluster stage and tile-to-cluster assignment.
pub(super) fn clusters(
    t: &mut Trace,
    plan: Option<&Plan<'_>>,
    count: i64,
    trips: i64,
    body: impl FnOnce(&mut Trace, Idx, Idx, Option<Idx>) -> R<()>,
) -> R<()> {
    match plan {
        Some(plan) => t.range(trips, |t, trip| {
            t.on_nodes(plan.nodes, |t, rank| {
                let work = |t: &mut Trace| {
                    t.par(count, g::UnitKind::Mg, At::Var, |t, unit| {
                        body(t, unit, trip, Some(rank.clone()))
                    })
                };
                if plan.active.len() == plan.nodes.ids().len() {
                    work(t)
                } else {
                    let p = plan.nodes.ids().len() as i64;
                    let lo = v::balanced_cut(plan.chunks, &rank, p);
                    let hi = v::balanced_cut(plan.chunks, &rank.add(1), p);
                    t.when(Cond::new(&hi.sub(&lo), g::CmpOp::Gt, 0), work)
                }
            })
        }),
        None => t.par(count, g::UnitKind::Mg, At::Var, |t, unit| {
            t.range(trips, |t, trip| body(t, unit, trip, None))
        }),
    }
}

pub(super) fn root(
    t: &mut Trace,
    plan: Option<&Plan<'_>>,
    body: impl FnOnce(&mut Trace) -> R<()>,
) -> R<()> {
    match plan {
        Some(plan) => t.on_node(plan.nodes.ids()[plan.root], body),
        None => body(t),
    }
}

/// Whole packed K chunks retain the producer's scale blocks.
pub(super) fn chunks(mm: &Mm, hw: &Hw, rank: &Idx, p: usize) -> (Idx, Idx) {
    let total = mm.kp / mm.kc(hw);
    let lo = v::balanced_cut(total, rank, p as i64);
    let hi = v::balanced_cut(total, &rank.add(1), p as i64);
    (lo.clone(), hi.sub(&lo))
}

pub(super) fn drain(
    t: &mut Trace,
    plan: &Plan<'_>,
    rank: &Idx,
    unit: &Idx,
    acc: crate::trace::V,
    dst: &View,
) -> R<()> {
    for &(from, to) in plan.edges {
        t.when(Cond::new(rank, g::CmpOp::Eq, from as i64), |t| {
            t.drain_add(acc, unit, plan.nodes.ids()[to])
        })?;
    }
    t.when(Cond::new(rank, g::CmpOp::Eq, plan.root as i64), |t| {
        t.drain(acc, dst)
    })
}

/// Quantise each node's complete K chunks into its existing staging layout.
#[allow(clippy::too_many_arguments)]
pub(super) fn quant(
    t: &mut Trace,
    plan: Option<&Plan<'_>>,
    mm: &Mm,
    hw: &Hw,
    dst: &View,
    src: &View,
    padded: bool,
    status: Option<&View>,
) -> R<()> {
    let Some(plan) = plan else {
        return quant_a(t, dst, src, padded, status);
    };
    let p = plan.nodes.ids().len();
    let cuts = crate::split::partition_tiles(mm.kp / mm.kc(hw), p)?;
    for (rank, cut) in cuts.iter().enumerate() {
        if cut.is_empty() {
            continue;
        }
        let lo = cut.start * mm.kc(hw);
        let hi = cut.end * mm.kc(hw);
        let mut dst = dst.clone();
        dst.axes[1] = Ax::Run(Idx::Int(lo), hi - lo);
        dst.whole = false;
        let mut src = src.clone();
        src.axes[1] = Ax::Run(Idx::Int(lo), hi.min(mm.k) - lo);
        src.whole = false;
        let status = status.map(|s| {
            let mut s = s.clone();
            let stride = s.buf.spec.shape[0] / p as i64;
            if let Ax::Run(start, _) = &mut s.axes[0] {
                *start = start.add(rank as i64 * stride);
            }
            s
        });
        t.on_node(plan.nodes.ids()[rank], |t| {
            quant_a(t, &dst, &src, padded, status.as_ref())
        })?;
    }
    Ok(())
}

pub(super) fn check(t: &mut Trace, plan: Option<&Plan<'_>>, status: &Buf) -> R<()> {
    let Some(plan) = plan else {
        return t.quant_check(&status.whole());
    };
    let stride = status.spec.shape[0] / plan.nodes.ids().len() as i64;
    for &rank in &plan.active {
        let node = plan.nodes.ids()[rank];
        let lo = rank as i64 * stride;
        t.on_node(node, |t| t.quant_check(&status.at(&[sl(lo, lo + stride)])?))?;
    }
    Ok(())
}
