//! Population trees over complete domains or domain element tiles.

use super::domains::GroupNormSpec;
use super::*;
use crate::gen::{cdiv, prepared, relayout::Logical, Hw};
use crate::split::{
    partition_tiles, CostEstimate, LegalityWitness, OutputLayout, PlannedKernel, Region, ShardRole,
    ShardTiles, SplitAxis, SplitOption, SplitRequest,
};
use crate::trace::buffer::sl;
use crate::trace::vector::{Fold, MappedLoad};
use crate::trace::{Buf, Idx, Trace, V};
use g::ValueOp as Op;

#[derive(Clone)]
struct State {
    n: i64,
    mean: Buf,
    m2: Buf,
    owner: usize,
}

fn storage(t: &mut Trace, band: i64, width: i64) -> R<Buf> {
    t.buffer(&[band, width], g::Dtype::F32, vec![])
}

fn load(t: &mut Trace, source: &Buf) -> R<V> {
    let v = t.load(&source.whole(), true)?;
    t.unpack(v, None)
}

#[allow(clippy::too_many_arguments)]
fn mapped(
    t: &mut Trace,
    source: &Buf,
    logical: &Logical,
    domain: i64,
    first: i64,
    count: i64,
    domains: i64,
    width: i64,
    band: i64,
    columns: i64,
) -> R<V> {
    t.load_mapped(
        source,
        &MappedLoad {
            steps: prepared::Read::steps(std::slice::from_ref(logical)),
            at: Idx::Int(domain * width + first),
            pitch: width,
            stride: 1,
            rows: Some((domains - domain).min(band)),
            cols: Some(count),
        },
        vec![band, columns],
    )
}

/// State records use ordinary F32 vector packing. Compact statistic packing is
/// not used: every row's mean/M2 is broadcast across one complete vector block.
#[allow(clippy::too_many_arguments)]
fn leaf(
    t: &mut Trace,
    source: &Buf,
    logical: &Logical,
    domain: i64,
    first: i64,
    count: i64,
    domains: i64,
    width: i64,
    owner: usize,
    request: &SplitRequest,
    hw: &Hw,
) -> R<State> {
    let mean = storage(t, hw.band, hw.kblock)?;
    let m2 = storage(t, hw.band, hw.kblock)?;
    t.on_node(request.nodes.ids()[owner], |t| {
        t.par(1, g::UnitKind::Vc, At::Var, |t, _| {
            t.range(1, |t, _| {
                let x = mapped(
                    t, source, logical, domain, first, count, domains, width, hw.band, hw.kblock,
                )?;
                let fold = Fold {
                    valid_cols: Some(count),
                    carry: false,
                };
                let sum = t.reduce_over(Op::Rsum, x.into(), fold)?;
                let avg = t.math(Op::Mul, &[sum.into(), (1.0 / count as f64).into()])?;
                let delta = t.math(Op::Sub, &[x.into(), avg.into()])?;
                let square = t.math(Op::Mul, &[delta.into(), delta.into()])?;
                let centered = t.reduce_over(Op::Rsum, square.into(), fold)?;
                let avg = t.broadcast(avg, hw.kblock)?;
                let centered = t.broadcast(centered, hw.kblock)?;
                t.store(&mean.whole(), avg.into())?;
                t.store(&m2.whole(), centered.into())
            })
        })
    })?;
    Ok(State {
        n: count,
        mean,
        m2,
        owner,
    })
}

fn merge(t: &mut Trace, a: State, b: State, request: &SplitRequest, hw: &Hw) -> R<State> {
    let expression = merge_states(
        &GroupStatState {
            n: a.n,
            mean: expr::load(0),
            m2: expr::load(1),
        },
        &GroupStatState {
            n: b.n,
            mean: expr::load(2),
            m2: expr::load(3),
        },
    )?;
    let mean = storage(t, hw.band, hw.kblock)?;
    let m2 = storage(t, hw.band, hw.kblock)?;
    t.on_node(request.nodes.ids()[a.owner], |t| {
        t.par(1, g::UnitKind::Vc, At::Var, |t, _| {
            t.range(1, |t, _| {
                let mut values = HashMap::new();
                for (i, buf) in [&a.mean, &a.m2, &b.mean, &b.m2].iter().enumerate() {
                    values.insert(i, Arg::Val(load(t, buf)?));
                }
                let avg = expr::emit(t, &expression.mean, &values)?;
                let centered = expr::emit(t, &expression.m2, &values)?;
                t.store(&mean.whole(), avg)?;
                t.store(&m2.whole(), centered)
            })
        })
    })?;
    Ok(State {
        n: expression.n,
        mean,
        m2,
        owner: a.owner,
    })
}

