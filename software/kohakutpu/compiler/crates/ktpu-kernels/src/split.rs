//! Kernel-owned sysnode splits, physical output ownership and plan selection.

pub mod cost;

use std::collections::{BTreeMap, BTreeSet};
use std::ops::Range;

use crate::gen::vector::balanced_cut;
use crate::kernel::Kernel;
use crate::trace::{fail, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;

pub use cost::{choose_split, CostEstimate, CostFeatures, CostModel, LocalTiming};
pub use ktpu_l2::lower::events::{EventId, EventSpec};

/// Ordered physical sysnodes. Construction validates the grant once.
#[derive(Clone, Debug)]
pub struct NodeSet(Vec<i64>);

impl NodeSet {
    pub fn new(nodes: Vec<i64>, machine: &Machine) -> R<Self> {
        let unique: BTreeSet<_> = nodes.iter().copied().collect();
        if nodes.is_empty()
            || unique.len() != nodes.len()
            || nodes.iter().any(|&n| n < 0 || n >= machine.nodes() as i64)
        {
            return fail("DSL.CTRL.001", "invalid sysnode grant");
        }
        Ok(Self(nodes))
    }

    pub fn ids(&self) -> &[i64] {
        &self.0
    }
}

pub type FormId = String;

#[derive(Clone, Debug, Default)]
pub struct OutputRequirement {
    /// Empty means the producer's existing form.
    pub accepted_forms: Vec<FormId>,
    pub scale_groups: Vec<i64>,
    pub mapped_reads: Vec<FormId>,
}

#[derive(Clone, Debug, Default)]
pub struct NumericContract {
    pub reduction_order: Vec<(usize, usize)>,
    pub precision: String,
}

#[derive(Clone, Copy, Debug)]
pub enum DispatchProfile {
    Firmware,
    Os,
    Common,
}

#[derive(Clone, Debug)]
pub struct SplitRequest {
    pub nodes: NodeSet,
    pub consumer: OutputRequirement,
    pub dispatch: DispatchProfile,
    pub numerics: NumericContract,
    pub input_forms: Vec<FormId>,
    pub force: Option<String>,
}

#[derive(Clone, Debug)]
pub enum Delivery {
    Direct,
    Relay,
}

#[derive(Clone, Debug)]
pub enum SplitAxis {
    Unsplit,
    M,
    N,
    K,
    MN {
        pm: usize,
        pn: usize,
    },
    BPass {
        pm: usize,
        pn: usize,
        delivery: Delivery,
    },
    Heads,
    QueryRows,
    HeadsQuery {
        ph: usize,
        pq: usize,
    },
    Rows,
    Tokens,
    DestinationTiles,
    Channels,
    Spatial,
    Groups,
    GroupSpatial {
        pg: usize,
        ps: usize,
    },
}

/// One contiguous run of a physical-layout walk, in canonical allocation bytes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Region {
    pub allocation: String,
    pub bytes: Range<u64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ShardRole {
    Compute,
    ReductionRoot,
    ReductionIntermediate,
    Empty,
}

#[derive(Clone, Debug)]
pub struct ShardTiles {
    pub logical_node: usize,
    pub tile_ranges: Vec<Range<i64>>,
    pub logical_origin: Vec<i64>,
    pub valid_extent: Vec<i64>,
    pub padded_extent: Vec<i64>,
    pub read_regions: Vec<Region>,
    pub write_regions: Vec<Region>,
    pub scratch_regions: Vec<Region>,
    pub local_schedule: String,
    pub role: ShardRole,
}

/// Tensor specs retain dtype, shape and physical layout/pack parameters.
#[derive(Clone, Debug, Default)]
pub struct OutputLayout {
    pub form: FormId,
    pub tensors: Vec<(String, g::Spec)>,
    pub valid_region: Vec<Region>,
    pub ownership: Vec<(usize, Region)>,
    pub readiness: Vec<(Region, EventId)>,
}

#[derive(Clone, Debug)]
pub struct LegalityWitness {
    pub numerics: NumericContract,
    pub clauses: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct SplitOption {
    pub id: String,
    pub axis: SplitAxis,
    pub p: usize,
    pub active_nodes: usize,
    pub shards: Vec<ShardTiles>,
    pub output: OutputLayout,
    pub events: Vec<EventSpec>,
    pub conversions: usize,
    pub cost: CostEstimate,
    pub legality: LegalityWitness,
}

#[derive(Clone, Debug)]
pub struct PlannedKernel {
    pub kernel: Kernel,
    pub split: SplitOption,
    pub request: SplitRequest,
}

/// Balanced cuts use the same expression as local vector scheduling.
pub fn partition_tiles(tiles: i64, participants: usize) -> R<Vec<Range<i64>>> {
    let Ok(p) = i64::try_from(participants) else {
        return fail("DSL.CTRL.001", "sysnode count exceeds index range");
    };
    if tiles < 0 || p == 0 {
        return fail(
            "DSL.CTRL.001",
            "partition needs nonnegative tiles and positive participants",
        );
    }
    let mut result = Vec::with_capacity(participants);
    let mut lo = 0;
    for rank in 1..=p {
        let Some(hi) = balanced_cut(tiles, &Idx::Int(rank), p).int() else {
            return fail("DSL.CTRL.001", "partition cut is not a constant");
        };
        result.push(lo..hi);
        lo = hi;
    }
    Ok(result)
}

/// No two owners write the same physical line, even when their bytes differ.
/// The byte union must exactly cover the declared valid output.
pub fn validate_ownership(output: &OutputLayout) -> R<()> {
    let mut lines: BTreeMap<&str, Vec<(u64, u64, usize)>> = BTreeMap::new();
    for (owner, region) in &output.ownership {
        if region.bytes.start > region.bytes.end {
            return fail("DSL.KERN.020", "reversed output region");
        }
        if region.bytes.is_empty() {
            continue;
        }
        lines.entry(&region.allocation).or_default().push((
            region.bytes.start / 64,
            (region.bytes.end - 1) / 64,
            *owner,
        ));
    }
    for runs in lines.values_mut() {
        runs.sort_unstable();
        let mut active: Option<(u64, usize)> = None;
        for &(lo, hi, owner) in runs.iter() {
            if let Some((end, previous)) = active {
                if lo <= end {
                    if previous != owner {
                        return fail(
                            "DSL.KERN.020",
                            "output writers share a physical 64-byte line",
                        );
                    }
                    active = Some((end.max(hi), owner));
                    continue;
                }
            }
            active = Some((hi, owner));
        }
    }
    fn union<'a>(
        regions: impl Iterator<Item = &'a Region>,
    ) -> R<BTreeMap<String, Vec<Range<u64>>>> {
        let mut allocations: BTreeMap<String, Vec<Range<u64>>> = BTreeMap::new();
        for region in regions {
            if region.bytes.start > region.bytes.end {
                return fail("DSL.KERN.020", "reversed output region");
            }
            if !region.bytes.is_empty() {
                allocations
                    .entry(region.allocation.clone())
                    .or_default()
                    .push(region.bytes.clone());
            }
        }
        for runs in allocations.values_mut() {
            runs.sort_by_key(|r| (r.start, r.end));
            let mut merged: Vec<Range<u64>> = Vec::new();
            for run in runs.drain(..) {
                if let Some(last) = merged.last_mut() {
                    if run.start <= last.end {
                        last.end = last.end.max(run.end);
                        continue;
                    }
                }
                merged.push(run);
            }
            *runs = merged;
        }
        Ok(allocations)
    }
    if union(output.valid_region.iter())? != union(output.ownership.iter().map(|(_, r)| r))? {
        return fail(
            "DSL.KERN.020",
            "output ownership does not cover exactly the valid output",
        );
    }
    Ok(())
}

pub fn emit_shares(
    trace: &mut Trace,
    nodes: &NodeSet,
    shares: &[ShardTiles],
    mut body: impl FnMut(&mut Trace, &ShardTiles) -> R<()>,
) -> R<()> {
    for share in shares {
        let Some(&node) = nodes.ids().get(share.logical_node) else {
            return fail("DSL.CTRL.001", "share outside sysnode grant");
        };
        if share.role != ShardRole::Empty {
            trace.on_node(node, |t| body(t, share))?;
        }
    }
    Ok(())
}
