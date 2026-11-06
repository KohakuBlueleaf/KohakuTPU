//! Output-rectangle ownership over an ordered sysnode grant.

use super::*;
use crate::split::{
    emit_shares, partition_tiles, validate_ownership, CostEstimate, LegalityWitness, OutputLayout,
    Region, ShardRole, ShardTiles, SplitAxis, SplitOption, SplitRequest,
};

pub struct Shape<'a> {
    pub mm: &'a Mm,
    pub class: Option<&'a crate::gen::classes::ShapeClass>,
    pub e: &'a E,
    pub extras: &'a [Operand],
    pub out: &'a Operand,
    pub knobs: &'a Knobs,
    pub made: &'a BTreeMap<usize, prepared::Read>,
    pub demands: &'a [QDemand],
}

/// Global input origins; scratch and the local schedule start at zero.
#[derive(Clone, Copy, Debug)]
pub(super) struct Origin {
    pub a: i64,
    pub b: i64,
    pub f16: i64,
    pub last_rows: bool,
}

impl Default for Origin {
    fn default() -> Self {
        Self {
            a: 0,
            b: 0,
            f16: 0,
            last_rows: true,
        }
    }
}

fn shifted(o: &Operand, row: i64, col: i64) -> Operand {
    let (rs, cs) = o.logical_strides();
    Operand {
        offset: o.offset + row * rs + col * cs,
        ..*o
    }
}

fn region(allocation: &str, start: i64, end: i64) -> Region {
    Region {
        allocation: allocation.into(),
        bytes: start as u64..end as u64,
    }
}

fn operand_regions(
    name: &str,
    o: &Operand,
    row: i64,
    col: i64,
    rows: i64,
    cols: i64,
) -> Vec<Region> {
    let (rs, cs) = o.logical_strides();
    let item = Hw::item(o.dtype);
    let count = if rs == 0 { 1 } else { rows };
    (0..count)
        .map(|r| {
            let start = o.offset + (row + r) * rs + col * cs;
            region(
                name,
                start * item,
                (start + if cs == 0 { 1 } else { cols }) * item,
            )
        })
        .collect()
}

fn gcd(mut a: i64, mut b: i64) -> i64 {
    while b != 0 {
        (a, b) = (b, a % b);
    }
    a
}

fn lcm(a: i64, b: i64) -> i64 {
    a / gcd(a, b) * b
}

/// Physical runs of complete MX entries, retaining the producer's packing.
#[allow(clippy::too_many_arguments)]
fn packed_rect(
    name: &str,
    row: i64,
    rows: i64,
    col: i64,
    cols: i64,
    groups: i64,
    nk: i64,
    pitch: i64,
    hw: &Hw,
) -> Vec<Region> {
    let chunk = nk * hw.kblock;
    let chunks = pitch / chunk;
    let mut runs = Vec::new();
    let mut lo = row / hw.band;
    let hi = (row + rows) / hw.band;
    while lo < hi {
        let tile = lo / groups;
        let end = hi.min((tile + 1) * groups);
        for k in col / chunk..(col + cols) / chunk {
            let first = ((tile * chunks + k) * groups + lo % groups) * nk;
            let bytes = hw.kblock * hw.band;
            runs.push(region(
                name,
                first * bytes,
                (first + (end - lo) * nk) * bytes,
            ));
        }
        lo = end;
    }
    runs
}

fn packed_regions(name: &str, row: i64, rows: i64, groups: i64, mm: &Mm, hw: &Hw) -> Vec<Region> {
    packed_rect(name, row, rows, 0, mm.kp, groups, mm.nk, mm.kp, hw)
}

pub(super) fn local(shape: &Shape<'_>, share: &ShardTiles, mach: &Machine) -> R<Mm> {
    let mm = shape.mm;
    if shape.class.is_some() {
        return Ok(Mm {
            bt: 1,
            m: share.valid_extent[0],
            mp: share.padded_extent[0],
            n: share.valid_extent[1],
            shared: true,
            ..*mm
        });
    }
    plan(
        1,
        share.valid_extent[0],
        share.valid_extent[1],
        mm.k,
        true,
        mm.quantum,
        if mm.nk > 1 {
            Pairing::Vector
        } else {
            Pairing::Unpaired
        },
        mach,
        shape.knobs,
    )
}

