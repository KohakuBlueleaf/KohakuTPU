//! Independent planar stage groups (DSL.SPEC.008, DSL.KERN.020).
//!
//! Group boundaries retain the ordinary local schedule. A distributed owner
//! builds its complete ordered subsequence with private scratch and mask cache.

use super::*;
use crate::gen::chain;
use crate::split::{
    partition_tiles, validate_ownership, CostEstimate, CostFeatures, LegalityWitness, LocalTiming,
    OutputLayout, PlannedKernel, Region, ShardRole, ShardTiles, SplitAxis, SplitOption,
    SplitRequest,
};
use serde_json::Value;
use std::collections::BTreeSet;
use std::ops::Range;

struct Group {
    steps: Range<usize>,
    batches: i64,
    heads: i64,
    private: BTreeSet<String>,
}

/// Boundaries are descriptor indices, never positions in a generated body.
pub(crate) fn boundaries(a: &Value, count: usize) -> R<Option<Vec<Range<usize>>>> {
    let Some(value) = a.get("group_ends") else {
        return Ok(None);
    };
    let ends: Vec<usize> = serde_json::from_value(value.clone())
        .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
    let mut first = 0;
    let mut ranges = Vec::new();
    for end in ends {
        if end <= first || end > count {
            return fail(
                "API.OP.001",
                "fused group boundaries must increase within the stages",
            );
        }
        ranges.push(first..end);
        first = end;
    }
    if first != count || ranges.is_empty() {
        return fail("API.OP.001", "fused groups must cover every stage");
    }
    Ok(Some(ranges))
}

/// A complete private definition precedes each private read. Public inputs are
/// immutable and public writes are strided scatters; no public value is an edge.
fn prove(p: &Package, ranges: Vec<Range<usize>>) -> Option<Vec<Group>> {
    if !p.staged.is_empty() {
        return None;
    }
    let input = |name: &str| p.ports.iter().any(|(b, w)| b.name == name && !w);
    let output = |name: &str| p.ports.iter().any(|(b, w)| b.name == name && *w);
    let scratch = |name: &str| p.scratch.iter().any(|b| b.name == name);
    let mut groups = Vec::new();
    for range in ranges {
        let mut defined = BTreeSet::new();
        let mut domain = None;
        let mut writes = false;
        let mut plane = |step: &planes::Planes, defined: &mut BTreeSet<String>| {
            let shape = (step.batches, step.groups);
            if domain.is_some_and(|old| old != shape) {
                return false;
            }
            domain = Some(shape);
            if step.mask.as_ref().is_some_and(|name| !input(name)) {
                return false;
            }
            if step.scatter {
                writes = true;
                output(&step.dst) && defined.contains(&step.src)
            } else if input(&step.src) && scratch(&step.dst) {
                defined.insert(step.dst.clone());
                true
            } else {
                false
            }
        };
        for step in &p.steps[range.clone()] {
            match step {
                Step::Planes(step) if plane(step, &mut defined) => {}
                Step::Attention(step) => {
                    if ![&step.q, &step.k, &step.vt]
                        .iter()
                        .all(|n| defined.contains(*n))
                        || !scratch(&step.out)
                        || step.tail.as_ref().is_some_and(|n| !input(n))
                    {
                        return None;
                    }
                    defined.insert(step.out.clone());
                    if let Some(merge) = &step.merge {
                        if !plane(merge, &mut defined) {
                            return None;
                        }
                    }
                }
                _ => return None,
            }
        }
        let (batches, heads) = domain?;
        if !writes || batches <= 0 || heads <= 0 {
            return None;
        }
        let count = batches.checked_mul(heads)?;
        for step in &p.steps[range.clone()] {
            if let Step::Attention(step) = step {
                if step.heads != count {
                    return None;
                }
            }
        }
        if p.scratch
            .iter()
            .filter(|b| defined.contains(&b.name))
            .any(|b| b.shape.len() != 2 || b.shape[0] % count != 0)
        {
            return None;
        }
        groups.push(Group {
            steps: range,
            batches,
            heads,
            private: defined,
        });
    }
    Some(groups)
}

fn regions(step: &planes::Planes, reads: &mut Vec<Region>, writes: &mut Vec<Region>) {
    let strided = if step.scatter { &step.dst } else { &step.src };
    for plane in 0..step.batches * step.groups {
        for row in 0..step.rows {
            let start = (step.source(plane) + row * step.row_stride) as u64 * 2;
            let region = Region {
                allocation: strided.clone(),
                bytes: start..start + step.columns as u64 * 2,
            };
            if step.scatter {
                writes.push(region);
            } else {
                reads.push(region);
            }
        }
    }
}

fn fragment(step: &planes::Planes, first: i64, count: i64) -> planes::Planes {
    planes::Planes {
        batches: 1,
        groups: count,
        source_offset: step.source(first),
        ..step.clone()
    }
}

