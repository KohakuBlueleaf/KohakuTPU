//! Output-rectangle ownership over an ordered sysnode grant.

use super::*;
use crate::split::{
    emit_shares, partition_tiles, validate_ownership, CostEstimate, LegalityWitness, OutputLayout,
    Region, ShardRole, ShardTiles, SplitAxis, SplitOption, SplitRequest,
};

#[derive(Debug)]
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
    plan_columns(
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
        packed_columns(shape.demands, &Hw::of(mach))?,
    )
}

/// Resolve the typed local geometry used for both pricing and emission.
fn local_option(shape: &Shape<'_>, share: &ShardTiles, axis: &SplitAxis, mach: &Machine) -> R<Mm> {
    match *axis {
        SplitAxis::RowGrain { .. } => {
            let knobs = Knobs {
                pad_m: false,
                ..*shape.knobs
            };
            let local_shape = Shape {
                knobs: &knobs,
                ..*shape
            };
            local(&local_shape, share, mach)
        }
        SplitAxis::Tiled { tm, tn } => Ok(Mm {
            bt: 1,
            m: share.valid_extent[0],
            mp: share.padded_extent[0],
            n: share.valid_extent[1],
            tm,
            tn,
            shared: true,
            ..*shape.mm
        }),
        SplitAxis::Batched { tm, tn } => Ok(Mm {
            bt: (share.tile_ranges[0].end - share.tile_ranges[0].start)
                / cdiv(shape.mm.m, shape.mm.quantum),
            tm,
            tn,
            ..*shape.mm
        }),
        SplitAxis::OwnerTiled { ref tiles }
        | SplitAxis::LayoutRing { ref tiles }
        | SplitAxis::ResidentB { ref tiles } => {
            let Some(tile) = tiles.iter().find(|tile| {
                tile.node == share.logical_node && tile.origin == share.logical_origin
            }) else {
                return fail("DSL.KERN.020", "an owner-local tile needs its output share");
            };
            Ok(Mm {
                bt: 1,
                m: share.valid_extent[0],
                mp: share.padded_extent[0],
                n: share.valid_extent[1],
                tm: tile.tm,
                tn: tile.tn,
                shared: true,
                ..*shape.mm
            })
        }
        _ => local(shape, share, mach),
    }
}

/// Independent output owners with every common legal local rectangle. K chunks,
/// producer packing, row padding and destination ownership remain unchanged.
fn joint_options(
    shape: &Shape<'_>,
    options: &[SplitOption],
    mach: &Machine,
) -> R<Vec<SplitOption>> {
    if shape.class.is_some() || !expr::reduces(shape.e).is_empty() || shape.out.kind == Kind::Row {
        return Ok(vec![]);
    }
    let hw = Hw::of(mach);
    let mm = shape.mm;
    let columns = packed_columns(shape.demands, &hw)?;
    let mut joint = Vec::new();
    for original in options {
        let shares: Vec<_> = original
            .shards
            .iter()
            .filter(|s| s.role != ShardRole::Empty)
            .collect();
        let Some(rows) = shares.iter().map(|s| s.padded_extent[0]).min() else {
            continue;
        };
        let most = rows
            .min(MAX_TM)
            .min(hw.bank / mm.nk * hw.band)
            .min(ring_rows(mach).max(mm.quantum));
        for tm in (mm.quantum..=most).step_by(mm.quantum as usize) {
            if tm % hw.band != 0 || shares.iter().any(|s| s.padded_extent[0] % tm != 0) {
                continue;
            }
            for tn in (columns..=hw.max_block()).step_by(columns as usize) {
                if (tm / hw.band) * (tn / hw.band) > hw.acc_subtiles
                    || (tn / hw.band) * mm.nk > hw.bank
                    || shares.iter().any(|s| s.valid_extent[1] % tn != 0)
                {
                    continue;
                }
                let mut option = original.clone();
                option.id = format!("{}-tile-{tm}x{tn}", original.id);
                option.axis = SplitAxis::Tiled { tm, tn };
                for share in &mut option.shards {
                    if share.role != ShardRole::Empty {
                        share.local_schedule =
                            local_option(shape, share, &option.axis, mach)?.py().repr();
                    }
                }
                joint.push(option);
            }
        }
    }
    Ok(joint)
}