/// M flattens batch row tiles; N and every factorization use column tiles.
/// Whole physical write lines and packing scale groups have a single owner.
pub fn split_options(
    shape: &Shape<'_>,
    request: &SplitRequest,
    mach: &Machine,
) -> R<Vec<SplitOption>> {
    let mm = shape.mm;
    let hw = Hw::of(mach);
    let p = request.nodes.ids().len();
    let row_unit = if shape.class.is_some() {
        mm.tm
    } else if shape.out.kind == Kind::Row {
        (mach.card.dram.line_bytes as i64 / Hw::item(shape.out.dtype)).max(mm.quantum)
    } else {
        mm.quantum
    };
    let mt = cdiv(mm.m, row_unit);
    let column_unit = shape.demands.iter().fold(
        if shape.class.is_some() {
            mm.tn
        } else {
            hw.kblock
        },
        |unit, d| lcm(unit, hw.kblock * d.nk),
    );
    let nt = mm.n / column_unit;
    let mut axes = vec![(SplitAxis::M, p, 1, "m".to_string())];
    let columns = expr::reduces(shape.e).is_empty() && shape.out.kind != Kind::Row;
    if columns {
        axes.push((SplitAxis::N, 1, p, "n".into()));
    }
    for pm in 1..=p {
        if p % pm == 0 && (columns || p / pm == 1) {
            axes.push((
                SplitAxis::MN { pm, pn: p / pm },
                pm,
                p / pm,
                format!("mn-{pm}x{}", p / pm),
            ));
        }
    }
    let mut options = Vec::new();
    for (axis, pm, pn, id) in axes {
        let mr = partition_tiles(mm.bt * mt, pm)?;
        let nr = partition_tiles(nt, pn)?;
        let mut shards = Vec::new();
        for (mi, rows) in mr.iter().enumerate() {
            for (ni, cols) in nr.iter().enumerate() {
                let node = mi * pn + ni;
                if rows.is_empty() || cols.is_empty() {
                    shards.push(ShardTiles {
                        logical_node: node,
                        tile_ranges: vec![rows.clone(), cols.clone()],
                        logical_origin: vec![0, 0, 0],
                        valid_extent: vec![0, 0],
                        padded_extent: vec![0, 0],
                        read_regions: vec![],
                        write_regions: vec![],
                        scratch_regions: vec![],
                        local_schedule: "empty".into(),
                        role: ShardRole::Empty,
                    });
                    continue;
                }
                for batch in rows.start / mt..=(rows.end - 1) / mt {
                    let r0 = (rows.start.max(batch * mt) - batch * mt) * row_unit;
                    let r1 = ((rows.end.min((batch + 1) * mt) - batch * mt) * row_unit).min(mm.mp);
                    let c0 = cols.start * column_unit;
                    let width = (cols.end - cols.start) * column_unit;
                    let grow = batch * mm.mp + r0;
                    let mut reads =
                        if shape.class.is_none() && takes_f16(mm.k, mm.nk, shape.knobs, mach) {
                            vec![region(
                                "a",
                                (batch * mm.m + r0) * mm.k * 2,
                                (batch * mm.m + r1.min(mm.m)) * mm.k * 2,
                            )]
                        } else {
                            packed_regions("a", grow, r1 - r0, mm.tm / hw.band, mm, &hw)
                        };
                    let brow = if mm.shared { c0 } else { batch * mm.n + c0 };
                    reads.extend(packed_regions("b", brow, width, mm.tn / hw.band, mm, &hw));
                    for (i, o) in shape.extras.iter().enumerate() {
                        if let Some(read) = shape.made.get(&(i + 1)) {
                            let (_, given) =
                                prepared::input_ports(read, &format!("p{}_", i + 1), mach)?;
                            for (name, spec) in given {
                                reads.push(region(
                                    &name,
                                    0,
                                    spec.shape.iter().product::<i64>() * Hw::item(spec.dtype),
                                ));
                            }
                        } else {
                            reads.extend(operand_regions(
                                &format!("in{}", i + 1),
                                o,
                                grow,
                                c0,
                                r1 - r0,
                                width,
                            ));
                        }
                    }
                    let mut writes = operand_regions("out", shape.out, grow, c0, r1 - r0, width);
                    for (i, d) in shape.demands.iter().enumerate() {
                        let end = if r1 >= mm.m { d.rp } else { r1 };
                        writes.extend(packed_rect(
                            &format!("q{i}"),
                            r0,
                            end - r0,
                            c0,
                            width,
                            d.groups,
                            d.nk,
                            d.cp,
                            &hw,
                        ));
                    }
                    let mut share = ShardTiles {
                        logical_node: node,
                        tile_ranges: vec![
                            rows.start.max(batch * mt)..rows.end.min((batch + 1) * mt),
                            cols.clone(),
                        ],
                        logical_origin: vec![batch, r0, c0],
                        valid_extent: vec![r1.min(mm.m) - r0, width],
                        padded_extent: vec![r1 - r0, width],
                        read_regions: reads,
                        write_regions: writes,
                        scratch_regions: vec![],
                        local_schedule: String::new(),
                        role: ShardRole::Compute,
                    };
                    let pl = local(shape, &share, mach)?;
                    share.local_schedule = pl.py().repr();
                    shards.push(share);
                }
            }
        }
        let mut output = OutputLayout {
            form: "producer".into(),
            ..OutputLayout::default()
        };
        for batch in 0..mm.bt {
            output.valid_region.extend(operand_regions(
                "out",
                shape.out,
                batch * mm.mp,
                0,
                (mt * row_unit).min(mm.mp),
                mm.n,
            ));
        }
        for (i, d) in shape.demands.iter().enumerate() {
            output.valid_region.extend(packed_rect(
                &format!("q{i}"),
                0,
                d.rp,
                0,
                d.cp,
                d.groups,
                d.nk,
                d.cp,
                &hw,
            ));
        }
        for share in &mut shards {
            coalesce(&mut share.write_regions);
        }
        for s in &shards {
            output
                .ownership
                .extend(s.write_regions.iter().cloned().map(|r| (s.logical_node, r)));
        }
        let assembled = line_owners(&mut output, &shards, mach.card.dram.line_bytes as u64);
        validate_ownership(&output)?;
        if assembled {
            for (index, share) in shards.iter_mut().enumerate() {
                let private: Vec<_> = share
                    .write_regions
                    .iter()
                    .filter(|r| r.allocation == "out")
                    .map(|r| {
                        let lo = private_byte(shape, share, r.bytes.start) as u64;
                        Region {
                            allocation: format!("boundary_{index}"),
                            bytes: lo..lo + r.bytes.end - r.bytes.start,
                        }
                    })
                    .collect();
                share.scratch_regions.extend(private);
                share.write_regions.retain(|r| r.allocation != "out");
            }
            for owner in 0..p {
                let mut reads = Vec::new();
                for producer in &shards {
                    if producer.role == ShardRole::Empty {
                        continue;
                    }
                    let origin = &producer.logical_origin;
                    let row = origin[0] * mm.mp + origin[1];
                    let produced = operand_regions(
                        "out",
                        shape.out,
                        row,
                        origin[2],
                        producer.padded_extent[0],
                        producer.padded_extent[1],
                    );
                    let consumes = output.ownership.iter().any(|(node, region)| {
                        *node == owner
                            && region.allocation == "out"
                            && produced.iter().any(|r| {
                                r.bytes.start < region.bytes.end && region.bytes.start < r.bytes.end
                            })
                    });
                    if consumes {
                        reads.extend(producer.scratch_regions.iter().cloned());
                    }
                }
                if let Some(share) = shards
                    .iter_mut()
                    .find(|s| s.logical_node == owner && s.role != ShardRole::Empty)
                {
                    share.read_regions.extend(reads);
                    share.write_regions.extend(
                        output
                            .ownership
                            .iter()
                            .filter(|(n, r)| *n == owner && r.allocation == "out")
                            .map(|(_, r)| r.clone()),
                    );
                }
            }
        }
        let active_nodes = (0..p)
            .filter(|n| {
                shards
                    .iter()
                    .any(|s| s.logical_node == *n && s.role != ShardRole::Empty)
            })
            .count();
        options.push(SplitOption {
            id,
            axis,
            p,
            active_nodes,
            shards,
            output,
            events: vec![],
            conversions: usize::from(assembled),
            cost: CostEstimate::default(),
            legality: LegalityWitness {
                numerics: request.numerics.clone(),
                clauses: vec![
                    "disjoint output lines; unchanged packed K chunks and accumulation order"
                        .into(),
                ],
            },
        });
    }
    if matches!(p, 2 | 4) && mm.kp > 0 && shape.class.is_none() {
        let mut single = request.clone();
        single.nodes = crate::split::NodeSet::new(vec![request.nodes.ids()[0]], mach)?;
        single.numerics.reduction_order.clear();
        let base = split_options(shape, &single, mach)?.remove(0);
        let cuts = partition_tiles(mm.kp / mm.kc(&hw), p)?;
        let active: Vec<_> = cuts
            .iter()
            .enumerate()
            .filter(|(_, cut)| !cut.is_empty())
            .map(|(rank, _)| rank)
            .collect();
        for tree in [true, false] {
            let (edges, root) = peer::reduction(p, tree, &active);
            if !request.numerics.reduction_order.is_empty()
                && request.numerics.reduction_order != edges
            {
                continue;
            }
            let mut output = base.output.clone();
            for (owner, _) in &mut output.ownership {
                *owner = root;
            }
            let mut shards = Vec::new();
            for (rank, cut) in cuts.iter().enumerate() {
                let lo = cut.start * mm.kc(&hw);
                let hi = cut.end * mm.kc(&hw);
                let mut reads = Vec::new();
                if !cut.is_empty() {
                    if takes_f16(mm.k, mm.nk, shape.knobs, mach) {
                        for row in 0..mm.bt * mm.m {
                            reads.push(region(
                                "a",
                                (row * mm.k + lo) * 2,
                                (row * mm.k + hi.min(mm.k)) * 2,
                            ));
                        }
                    } else {
                        reads.extend(packed_rect(
                            "a",
                            0,
                            mm.bt * mm.mp,
                            lo,
                            hi - lo,
                            mm.tm / hw.band,
                            mm.nk,
                            mm.kp,
                            &hw,
                        ));
                    }
                    reads.extend(packed_rect(
                        "b",
                        0,
                        if mm.shared { mm.n } else { mm.bt * mm.n },
                        lo,
                        hi - lo,
                        mm.tn / hw.band,
                        mm.nk,
                        mm.kp,
                        &hw,
                    ));
                }
                if rank == root {
                    reads.extend(
                        base.shards
                            .iter()
                            .flat_map(|s| s.read_regions.iter())
                            .filter(|r| r.allocation != "a" && r.allocation != "b")
                            .cloned(),
                    );
                }
                shards.push(ShardTiles {
                    logical_node: rank,
                    tile_ranges: vec![0..mm.bt * (mm.mp / mm.tm), 0..mm.n / mm.tn, cut.clone()],
                    logical_origin: vec![0, 0, 0, lo],
                    valid_extent: vec![mm.bt * mm.m, mm.n, (hi.min(mm.k) - lo).max(0)],
                    padded_extent: vec![mm.bt * mm.mp, mm.n, hi - lo],
                    read_regions: reads,
                    write_regions: if rank == root {
                        output.valid_region.clone()
                    } else {
                        vec![]
                    },
                    scratch_regions: vec![],
                    local_schedule: mm.py().repr(),
                    role: if cut.is_empty() {
                        ShardRole::Empty
                    } else if rank == root {
                        ShardRole::ReductionRoot
                    } else if edges.iter().any(|&(_, to)| to == rank) {
                        ShardRole::ReductionIntermediate
                    } else {
                        ShardRole::Compute
                    },
                });
            }
            let mut events = Vec::new();
            for &(from, to) in &edges {
                for (kind, label) in [
                    (ktpu_l2::lower::events::EventKind::AccumulatorReady, "ready"),
                    (ktpu_l2::lower::events::EventKind::PeerComplete, "complete"),
                ] {
                    let producer = request.nodes.ids()[to];
                    let receiver = request.nodes.ids()[from];
                    events.push(crate::split::EventSpec {
                        id: crate::split::EventId(format!("@{label}:{producer}:{receiver}@")),
                        producers: vec![producer],
                        consumers: vec![receiver],
                        released_regions: vec![],
                        acquire_regions: vec![],
                        kind,
                        count: 1,
                        lifetime: 0..1,
                    });
                }
            }
            options.push(SplitOption {
                id: if tree { "k-tree" } else { "k-serial" }.into(),
                axis: SplitAxis::K,
                p,
                active_nodes: active.len(),
                shards,
                output,
                events,
                conversions: 0,
                cost: CostEstimate::default(),
                legality: LegalityWitness {
                    numerics: crate::split::NumericContract {
                        precision: "generator".into(),
                        reduction_order: edges,
                    },
                    clauses: vec![
                        "whole packed K chunks; native FP22 peer reduction; root epilogue".into(),
                    ],
                },
            });
        }
    }
    if !request.numerics.reduction_order.is_empty() {
        options.retain(|o| {
            o.legality.numerics.reduction_order == request.numerics.reduction_order
                && matches!(o.axis, SplitAxis::K)
        });
    }
    Ok(options)
}