/// Slice typed plane domains, retaining global public offsets. Each fragment's
/// named allocations have their own scope; the common lifetime pass pools them.
fn local(p: &Package, groups: &[Group], selected: Range<i64>) -> Package {
    let mut result = Package {
        ports: p.ports.clone(),
        scratch: vec![],
        staged: vec![],
        steps: vec![],
        scratch_space: p.scratch_space,
        balance_stages: p.balance_stages,
    };
    let mut base = 0;
    for group in groups {
        let size = group.batches * group.heads;
        let mut first = (selected.start - base).max(0);
        let end = (selected.end - base).min(size);
        while first < end {
            let count = (group.heads - first % group.heads).min(end - first);
            let prefix = (0..)
                .map(|i| format!("group{i}_"))
                .find(|prefix| {
                    group.private.iter().all(|name| {
                        let name = format!("{prefix}{name}");
                        !p.ports.iter().any(|(b, _)| b.name == name)
                            && !result.scratch.iter().any(|b| b.name == name)
                    })
                })
                .expect("fresh typed group scope");
            let rename = |name: &str| {
                if group.private.contains(name) {
                    format!("{prefix}{name}")
                } else {
                    name.into()
                }
            };
            for buffer in p.scratch.iter().filter(|b| group.private.contains(&b.name)) {
                let mut buffer = buffer.clone();
                buffer.name = rename(&buffer.name);
                buffer.shape[0] = buffer.shape[0] / size * count;
                result.scratch.push(buffer);
            }
            let planar = |step: &planes::Planes| {
                let mut step = fragment(step, first, count);
                step.src = rename(&step.src);
                step.dst = rename(&step.dst);
                step
            };
            for step in &p.steps[group.steps.clone()] {
                result.steps.push(match step {
                    Step::Planes(step) => Step::Planes(planar(step)),
                    Step::Attention(step) => {
                        let mut step = step.clone();
                        step.heads = count;
                        if step.head_group > 0 {
                            step.head_group = (1..=step.head_group.min(count))
                                .rev()
                                .find(|n| count % n == 0)
                                .expect("one divides a head count");
                        }
                        step.q = rename(&step.q);
                        step.k = rename(&step.k);
                        step.vt = rename(&step.vt);
                        step.out = rename(&step.out);
                        step.merge = step.merge.as_ref().map(planar);
                        Step::Attention(step)
                    }
                    _ => unreachable!("proved planar group"),
                });
            }
            first += count;
        }
        base += size;
    }
    result
}

fn manifest(p: &Package) -> (Vec<Region>, Vec<Region>) {
    let mut reads = Vec::new();
    let mut writes = Vec::new();
    let mut shared = BTreeSet::new();
    for step in &p.steps {
        match step {
            Step::Planes(step) => {
                regions(step, &mut reads, &mut writes);
                shared.extend(step.mask.iter().cloned());
            }
            Step::Attention(step) => {
                shared.extend(step.tail.iter().cloned());
                if let Some(merge) = &step.merge {
                    regions(merge, &mut reads, &mut writes);
                }
            }
            _ => unreachable!("proved planar group"),
        }
    }
    // Immutable masks and other shared inputs are small whole public buffers.
    for (port, written) in &p.ports {
        if !written
            && (shared.contains(&port.name) || !reads.iter().any(|r| r.allocation == port.name))
        {
            reads.push(Region {
                allocation: port.name.clone(),
                bytes: 0..port.shape.iter().product::<i64>() as u64 * Hw::item(port.dtype) as u64,
            });
        }
    }
    (reads, writes)
}