/// Local-wave candidates retain each share's own divisors and packing geometry.
/// The inexpensive geometry price seeds two complete emitted alternatives.
fn owner_options(
    shape: &Shape<'_>,
    options: &[SplitOption],
    mach: &Machine,
) -> R<Vec<SplitOption>> {
    if shape.class.is_some() || !expr::reduces(shape.e).is_empty() || shape.out.kind == Kind::Row {
        return Ok(vec![]);
    }
    let hw = Hw::of(mach);
    let mm = shape.mm;
    let columns = packed_columns(shape.demands, &hw)?;
    let mut result = Vec::new();
    for original in options {
        'policy: for (waves, reuse_b) in [(1, false), (2, false), (1, true), (2, true)] {
            let mut tiles = Vec::new();
            for share in original
                .shards
                .iter()
                .filter(|s| s.role != ShardRole::Empty)
            {
                let rows = share.padded_extent[0];
                let cols = share.valid_extent[1];
                let max_rows = rows
                    .min(MAX_TM)
                    .min(hw.bank / mm.nk * hw.band)
                    .min(ring_rows(mach).max(mm.quantum));
                let most_tiles = (rows / mm.quantum) * (cols / columns);
                let wanted = (waves * hw.mg).min(most_tiles);
                let mut best = None;
                for tm in (mm.quantum..=max_rows).step_by(mm.quantum as usize) {
                    if rows % tm != 0 || tm % hw.band != 0 {
                        continue;
                    }
                    for tn in (columns..=cols.min(hw.max_block())).step_by(columns as usize) {
                        if cols % tn != 0
                            || (tm / hw.band) * (tn / hw.band) > hw.acc_subtiles
                            || (tn / hw.band) * mm.nk > hw.bank
                            || (rows / tm) * (cols / tn) < wanted
                        {
                            continue;
                        }
                        if reuse_b {
                            let local = Mm {
                                bt: 1,
                                m: share.valid_extent[0],
                                mp: rows,
                                n: cols,
                                tm,
                                tn,
                                shared: true,
                                ..*mm
                            };
                            let r = ring::resolve(
                                &local,
                                &computed(shape.e, shape.knobs),
                                shape.out,
                                shape.knobs,
                                mach,
                            );
                            let resident = ring::resident(r, &local, shape.knobs, mach);
                            if rows / tm < 2
                                || (tn / hw.band) * (mm.kp / hw.kblock) > hw.bank
                                || r.tiles != 1
                                || r.quant != 0
                                || (shape.knobs.activation_any
                                    && takes_f16(mm.k, mm.nk, shape.knobs, mach))
                                || (r.depth > 0 && resident.depth == 0)
                                || !shape.demands.is_empty()
                            {
                                continue;
                            }
                        }
                        let group_tm = if reuse_b { 2 * tm } else { tm };
                        let price = (
                            wave_cost(cdiv(rows, group_tm), cols, mm.kp, mm.nk, group_tm, tn, &hw),
                            tile_cost(rows, cols, mm.kp, mm.nk, group_tm, tn, &hw),
                            -tm,
                            -tn,
                        );
                        if best.as_ref().is_none_or(|(p, _, _)| price < *p) {
                            best = Some((price, tm, tn));
                        }
                    }
                }
                let Some((_, tm, tn)) = best else {
                    continue 'policy;
                };
                tiles.push(crate::split::LocalTile {
                    node: share.logical_node,
                    origin: share.logical_origin.clone(),
                    tm,
                    tn,
                });
            }
            let mut option = original.clone();
            option.id = if reuse_b {
                format!("{}-resident-b-{waves}", original.id)
            } else {
                format!("{}-owner-waves-{waves}", original.id)
            };
            option.axis = if reuse_b {
                SplitAxis::ResidentB { tiles }
            } else {
                SplitAxis::OwnerTiled { tiles }
            };
            for share in &mut option.shards {
                if share.role != ShardRole::Empty {
                    share.local_schedule =
                        local_option(shape, share, &option.axis, mach)?.py().repr();
                    if reuse_b {
                        share.local_schedule = format!("resident-b {}", share.local_schedule);
                    }
                }
            }
            if !reuse_b
                && shape.knobs.ring_depth > 0
                && shape.out.kind == Kind::Full
                && expr::count(shape.e) == 0
                && shape.demands.is_empty()
                && !takes_f16(mm.k, mm.nk, shape.knobs, mach)
            {
                if let SplitAxis::OwnerTiled { tiles } = &option.axis {
                    let mut pipelined = option.clone();
                    pipelined.id = format!("{}-layout-ring-{waves}", original.id);
                    pipelined.axis = SplitAxis::LayoutRing {
                        tiles: tiles.clone(),
                    };
                    for share in &mut pipelined.shards {
                        if share.role != ShardRole::Empty {
                            let local = local_option(shape, share, &pipelined.axis, mach)?;
                            let ring = ring::resolve_layout(
                                &local,
                                shape.e,
                                shape.out,
                                shape.knobs,
                                mach,
                                true,
                            );
                            share.local_schedule = format!(
                                "layout-ring {} {}",
                                share.local_schedule,
                                ring.py().repr(),
                            );
                        }
                    }
                    result.push(pipelined);
                }
            }
            result.push(option);
        }
    }
    Ok(result)
}