pub(crate) fn emit(
    t: &mut Trace,
    b: &[Buf],
    shape: &Shape<'_>,
    request: &SplitRequest,
    option: &SplitOption,
    mach: &Machine,
) -> R<()> {
    if matches!(option.axis, SplitAxis::K) {
        let plan = peer::Plan::new(&request.nodes, option);
        return emit_local(
            t,
            b,
            shape.mm,
            shape.e,
            shape.extras,
            shape.out,
            shape.knobs,
            mach,
            shape.made,
            shape.demands,
            Origin::default(),
            Some(&plan),
        );
    }
    let mut private = Vec::new();
    if option.conversions != 0 {
        for share in &option.shards {
            if share.role == ShardRole::Empty {
                private.push(None);
            } else {
                let spec = &b[2 + shape.extras.len()].spec;
                let dims = if shape.demands.is_empty() {
                    match shape.out.kind {
                        Kind::Row => vec![share.padded_extent[0]],
                        _ => share.padded_extent.clone(),
                    }
                } else {
                    spec.shape.clone()
                };
                private.push(Some(t.buffer(&dims, spec.dtype, spec.layout.clone())?));
            }
        }
    }
    let mut index = 0;
    emit_shares(t, &request.nodes, &option.shards, |t, share| {
        while option.shards[index].role == ShardRole::Empty {
            index += 1;
        }
        let mut local_bufs = b.to_vec();
        if let Some(Some(buffer)) = private.get(index) {
            local_bufs[2 + shape.extras.len()] = buffer.clone();
        }
        index += 1;
        let mm = shape.mm;
        let [batch, row, col] = share.logical_origin[..] else {
            return fail(
                "DSL.KERN.020",
                "a matmul share needs batch, row and column origins",
            );
        };
        let local = local(shape, share, mach)?;
        let grow = batch * mm.mp + row;
        let extras: Vec<_> = shape.extras.iter().map(|o| shifted(o, grow, col)).collect();
        let out = if option.conversions != 0 && shape.demands.is_empty() {
            Operand {
                offset: 0,
                pitch: local.n,
                rows: local.mp,
                ..*shape.out
            }
        } else {
            shifted(shape.out, grow, col)
        };
        let origin = Origin {
            a: grow,
            b: if mm.shared { col } else { batch * mm.n + col },
            f16: batch * mm.m + row,
            last_rows: row + share.padded_extent[0] >= mm.m,
        };
        let demands: Vec<_> = shape
            .demands
            .iter()
            .map(|d| QDemand {
                cp: local.n,
                rp: local.mp,
                ..*d
            })
            .collect();
        if let Some(class) = shape.class {
            return crate::gen::classes::mm::emit(
                t,
                &local_bufs,
                &local,
                shape.e,
                &extras,
                &out,
                class,
                mach,
                origin.a,
                origin.b,
            );
        }
        emit_local(
            t,
            &local_bufs,
            &local,
            shape.e,
            &extras,
            &out,
            shape.knobs,
            mach,
            shape.made,
            &demands,
            origin,
            None,
        )
    })?;
    if option.conversions != 0 {
        assemble(t, b, shape, request, option, &private, mach)?;
    }
    Ok(())
}