pub(crate) fn split(
    p: &Package,
    a: &Value,
    request: &SplitRequest,
    m: &Machine,
) -> R<Option<PlannedKernel>> {
    let ranges = boundaries(a, p.steps.len())?.expect("grouped descriptor");
    let ordinary = if request.force.as_deref() == Some("groups") {
        super::unfinalized(p, &a.to_string(), m)?
    } else {
        super::scheduled(p, a, &a.to_string(), m)?
    };
    let Some(groups) = prove(p, ranges) else {
        return Ok(None);
    };
    let mut admission = crate::split::cost::Admission::default();
    let mut candidates = Vec::new();
    let participants = request.nodes.ids().len();
    for distributed in [false, true] {
        if !distributed && request.force.as_deref() == Some("groups") {
            continue;
        }
        if distributed && (participants == 1 || request.force.as_deref() == Some("p1")) {
            continue;
        }
        let candidate = || -> R<(SplitOption, Kernel)> {
            let mut shares = Vec::new();
            let mut shards = Vec::new();
            let mut output = OutputLayout {
                form: "producer".into(),
                tensors: ordinary
                    .graph
                    .ports
                    .iter()
                    .filter(|p| p.role == g::Role::Out)
                    .map(|p| (p.name.clone(), p.spec.clone()))
                    .collect(),
                ..OutputLayout::default()
            };
            let cuts = if distributed {
                let total = groups
                    .iter()
                    .try_fold(0i64, |sum, group| {
                        group
                            .batches
                            .checked_mul(group.heads)
                            .and_then(|n| sum.checked_add(n))
                    })
                    .ok_or_else(|| {
                        ktpu_machine::Refusal::new("API.OP.001", "group domain exceeds i64")
                    })?;
                partition_tiles(total, participants)?
            } else {
                vec![0..1]
            };
            for (rank, &node) in request.nodes.ids().iter().enumerate() {
                let cut = cuts.get(rank).cloned().unwrap_or(0..0);
                let mut shard = ShardTiles {
                    logical_node: rank,
                    tile_ranges: vec![cut.clone()],
                    logical_origin: vec![cut.start],
                    valid_extent: vec![cut.end - cut.start],
                    padded_extent: vec![cut.end - cut.start],
                    read_regions: vec![],
                    write_regions: vec![],
                    scratch_regions: vec![],
                    local_schedule: "independent-planar-groups".into(),
                    role: if cut.is_empty() {
                        ShardRole::Empty
                    } else {
                        ShardRole::Compute
                    },
                };
                if !cut.is_empty() {
                    let kernel = if distributed {
                        let key = format!("{a}:{}:{}", cut.start, cut.end);
                        let local = local(p, &groups, cut);
                        (shard.read_regions, shard.write_regions) = manifest(&local);
                        super::unfinalized(&local, &key, m)?
                    } else {
                        (shard.read_regions, shard.write_regions) = manifest(p);
                        ordinary.clone()
                    };
                    output
                        .ownership
                        .extend(shard.write_regions.iter().cloned().map(|r| (rank, r)));
                    shares.push(chain::placed(kernel, node)?);
                }
                shards.push(shard);
            }
            output.valid_region = output.ownership.iter().map(|(_, r)| r.clone()).collect();
            validate_ownership(&output)?;
            let mut kernel = chain::sequence::scoped(&ordinary, shares)?;
            if distributed {
                kernel = chain::sequence::finish(kernel, a, m)?;
            }
            let cycles = ktpu_graph::node_cycles(&kernel.graph, m)?;
            let features = CostFeatures {
                share_cycles: shards
                    .iter()
                    .filter(|s| s.role == ShardRole::Compute)
                    .map(|s| {
                        cycles
                            .get(&request.nodes.ids()[s.logical_node])
                            .copied()
                            .unwrap_or(0)
                    })
                    .collect(),
                dispatch_intercept: m.vc.launch_front as i64,
            };
            chain::sequence::scratch_regions(&kernel, &request.nodes, &mut shards, m)?;
            let option = SplitOption {
                id: if distributed { "groups" } else { "p1" }.into(),
                axis: if distributed {
                    SplitAxis::Groups
                } else {
                    SplitAxis::Unsplit
                },
                p: if distributed { participants } else { 1 },
                active_nodes: shards
                    .iter()
                    .filter(|s| s.role == ShardRole::Compute)
                    .count(),
                shards,
                output,
                events: vec![],
                conversions: 0,
                cost: CostEstimate::default(),
                legality: LegalityWitness {
                    numerics: request.numerics.clone(),
                    clauses: vec![concat!(
                        "private complete plane definitions; immutable public reads; ",
                        "disjoint destination lines"
                    )
                    .into()],
                },
            };
            let option = crate::split::cost::price_option(option, &features, &LocalTiming)?;
            Ok((option, kernel))
        };
        match admission.consider(if distributed { "groups" } else { "p1" }, candidate()) {
            Ok(Some(candidate)) => candidates.push(candidate),
            Ok(None) => {}
            Err(_) if distributed && request.force.as_deref() != Some("groups") => {}
            Err(error) => return Err(error),
        }
    }
    let selected = crate::split::cost::select_admitted(
        admission.finish(
            candidates
                .iter()
                .map(|(option, _)| option.clone())
                .collect(),
            request.force.as_deref(),
        )?,
        request.force.as_deref(),
        |option| {
            let (_, kernel) = candidates
                .iter()
                .find(|(candidate, _)| candidate.id == option.id)
                .expect("priced group candidate");
            ktpu_graph::check_staging(&kernel.graph, m, request.nodes.staging_budget())
        },
    )?;
    let (_, kernel) = candidates
        .into_iter()
        .find(|(o, _)| o.id == selected.id)
        .expect("selected emitted candidate");
    Ok(Some(PlannedKernel {
        kernel,
        l1: None,
        split: selected,
        request: request.clone(),
    }))
}