/// Identity-layout delivery competes at the existing partition and common-tile
/// geometries, independently of the owner-local wave heuristic.
fn layout_options(
    shape: &Shape<'_>,
    options: &[SplitOption],
    mach: &Machine,
) -> R<Vec<SplitOption>> {
    if shape.class.is_some()
        || shape.knobs.ring_depth == 0
        || shape.out.kind != Kind::Full
        || expr::count(shape.e) != 0
        || !shape.demands.is_empty()
        || takes_f16(shape.mm.k, shape.mm.nk, shape.knobs, mach)
    {
        return Ok(vec![]);
    }
    let mut result = Vec::new();
    'candidate: for original in options {
        if !matches!(
            original.axis,
            SplitAxis::M
                | SplitAxis::N
                | SplitAxis::MN { .. }
                | SplitAxis::Groups
                | SplitAxis::Tiled { .. }
        ) {
            continue;
        }
        let mut tiles = Vec::new();
        let mut option = original.clone();
        let mut active = false;
        for share in &mut option.shards {
            if share.role == ShardRole::Empty {
                continue;
            }
            let local = local_option(shape, share, &original.axis, mach)?;
            // Typed local tiles retain the declared physical owner extent.
            if local.mp != share.padded_extent[0] {
                continue 'candidate;
            }
            let ring = ring::resolve_layout(&local, shape.e, shape.out, shape.knobs, mach, true);
            active |= ring.depth > 0;
            tiles.push(crate::split::LocalTile {
                node: share.logical_node,
                origin: share.logical_origin.clone(),
                tm: local.tm,
                tn: local.tn,
            });
            share.local_schedule =
                format!("layout-ring {} {}", local.py().repr(), ring.py().repr());
        }
        if active {
            option.id = format!("{}-layout-ring", original.id);
            option.axis = SplitAxis::LayoutRing { tiles };
            result.push(option);
        }
    }
    Ok(result)
}

/// Emission groups consecutive complete batches without changing manifest shards.
fn batch_shards(shares: &[ShardTiles]) -> Vec<ShardTiles> {
    let mut groups: Vec<ShardTiles> = Vec::new();
    for share in shares {
        if let Some(last) = groups.last_mut().filter(|last| {
            last.role != ShardRole::Empty
                && share.role != ShardRole::Empty
                && last.logical_node == share.logical_node
                && last.tile_ranges[0].end == share.tile_ranges[0].start
        }) {
            last.tile_ranges[0].end = share.tile_ranges[0].end;
        } else {
            groups.push(share.clone());
        }
    }
    groups
}