/// Adjacent pairs merge left to right at each level; an unmatched state advances
/// unchanged. Empty shares are absent, so no empty population is divided.
fn tree(t: &mut Trace, mut states: Vec<State>, request: &SplitRequest, hw: &Hw) -> R<State> {
    while states.len() > 1 {
        let mut next = Vec::new();
        let mut pairs = states.into_iter();
        while let Some(a) = pairs.next() {
            next.push(match pairs.next() {
                Some(b) => merge(t, a, b, request, hw)?,
                None => a,
            });
        }
        states = next;
    }
    states
        .pop()
        .ok_or_else(|| ktpu_machine::Refusal::new("API.OP.001", "empty GroupNorm domain"))
}

fn emit(
    s: &GroupNormSpec,
    request: &SplitRequest,
    spatial_split: bool,
    m: &Machine,
) -> R<PlannedKernel> {
    let (domains, width, spatial) = s.extents()?;
    let hw = Hw::of(m);
    let p = request.nodes.ids().len();
    let domain_unit = if domains / hw.band >= p as i64 && domains % hw.band == 0 {
        hw.band
    } else {
        1
    };
    let rows = domains;
    let columns = cdiv(width, hw.kblock) * hw.kblock;
    let output_bytes = rows
        .checked_mul(columns)
        .and_then(|n| n.checked_mul(2))
        .ok_or_else(|| {
            ktpu_machine::Refusal::new("API.OP.001", "GroupNorm output extent overflow")
        })?;
    let elements = domains * width;
    let input = [cdiv(elements, hw.lanes) * hw.lanes];
    let affine = [cdiv(s.channels, hw.lanes) * hw.lanes];
    let output = [rows, columns];
    let mut ports = Vec::new();
    let mut given = Vec::new();
    for (name, shape, out) in [
        ("x", input.as_slice(), false),
        ("gamma", affine.as_slice(), false),
        ("beta", affine.as_slice(), false),
        ("out", output.as_slice(), true),
    ] {
        ports.push(if out {
            Port::output(name, shape, g::Dtype::F16)
        } else {
            Port::input(name, shape, g::Dtype::F16)
        });
        given.push((
            name.into(),
            spec(shape, g::Dtype::F16, vec![], g::BufSpace::Dram)?,
        ));
    }
    let identity = Logical {
        n: elements,
        at: 0,
        dat: 0,
        m: elements,
        walk: vec![(elements, 1)],
        dwalk: vec![(elements, 1)],
    };
    let parameters = Logical {
        n: s.channels,
        at: 0,
        dat: 0,
        m: elements,
        walk: vec![(s.batch, 0), (s.channels, 1), (spatial, 0)],
        dwalk: vec![
            (s.batch, s.channels * spatial),
            (s.channels, spatial),
            (spatial, 1),
        ],
    };
    let domain_cuts = partition_tiles(rows / domain_unit, p)?;
    let column_cuts = partition_tiles(columns / hw.kblock, p)?;
    let mut owners = Vec::new();
    let mut used = vec![false; p];
    let id = if spatial_split { "spatial" } else { "domains" };
    let key = name(
        "groupnorm",
        vec![
            Py::str(&format!("{s:?}")),
            Py::str(id),
            Py::ints(request.nodes.ids()),
        ],
    );
    let kernel = build(&key, m, ports, given, |t, b| {
        for band in 0..rows / domain_unit {
            let domain = band * domain_unit;
            let valid_domains = domain + domain_unit;
            let private = if domain_unit == hw.band {
                None
            } else {
                Some(t.buffer(&[hw.band, columns], g::Dtype::F16, vec![])?)
            };
            let destination = private.as_ref().unwrap_or(&b[3]);
            let destination_row = if private.is_some() { 0 } else { domain };
            let domain_owner = domain_cuts
                .iter()
                .position(|r| r.contains(&band))
                .ok_or_else(|| ktpu_machine::Refusal::new("DSL.KERN.020", "domain has no owner"))?;
            let tiles_of = |rank: usize| {
                if spatial_split {
                    column_cuts[rank].clone()
                } else if rank == domain_owner {
                    0..columns / hw.kblock
                } else {
                    0..0
                }
            };
            let mut states = Vec::new();
            for rank in 0..p {
                let mut local = Vec::new();
                for tile in tiles_of(rank) {
                    let first = tile * hw.kblock;
                    let count = (width - first).min(hw.kblock);
                    used[rank] = true;
                    local.push(leaf(
                        t,
                        &b[0],
                        &identity,
                        domain,
                        first,
                        count,
                        valid_domains,
                        width,
                        rank,
                        request,
                        &hw,
                    )?);
                }
                if !local.is_empty() {
                    states.push(tree(t, local, request, &hw)?);
                }
            }
            let root = tree(t, states, request, &hw)?;
            let inv = storage(t, hw.band, hw.kblock)?;
            t.on_node(request.nodes.ids()[root.owner], |t| {
                t.par(1, g::UnitKind::Vc, At::Var, |t, _| {
                    t.range(1, |t, _| {
                        let m2 = load(t, &root.m2)?;
                        let variance =
                            t.math(Op::Mul, &[m2.into(), (1.0 / width as f64).into()])?;
                        let shifted = t.math(Op::Add, &[variance.into(), s.eps.into()])?;
                        let inverse = t.math(Op::Rsqrt, &[shifted.into()])?;
                        t.store(&inv.whole(), inverse.into())
                    })
                })
            })?;
            for rank in 0..p {
                let tiles = tiles_of(rank);
                if tiles.is_empty() {
                    continue;
                }
                for row in domain..domain + domain_unit {
                    owners.push((
                        rank,
                        Region {
                            allocation: "out".into(),
                            bytes: ((row * columns + tiles.start * hw.kblock) * 2) as u64
                                ..((row * columns + tiles.end * hw.kblock) * 2) as u64,
                        },
                    ));
                }
                t.on_node(request.nodes.ids()[rank], |t| {
                    for tile in tiles.clone() {
                        t.par(1, g::UnitKind::Vc, At::Var, |t, _| {
                            t.range(1, |t, _| {
                                let avg = load(t, &root.mean)?;
                                let inverse = load(t, &inv)?;
                                let first = tile * hw.kblock;
                                let count = (width - first).min(hw.kblock);
                                let x = mapped(
                                    t,
                                    &b[0],
                                    &identity,
                                    domain,
                                    first,
                                    count,
                                    valid_domains,
                                    width,
                                    hw.band,
                                    hw.kblock,
                                )?;
                                let gamma = mapped(
                                    t,
                                    &b[1],
                                    &parameters,
                                    domain,
                                    first,
                                    count,
                                    valid_domains,
                                    width,
                                    hw.band,
                                    hw.kblock,
                                )?;
                                let beta = mapped(
                                    t,
                                    &b[2],
                                    &parameters,
                                    domain,
                                    first,
                                    count,
                                    valid_domains,
                                    width,
                                    hw.band,
                                    hw.kblock,
                                )?;
                                let centered = t.math(Op::Sub, &[x.into(), avg.into()])?;
                                let normalized =
                                    t.math(Op::Mul, &[centered.into(), inverse.into()])?;
                                let scaled = t.math(Op::Mul, &[normalized.into(), gamma.into()])?;
                                let result = t.math(Op::Add, &[scaled.into(), beta.into()])?;
                                t.store(
                                    &destination.at(&[
                                        sl(destination_row, destination_row + hw.band),
                                        sl(first, first + hw.kblock),
                                    ])?,
                                    result.into(),
                                )
                            })
                        })?;
                    }
                    if private.is_some() {
                        let first = tiles.start * hw.kblock;
                        let values = (tiles.end - tiles.start) * hw.kblock;
                        let walk = [(values * 2 / hw.word_bytes, hw.word_bytes)];
                        t.copy(
                            &b[3].whole(),
                            &destination.whole(),
                            Some((&walk, Some(first * 2))),
                            Some((&walk, Some((domain * columns + first) * 2))),
                        )?;
                    }
                    Ok(())
                })?;
            }
        }
        Ok(())
    })?;
    let output = OutputLayout {
        form: "producer".into(),
        tensors: kernel
            .graph
            .ports
            .iter()
            .filter(|p| p.role == g::Role::Out)
            .map(|p| (p.name.clone(), p.spec.clone()))
            .collect(),
        valid_region: vec![Region {
            allocation: "out".into(),
            bytes: 0..output_bytes as u64,
        }],
        ownership: owners.clone(),
        readiness: vec![],
    };
    crate::split::validate_ownership(&output)?;
    let cycles = ktpu_graph::cycles(&kernel.graph, m)?;
    let mut local_cycles = Vec::new();
    let mut scratch_regions = vec![Vec::new(); p];
    for rank in 0..p {
        if !used[rank] {
            continue;
        }
        let mut local = kernel.graph.clone();
        local.body.retain(|statement| {
            matches!(&statement.node, g::Node::Par { lo: g::Index::Int(node), .. }
                if *node == request.nodes.ids()[rank])
        });
        local_cycles.push(ktpu_graph::cycles(&local, m)? - m.vc.launch_front as i64);
        let serialized = serde_json::to_value(&local.body).map_err(|e| {
            ktpu_machine::Refusal::new("DSL.GRAPH.001", format!("GroupNorm accesses: {e}"))
        })?;
        let mut names = std::collections::BTreeSet::new();
        buffers(&serialized, &mut names);
        scratch_regions[rank] = local
            .scratch
            .iter()
            .filter(|s| names.contains(&s.name))
            .map(|s| Region {
                allocation: s.name.clone(),
                bytes: 0..(s.spec.shape.iter().product::<i64>() * Hw::item(s.spec.dtype)) as u64,
            })
            .collect();
    }
    let mut numerics = request.numerics.clone();
    numerics.precision = "e8m15-f32-state".into();
    if spatial_split {
        let mut ranks: Vec<_> = column_cuts
            .iter()
            .enumerate()
            .filter_map(|(rank, cut)| (!cut.is_empty()).then_some(rank))
            .collect();
        while ranks.len() > 1 {
            let mut next = Vec::new();
            for pair in ranks.chunks(2) {
                if pair.len() == 2 {
                    numerics.reduction_order.push((pair[1], pair[0]));
                }
                next.push(pair[0]);
            }
            ranks = next;
        }
    }
    let shards = (0..p)
        .map(|rank| ShardTiles {
            logical_node: rank,
            tile_ranges: vec![if spatial_split {
                column_cuts[rank].clone()
            } else {
                domain_cuts[rank].clone()
            }],
            logical_origin: vec![
                if spatial_split {
                    0
                } else {
                    domain_cuts[rank].start * domain_unit
                },
                if spatial_split {
                    column_cuts[rank].start * hw.kblock
                } else {
                    0
                },
            ],
            valid_extent: vec![
                if spatial_split {
                    domains
                } else {
                    (domain_cuts[rank].end - domain_cuts[rank].start) * domain_unit
                },
                if spatial_split {
                    (column_cuts[rank].end * hw.kblock).min(width)
                        - (column_cuts[rank].start * hw.kblock).min(width)
                } else {
                    width
                },
            ],
            padded_extent: vec![
                if spatial_split {
                    domains
                } else {
                    (domain_cuts[rank].end - domain_cuts[rank].start) * domain_unit
                },
                if spatial_split {
                    (column_cuts[rank].end - column_cuts[rank].start) * hw.kblock
                } else {
                    columns
                },
            ],
            read_regions: if used[rank] {
                vec![
                    Region {
                        allocation: "x".into(),
                        bytes: 0..(elements * 2) as u64,
                    },
                    Region {
                        allocation: "gamma".into(),
                        bytes: 0..(s.channels * 2) as u64,
                    },
                    Region {
                        allocation: "beta".into(),
                        bytes: 0..(s.channels * 2) as u64,
                    },
                ]
            } else {
                vec![]
            },
            write_regions: owners
                .iter()
                .filter(|(r, _)| *r == rank)
                .map(|(_, r)| r.clone())
                .collect(),
            scratch_regions: scratch_regions[rank].clone(),
            local_schedule: "balanced-population-tree-f32".into(),
            role: if used[rank] {
                ShardRole::Compute
            } else {
                ShardRole::Empty
            },
        })
        .collect();
    Ok(PlannedKernel {
        kernel,
        l1: None,
        request: request.clone(),
        split: SplitOption {
            id: id.into(),
            axis: if spatial_split {
                SplitAxis::Spatial
            } else {
                SplitAxis::Groups
            },
            p,
            active_nodes: used.iter().filter(|&&v| v).count(),
            shards,
            output,
            events: vec![],
            conversions: usize::from(domain_unit != hw.band),
            cost: CostEstimate {
                makespan: cycles,
                local_cycles,
                dispatch_intercept: m.vc.launch_front as i64,
                provenance: "ktpu-l2 population tree, F32 transport and normalization".into(),
                ..Default::default()
            },
            legality: LegalityWitness {
                numerics,
                clauses: vec![
                    "NCHW domains; padded domain-major output; exact valid counts; fixed adjacent-pair tree; F32 vector states".into(),
                ],
            },
        },
    })
}

pub fn kernel(s: &GroupNormSpec, request: &SplitRequest, m: &Machine) -> R<PlannedKernel> {
    let mut candidates = Vec::new();
    for (id, spatial) in [("domains", false), ("spatial", true)] {
        if request.force.as_deref().is_some_and(|forced| forced != id) {
            continue;
        }
        candidates.push(emit(s, request, spatial, m)?);
    }
    let chosen = crate::split::cost::select_split(
        candidates.iter().map(|p| p.split.clone()).collect(),
        request.force.as_deref(),
    )?;
    candidates
        .into_iter()
        .find(|p| p.split.id == chosen.id)
        .ok_or_else(|| ktpu_machine::Refusal::new("DSL.KERN.020", "no GroupNorm option emitted"))
}

fn buffers(value: &serde_json::Value, names: &mut std::collections::BTreeSet<String>) {
    match value {
        serde_json::Value::Object(fields) => {
            for (key, value) in fields {
                if key == "buffer" {
                    if let Some(name) = value.as_str() {
                        names.insert(name.into());
                    }
                } else {
                    buffers(value, names);
                }
            }
        }
        serde_json::Value::Array(values) => {
            for value in values {
                buffers(value, names);
            }
        }
        _ => {}
    }
}