/// Build explicit shares and price each node's complete local schedule.
pub fn kernel(
    shape: &Shape<'_>,
    request: &SplitRequest,
    mach: &Machine,
) -> R<crate::split::PlannedKernel> {
    use crate::split::{choose_split, CostFeatures, LocalTiming, PlannedKernel};

    let options = split_options(shape, request, mach)?;
    let mut priced = Vec::new();
    for option in options {
        let mut kernels = Vec::new();
        if option.conversions != 0 || matches!(option.axis, SplitAxis::K) {
            let whole = build_option(shape, request, &option, mach)?;
            let cycles = g::cycles(&whole.graph, mach)? - mach.vc.launch_front as i64;
            let active = option.active_nodes;
            priced.push((
                option,
                CostFeatures {
                    share_cycles: vec![cycles; active],
                    dispatch_intercept: mach.vc.launch_front as i64,
                },
            ));
            continue;
        }
        for node in 0..option.p {
            let mut part = option.clone();
            part.shards.retain(|s| s.logical_node == node);
            if part.shards.iter().all(|s| s.role == ShardRole::Empty) {
                continue;
            }
            kernels.push(build_option(shape, request, &part, mach)?);
        }
        priced.push((option, CostFeatures::of_shares(&kernels, mach)?));
    }
    let mut split = choose_split(priced, &LocalTiming, request.force.as_deref())?;
    let kernel = build_option(shape, request, &split, mach)?;
    split.output.tensors = kernel
        .graph
        .ports
        .iter()
        .filter(|p| p.role == g::Role::Out)
        .map(|p| (p.name.clone(), p.spec.clone()))
        .collect();
    validate_ownership(&split.output)?;
    Ok(PlannedKernel {
        kernel,
        l1: None,
        split,
        request: request.clone(),
    })
}