/// Whole consecutive batches retain their global strides while sharing local waves.
fn batched_options(
    shape: &Shape<'_>,
    options: &[SplitOption],
    mach: &Machine,
) -> R<Vec<SplitOption>> {
    let mm = shape.mm;
    if mm.bt < 2
        || shape.class.is_some()
        || !shape.demands.is_empty()
        || !shape.made.is_empty()
        || shape.out.kind == Kind::Row
        || !expr::reduces(shape.e).is_empty()
    {
        return Ok(vec![]);
    }
    let mut result = Vec::new();
    for original in options
        .iter()
        .filter(|o| matches!(o.axis, SplitAxis::Groups))
    {
        if original.conversions != 0
            || original.shards.iter().any(|s| {
                s.role != ShardRole::Empty
                    && (s.logical_origin[1] != 0
                        || s.logical_origin[2] != 0
                        || s.valid_extent != [mm.m, mm.n]
                        || s.padded_extent != [mm.mp, mm.n])
            })
        {
            continue;
        }
        let shards = batch_shards(&original.shards);
        if shards.len() == original.shards.len() {
            continue;
        }
        let Some(first) = shards.iter().find(|s| s.role != ShardRole::Empty) else {
            continue;
        };
        let local = local(shape, first, mach)?;
        let mut geometries =
            std::collections::BTreeSet::from([(mm.tm, mm.tn), (local.tm, local.tn)]);
        geometries.extend(options.iter().filter_map(|candidate| match candidate.axis {
            SplitAxis::Tiled { tm, tn }
                if candidate.output.ownership == original.output.ownership =>
            {
                Some((tm, tn))
            }
            _ => None,
        }));
        for (tm, tn) in geometries {
            if mm.mp % tm != 0 || mm.n % tn != 0 {
                continue;
            }
            let mut option = original.clone();
            option.id = format!("{}-batch-{tm}x{tn}", original.id);
            option.axis = SplitAxis::Batched { tm, tn };
            for share in &mut option.shards {
                if share.role != ShardRole::Empty {
                    share.local_schedule = format!(
                        "batched {}",
                        local_option(shape, share, &option.axis, mach)?.py().repr()
                    );
                }
            }
            option.legality.clauses.push(
                "whole consecutive batches; original A/B batch strides and ordered K chunks".into(),
            );
            result.push(option);
        }
    }
    Ok(result)
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
    let column_unit = v::split::common_quantum(
        if shape.class.is_some() {
            mm.tn
        } else {
            hw.kblock
        },
        packed_columns(shape.demands, &hw)?,
    )?;
    let mut axes = vec![
        (SplitAxis::Unsplit, 1, 1, "p1".to_string()),
        (SplitAxis::M, p, 1, "m".to_string()),
    ];
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
    if mm.bt > 1 {
        axes.push((SplitAxis::Groups, p, 1, "groups".into()));
    }
    let mut axes: Vec<_> = axes
        .into_iter()
        .map(|(axis, pm, pn, id)| (axis, pm, pn, id, column_unit, row_unit))
        .collect();
    if columns && p > 1 && shape.class.is_none() {
        for grain in [hw.max_block() / 2, hw.max_block()] {
            if grain > column_unit && grain % column_unit == 0 && mm.n % grain == 0 {
                let ordinary = partition_tiles(mm.n / column_unit, p)?;
                let tiled = partition_tiles(mm.n / grain, p)?;
                if ordinary.iter().zip(&tiled).any(|(a, b)| {
                    a.start * column_unit != b.start * grain || a.end * column_unit != b.end * grain
                }) {
                    axes.push((
                        SplitAxis::N,
                        1,
                        p,
                        format!("n-grain-{grain}"),
                        grain,
                        row_unit,
                    ));
                }
            }
        }
    }
    if columns
        && p > 1
        && shape.class.is_none()
        && shape.demands.is_empty()
        && mm.mp == cdiv(mm.m, mm.quantum) * mm.quantum
    {
        let base_axes = axes.clone();
        for multiple in [1, 2] {
            let rows = multiple * hw.mg * row_unit;
            if rows <= row_unit || cdiv(mm.m, rows) >= cdiv(mm.m, row_unit) {
                continue;
            }
            for (axis, pm, pn, id, grain, _) in &base_axes {
                if *pm <= 1 || !matches!(axis, SplitAxis::M | SplitAxis::MN { .. }) {
                    continue;
                }
                let old_count = cdiv(mm.m, row_unit);
                let new_count = cdiv(mm.m, rows);
                let old = partition_tiles(mm.bt * old_count, *pm)?;
                let new = partition_tiles(mm.bt * new_count, *pm)?;
                // A flattened tile index names a batch and a within-batch row.
                let address =
                    |at: i64, count: i64, grain: i64| (at / count, (at % count * grain).min(mm.mp));
                if old.iter().zip(&new).all(|(a, b)| {
                    address(a.start, old_count, row_unit) == address(b.start, new_count, rows)
                        && address(a.end, old_count, row_unit) == address(b.end, new_count, rows)
                }) {
                    continue;
                }
                axes.push((
                    SplitAxis::RowGrain { rows },
                    *pm,
                    *pn,
                    format!("{id}-row-grain-{rows}"),
                    *grain,
                    rows,
                ));
            }
        }
    }
    let mut options = Vec::new();
    for (axis, pm, pn, id, grain, row_unit) in axes {
        let mt = cdiv(mm.m, row_unit);
        let nt = mm.n / grain;
        let mr = if matches!(axis, SplitAxis::Groups) {
            partition_tiles(mm.bt, pm)?
                .into_iter()
                .map(|r| r.start * mt..r.end * mt)
                .collect()
        } else {
            partition_tiles(mm.bt * mt, pm)?
        };
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
                    let c0 = cols.start * grain;
                    let width = (cols.end - cols.start) * grain;
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
                    let pl = local_option(shape, &share, &axis, mach)?;
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
    let owners = owner_options(shape, &options, mach)?;
    let joint = joint_options(shape, &options, mach)?;
    options.extend(joint);
    let layouts = layout_options(shape, &options, mach)?;
    options.extend(layouts);
    options.extend(owners);
    let batched = batched_options(shape, &options, mach)?;
    options.extend(batched);
    if p > 1 && mm.kp > 0 && shape.class.is_none() {
        let assignments: Vec<_> = options
            .iter()
            .filter_map(|option| match option.axis {
                SplitAxis::M => Some((option.clone(), p, 1)),
                SplitAxis::MN { pm, pn } if pm > 1 && pn > 1 => Some((option.clone(), pm, pn)),
                _ => None,
            })
            .collect();
        for (assignment, pm, pn) in assignments {
            for delivery in [
                crate::split::Delivery::Direct,
                crate::split::Delivery::Relay,
            ] {
                let mut option = assignment.clone();
                let suffix = match delivery {
                    crate::split::Delivery::Direct => "",
                    crate::split::Delivery::Relay => "-relay",
                };
                option.id = if pn == 1 {
                    format!("b-pass{suffix}")
                } else {
                    format!("b-pass-{pm}x{pn}{suffix}")
                };
                option.axis = SplitAxis::BPass { pm, pn, delivery };
                option.legality.clauses.extend([
                    "VC raw-word fill and CU_DATA buf=1 preserve the producer's MX7B layout".into(),
                    "private A and shared-A prerequisites retire before B-ready; COUNT is operand-B".into(),
                    "VC0 owns two half-L1 buffers during MG stages; all sends retire before reuse".into(),
                    "bank-generation ready/credit tokens alternate on receiver-scoped leased FIFOs".into(),
                ]);
                options.push(option);
            }
        }
    }
    if matches!(p, 2 | 4) && mm.kp > 0 && shape.class.is_none() {
        let base = options[0].clone();
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
    let batched;
    let shares = if matches!(option.axis, SplitAxis::Batched { .. }) {
        batched = batch_shards(&option.shards);
        &batched
    } else {
        &option.shards
    };
    let mut index = 0;
    emit_shares(t, &request.nodes, shares, |t, share| {
        while shares[index].role == ShardRole::Empty {
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
        let local = local_option(shape, share, &option.axis, mach)?;
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
        emit_local_schedule(
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
            matches!(option.axis, SplitAxis::ResidentB { .. }),
            matches!(option.axis, SplitAxis::LayoutRing { .. }),
        )
    })?;
    if option.conversions != 0 {
        assemble(t, b, shape, request, option, &private, mach)?;
    }
    Ok(())
}

/// Build explicit shares; retain all candidate prices for the process lifetime.
pub fn kernel(
    shape: &Shape<'_>,
    request: &SplitRequest,
    mach: &Machine,
) -> R<crate::split::PlannedKernel> {
    kernel_with_timing(shape, request, mach, false)
}

/// Price the selected single-node schedule when a sequence consumes its timing.
pub(crate) fn kernel_with_timing(
    shape: &Shape<'_>,
    request: &SplitRequest,
    mach: &Machine,
    price_unsplit: bool,
) -> R<crate::split::PlannedKernel> {
    kernel_with_policy(
        shape,
        request,
        mach,
        price_unsplit,
        super::super::chain::quant::Backend::Generator,
    )
}

/// Price and emit the selected quant schedule as part of every complete option.
pub(crate) fn kernel_with_policy(
    shape: &Shape<'_>,
    request: &SplitRequest,
    mach: &Machine,
    price_unsplit: bool,
    backend: super::super::chain::quant::Backend,
) -> R<crate::split::PlannedKernel> {
    use crate::split::cost::ChoiceCache;
    use crate::split::PlannedKernel;
    use std::sync::OnceLock;

    static CHOICES: OnceLock<ChoiceCache> = OnceLock::new();
    let mut unforced = request.clone();
    unforced.force = None;
    let key = format!(
        "{shape:?}/{unforced:?}/{backend:?}/{:?}/{}",
        mach.digest_bytes(),
        pricing_mutants(),
    );
    let equivalent = std::cell::RefCell::new(BTreeMap::<String, CostEstimate>::new());
    let mut split = CHOICES.get_or_init(ChoiceCache::default).choose_bounded_with_prices(
        key,
        request.force.as_deref(),
        |known| {
            // A cached exact option must seed the same equivalence table as a
            // newly priced option, including after a forced-only invocation.
            equivalent.borrow_mut().extend(known.values().filter_map(|option| {
                ordinary_schedule_key(option, &unforced).map(|key| (key, option.cost.clone()))
            }));
            let bounds = request.force.is_none() && pricing_mutants().is_empty();
            let candidates = split_options(shape, &unforced, mach)?
                .into_iter()
                .filter(|option| request.force.as_deref().is_none_or(|id| id == option.id))
                .map(|option| {
                    let floor = bounds
                        .then(|| resource_floor(shape, &unforced, &option, mach))
                        .flatten();
                    (option, floor)
                })
                .collect();
            Ok(candidates)
        },
        |mut option| {
            let key = ordinary_schedule_key(&option, &unforced);
            if let Some(cost) = key.as_ref().and_then(|key| equivalent.borrow().get(key).cloned()) {
                option.cost = cost;
                return Ok(option);
            }
            let option = price_option(shape, &unforced, option, mach, backend)?;
            if let Some(key) = key {
                equivalent.borrow_mut().insert(key, option.cost.clone());
            }
            Ok(option)
        },
        |option| {
            let kernel = build_option(shape, &unforced, option, mach, backend)?;
            if matches!(option.axis, SplitAxis::BPass { .. }) {
                let emitted = b_pass::emit(&kernel, option, &unforced, mach)?;
                let module = ktpu_l1::text::read(&emitted.text, "B delivery")
                    .map_err(|e| ktpu_machine::Refusal::new(&e.clause, e.message))?;
                crate::split::cost::l1_staging(
                    &module,
                    &kernel.graph.name,
                    mach,
                    request.nodes.staging_budget(),
                )
            } else {
                g::check_staging(&kernel.graph, mach, request.nodes.staging_budget())
            }
        },
    )?;
    let kernel = build_option(shape, request, &split, mach, backend)?;
    let l1 = if matches!(split.axis, SplitAxis::BPass { .. }) {
        let emitted = b_pass::emit(&kernel, &split, request, mach)?;
        emitted.manifest(&mut split, request);
        Some(emitted.text)
    } else {
        None
    };
    if price_unsplit && request.nodes.ids().len() == 1 {
        let features = crate::split::CostFeatures::of_shares(std::slice::from_ref(&kernel), mach)?;
        split = crate::split::cost::price_option(split, &features, &crate::split::LocalTiming)?;
    }
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
        l1,
        split,
        request: request.clone(),
    })
}

fn pricing_mutants() -> String {
    let mut active = Vec::new();
    for (names, on) in [
        (
            crate::mutants::NAMES,
            crate::mutants::on as fn(&str) -> bool,
        ),
        (crate::mutants::LOCAL, crate::mutants::on),
        (g::mutant::NAMES, g::mutant::on),
        (ktpu_l2::mutant::NAMES, ktpu_l2::mutant::on),
        (ktpu_l1::mutant::NAMES, ktpu_l1::mutant::on),
        (ktpu_machine::mutant::NAMES, ktpu_machine::mutant::on),
    ] {
        active.extend(names.iter().copied().filter(|name| on(name)));
    }
    active.join("/")
}

/// Model-service bound before graph construction. Ordinary products retain two
/// fills and one sweep per tile/K chunk. Resident pairs share B fill entries,
/// with one additional B sequence for an odd row tail, without changing sweeps. K owners retain their own chunks; B-pass replaces B fills and serializes local budgets. Epilogue, delivery,
/// drain and dependency costs are omitted, never estimated upward.
pub fn resource_floor(
    shape: &Shape<'_>,
    request: &SplitRequest,
    option: &SplitOption,
    mach: &Machine,
) -> Option<i64> {
    if shape.class.is_some() || !pricing_mutants().is_empty() {
        return None;
    }
    let hw = Hw::of(mach);
    let params = ktpu_l2::cost::resource::Params::planning(mach);
    let serial = matches!(option.axis, SplitAxis::BPass { .. });
    let unscoped = request.nodes.ids() == [0]
        && !matches!(
            option.axis,
            SplitAxis::Tiled { .. }
                | SplitAxis::Batched { .. }
                | SplitAxis::OwnerTiled { .. }
                | SplitAxis::ResidentB { .. }
                | SplitAxis::LayoutRing { .. }
        );
    let mut owners = BTreeMap::<usize, [u64; 3]>::new();
    for share in option
        .shards
        .iter()
        .filter(|s| s.role != ShardRole::Empty)
        .take(if unscoped { 1 } else { usize::MAX })
    {
        let mm = if unscoped || matches!(option.axis, SplitAxis::K) {
            *shape.mm
        } else {
            local_option(shape, share, &option.axis, mach).ok()?
        };
        let positive = [
            mm.bt, mm.mp, mm.n, mm.tm, mm.tn, mm.nk, hw.band, hw.kblock, hw.mg,
        ];
        if positive.iter().any(|&n| n <= 0) || mm.kp <= 0 {
            return None;
        }
        let chunks = if matches!(option.axis, SplitAxis::K) {
            *share.padded_extent.get(2)? / mm.kc(&hw)
        } else {
            mm.kp / mm.kc(&hw)
        };
        let tiles = u64::try_from(mm.bt)
            .ok()?
            .checked_mul(u64::try_from(mm.mp / mm.tm).ok()?)?
            .checked_mul(u64::try_from(mm.n / mm.tn).ok()?)?;
        let issues = tiles.checked_mul(u64::try_from(chunks).ok()?)?;
        let gm = u64::try_from(mm.tm / hw.band).ok()?;
        let gn = u64::try_from(mm.tn / hw.band).ok()?;
        let nk = u64::try_from(mm.nk).ok()?;
        let entries = if matches!(option.axis, SplitAxis::ResidentB { .. }) {
            let b_issues = u64::try_from(mm.bt)
                .ok()?
                .checked_mul(u64::try_from(cdiv(mm.mp / mm.tm, 2)).ok()?)?
                .checked_mul(u64::try_from(mm.n / mm.tn).ok()?)?
                .checked_mul(u64::try_from(chunks).ok()?)?;
            issues
                .checked_mul(nk)?
                .checked_mul(gm)?
                .checked_add(b_issues.checked_mul(nk)?.checked_mul(gn)?)?
        } else {
            issues
                .checked_mul(nk)?
                .checked_mul(if serial { gm } else { gm.checked_add(gn)? })?
        };
        let steps = issues.checked_mul(nk)?.checked_mul(gm)?.checked_mul(gn)?;
        let total = owners.entry(share.logical_node).or_default();
        for (sum, count) in total.iter_mut().zip([entries, issues, steps]) {
            *sum = sum.checked_add(count)?;
        }
    }
    let mut floor = 0.0f64;
    for [entries, issues, steps] in owners.into_values() {
        let local = params.matrix_floor(entries, issues, steps, u32::try_from(hw.mg).ok()?)?;
        floor = if serial {
            floor + local
        } else {
            floor.max(local)
        };
    }
    floor += mach.vc.launch_front as f64;
    // Leave a relative margin for floating point arithmetic and accumulated
    // task-clock rounding. Beyond exact integer conversion no pruning is used.
    if !floor.is_finite() || !(0.0..=(1u64 << 52) as f64).contains(&floor) {
        return None;
    }
    Some((floor * (1.0 - 1e-10)).floor() as i64 - 1)
}

/// Within one fixed shape/grant/backend, ordinary emission consults local
/// geometry, origins and boundary ownership, not the diagnostic split axis/ID.
fn ordinary_schedule_key(option: &SplitOption, request: &SplitRequest) -> Option<String> {
    if matches!(option.axis, SplitAxis::K | SplitAxis::BPass { .. }) {
        return None;
    }
    if request.nodes.ids() == [0]
        && !matches!(
            option.axis,
            SplitAxis::Tiled { .. }
                | SplitAxis::Batched { .. }
                | SplitAxis::OwnerTiled { .. }
                | SplitAxis::ResidentB { .. }
                | SplitAxis::LayoutRing { .. }
        )
    {
        return Some("unscoped".into());
    }
    let shares: Vec<_> = option
        .shards
        .iter()
        .map(|share| {
            (
                share.logical_node,
                &share.logical_origin,
                &share.valid_extent,
                &share.padded_extent,
                &share.local_schedule,
                &share.role,
            )
        })
        .collect();
    Some(format!(
        "{:?}",
        (shares, option.conversions, &option.output.ownership)
    ))
}

fn price_option(
    shape: &Shape<'_>,
    request: &SplitRequest,
    mut option: SplitOption,
    mach: &Machine,
    backend: super::super::chain::quant::Backend,
) -> R<SplitOption> {
    use crate::split::{CostFeatures, LocalTiming};

    if matches!(option.axis, SplitAxis::BPass { .. }) {
        let kernel = build_option(shape, request, &option, mach, backend)?;
        let emitted = b_pass::emit(&kernel, &option, request, mach)?;
        emitted.manifest(&mut option, request);
        option.cost = emitted.price(&kernel.graph.name, request, mach)?;
        return Ok(option);
    }
    let whole = build_option(shape, request, &option, mach, backend)?;
    let budgets = g::node_cycles(&whole.graph, mach)?;
    let mut share_cycles = Vec::new();
    for (rank, physical) in request.nodes.ids().iter().enumerate() {
        if option
            .shards
            .iter()
            .any(|share| share.logical_node == rank && share.role != ShardRole::Empty)
        {
            let cycles = budgets.get(physical).copied().ok_or_else(|| {
                ktpu_machine::Refusal::new(
                    "L2.COST.001",
                    "an active ownership share has no emitted physical-sysnode budget",
                )
            })?;
            share_cycles.push(cycles);
        }
    }
    crate::split::cost::price_option(
        option,
        &CostFeatures {
            share_cycles,
            dispatch_intercept: mach.vc.launch_front as i64,
        },
        &LocalTiming,
    )
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
    backend: super::super::chain::quant::Backend,
) -> R<Kernel> {
    let placement = if request.nodes.ids() == [0]
        && !matches!(
            option.axis,
            SplitAxis::Tiled { .. }
                | SplitAxis::Batched { .. }
                | SplitAxis::OwnerTiled { .. }
                | SplitAxis::ResidentB { .. }
                | SplitAxis::LayoutRing { .. }
        ) {
        None
    } else {
        Some((request, option))
    };
    if let Some(class) = shape.class {
        let kernel = crate::gen::classes::mm::kernel_split(
            shape.mm,
            shape.e,
            shape.extras,
            shape.out,
            class,
            mach,
            placement,
        )?
        .kernel;
        return super::super::chain::quant::kernel(kernel, backend, mach);
    }
    let kernel = mm_kernel_build(
        shape.mm,
        shape.e,
        shape.extras,
        shape.out,
        shape.knobs,
        mach,
        shape.made,
        shape.demands,
        placement,
    )?;
    super::super::chain::quant::kernel(kernel, backend, mach)
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