/// Assign every destination line to the first producer touching it. A repair
/// phase copies only produced bytes, leaving the caller's other bytes intact.
fn line_owners(output: &mut OutputLayout, shares: &[ShardTiles], line: u64) -> bool {
    let mut owners = BTreeMap::new();
    let mut conflict = false;
    for share in shares {
        for r in &share.write_regions {
            if r.allocation != "out" || r.bytes.is_empty() {
                continue;
            }
            for l in r.bytes.start / line..=(r.bytes.end - 1) / line {
                let owner = owners.entry(l).or_insert(share.logical_node);
                conflict |= *owner != share.logical_node;
            }
        }
    }
    if !conflict {
        return false;
    }
    output.ownership.retain(|(_, r)| r.allocation != "out");
    for share in shares {
        for r in share.write_regions.iter().filter(|r| r.allocation == "out") {
            let mut lo = r.bytes.start;
            while lo < r.bytes.end {
                let l = lo / line;
                let hi = r.bytes.end.min((l + 1) * line);
                output.ownership.push((
                    owners[&l],
                    Region {
                        allocation: "out".into(),
                        bytes: lo..hi,
                    },
                ));
                lo = hi;
            }
        }
    }
    true
}

/// A node owns every partial transfer to each of its destination lines. The
/// lowering sees the producer scratch reads and inserts the release/acquire edge.
fn assemble(
    t: &mut Trace,
    b: &[Buf],
    shape: &Shape<'_>,
    request: &SplitRequest,
    option: &SplitOption,
    private: &[Option<Buf>],
    mach: &Machine,
) -> R<()> {
    let dst = &b[2 + shape.extras.len()];
    let word = mach.die.word_bytes as u64;
    for owner in 0..option.p {
        let runs: Vec<_> = option
            .output
            .ownership
            .iter()
            .filter(|(n, r)| *n == owner && r.allocation == "out")
            .collect();
        if runs.is_empty() {
            continue;
        }
        t.on_node(request.nodes.ids()[owner], |t| {
            for (_, run) in &runs {
                for (share, src) in option.shards.iter().zip(private) {
                    let Some(src) = src else { continue };
                    let origin = &share.logical_origin;
                    let row = origin[0] * shape.mm.mp + origin[1];
                    let mut produced = operand_regions(
                        "out",
                        shape.out,
                        row,
                        origin[2],
                        share.padded_extent[0],
                        share.padded_extent[1],
                    );
                    coalesce(&mut produced);
                    for produced in &produced {
                        let lo = run.bytes.start.max(produced.bytes.start);
                        let hi = run.bytes.end.min(produced.bytes.end);
                        if lo >= hi {
                            continue;
                        }
                        if lo % word != 0 || (hi - lo) % word != 0 {
                            return fail(
                                "DSL.MOV.001",
                                "a boundary copy must cover complete transfer words",
                            );
                        }
                        let source = private_byte(shape, share, lo);
                        let walk = [(((hi - lo) / word) as i64, word as i64)];
                        t.copy(
                            &dst.whole(),
                            &src.whole(),
                            Some((&walk, Some(source))),
                            Some((&walk, Some(lo as i64))),
                        )?;
                    }
                }
            }
            Ok(())
        })?;
    }
    Ok(())
}

fn coalesce(regions: &mut Vec<Region>) {
    regions.sort_by(|a, b| (&a.allocation, a.bytes.start).cmp(&(&b.allocation, b.bytes.start)));
    let mut merged: Vec<Region> = Vec::new();
    for region in regions.drain(..) {
        if let Some(last) = merged.last_mut() {
            if last.allocation == region.allocation && last.bytes.end == region.bytes.start {
                last.bytes.end = region.bytes.end;
                continue;
            }
        }
        merged.push(region);
    }
    *regions = merged;
}

fn build_option(
    shape: &Shape<'_>,
    request: &SplitRequest,
    option: &SplitOption,
    mach: &Machine,
) -> R<Kernel> {
    if let Some(class) = shape.class {
        return Ok(crate::gen::classes::mm::kernel_split(
            shape.mm,
            shape.e,
            shape.extras,
            shape.out,
            class,
            mach,
            Some((request, option)),
        )?
        .kernel);
    }
    mm_kernel_build(
        shape.mm,
        shape.e,
        shape.extras,
        shape.out,
        shape.knobs,
        mach,
        shape.made,
        shape.demands,
        Some((request, option)),
    )
}

fn private_byte(shape: &Shape<'_>, share: &ShardTiles, byte: u64) -> i64 {
    if !shape.demands.is_empty() {
        return byte as i64;
    }
    let origin = &share.logical_origin;
    let row = origin[0] * shape.mm.mp + origin[1];
    let item = Hw::item(shape.out.dtype);
    let at = shifted(shape.out, row, origin[2]).offset * item;
    let delta = byte as i64 - at;
    if shape.out.kind == Kind::Row {
        delta
    } else {
        let pitch = shape.out.pitch * item;
        delta / pitch * share.padded_extent[1] * item + delta % pitch
    }
}
