//! Complete physical packed tiles with global source and destination origins.

use super::*;
use crate::gen::vector;
use crate::split::{PlannedKernel, SplitAxis, SplitRequest};

/// Maximal rectangles in a contiguous interval of physical packing tiles.
fn rectangles(first: i64, count: i64, rp: i64, cp: i64, g: i64, nk: i64) -> Vec<[i64; 4]> {
    let (rh, cw) = (4 * g, 32 * nk);
    let columns = cp / cw;
    let mut at = first;
    let end = first + count;
    let mut out = Vec::new();
    while at < end {
        let row = at / columns * rh;
        let col = at % columns * cw;
        if col == 0 && end - at >= columns {
            let height = ((end - at) / columns * rh).min(rp - row % rp);
            out.push([row, height, 0, cp]);
            at += height / rh * columns;
        } else {
            let width = (end - at).min(columns - at % columns) * cw;
            out.push([row, rh, col, width]);
            at += width / cw;
        }
    }
    out
}

#[allow(clippy::too_many_arguments)]
pub fn kernel(
    src: &Operand,
    rows: i64,
    cols: i64,
    rp: i64,
    cp: i64,
    side: Side,
    groups: i64,
    nk: i64,
    batches: i64,
    finite: bool,
    schedule: &[(&str, i64)],
    staged: bool,
    walked: Option<(i64, &[(i64, i64)], i64)>,
    mapped: Option<&[crate::gen::relayout::Logical]>,
    request: &SplitRequest,
    mach: &Machine,
) -> R<PlannedKernel> {
    let read = match mapped {
        Some(steps) => prepared::Read::Composed(steps.to_vec()),
        None => prepared::Read::Materialized(vec![]),
    };
    let signature = if let Some((bytes, levels, at)) = walked {
        quant_walk_kernel(
            bytes, levels, at, rows, cols, rp, cp, side, groups, nk, batches, mach, finite,
            schedule,
        )?
    } else {
        quant_kernel_scheduled(
            src, rows, cols, rp, cp, side, groups, nk, batches, mach, finite, schedule, &read,
            staged,
        )?
    };
    let (window_rows, window_rp) = if rows == rp {
        (batches * rows, batches * rp)
    } else {
        (rows, rp)
    };
    let tiles = batches * rp / (4 * groups) * (cp / (32 * nk));
    let tile_bytes = 128 * groups * nk;
    let shape = vector::split::Shape {
        rows: tiles,
        quantum: 1,
        reads: vec![],
        writes: vec![vector::split::Tensor {
            name: "q".into(),
            operand: Operand {
                kind: vector::Kind::Full,
                dtype: g::Dtype::F16,
                offset: 0,
                pitch: tile_bytes / 2,
                rows: tiles,
            },
            width: tile_bytes / 2,
            planes: vec![0],
        }],
        tokens: false,
    };
    let mut options = vector::split::split_options(&shape, request, mach)?;
    for option in &mut options {
        option.id = "destination-tiles".into();
        option.axis = SplitAxis::DestinationTiles;
        option.legality.clauses = vec![
            "complete (side, g, nk) tiles in physical order; disjoint destination lines".into(),
        ];
        for share in &mut option.shards {
            for [row, height, col, width] in rectangles(
                share.logical_origin[0],
                share.valid_extent[0],
                window_rp,
                cp,
                groups,
                nk,
            ) {
                let bi = row / window_rp;
                let lo = (row % window_rp).min(window_rows);
                let hi = (row % window_rp + height).min(window_rows);
                let left = col.min(cols);
                let right = (col + width).min(cols);
                if hi > lo && right > left {
                    let start = src.offset + (bi * window_rows + lo) * src.pitch + left;
                    let bytes = if mapped.is_some() {
                        let (_, spec) = &signature.given[0];
                        0..(spec.shape.iter().product::<i64>() * 2) as u64
                    } else if let Some((bytes, _, _)) = walked {
                        0..bytes as u64
                    } else {
                        (start * 2) as u64
                            ..((start + (hi - lo - 1) * src.pitch + right - left) * 2) as u64
                    };
                    share.read_regions.push(crate::split::Region {
                        allocation: if mapped.is_some() { "p_x" } else { "x" }.into(),
                        bytes,
                    });
                }
            }
        }
    }
    let hw = Hw::of(mach);
    let xs = if let Some((bytes, _, _)) = walked {
        [cdiv(bytes, hw.word_bytes), hw.lanes]
    } else {
        [
            cdiv(src.offset + batches * rows * src.pitch, src.pitch),
            src.pitch,
        ]
    };
    let qs = [batches * rp, cp];
    let ports = if mapped.is_some() {
        let (mut ports, _) = prepared::input_ports(&read, "p_", mach)?;
        ports.push(Port::output("q", &qs, side.dtype()).with_layout(side.layout(groups, nk)));
        ports
    } else {
        vec![
            Port::input("x", &xs, src.dtype),
            Port::output("q", &qs, side.dtype()).with_layout(side.layout(groups, nk)),
        ]
    };
    let given = signature.given.clone();
    let identity = signature.graph.name.clone();
    let vector = mapped.is_some() || vector_packs(nk, rows != rp || cols != cp, mach);
    let schedule_on = if vector { schedule } else { &[] };
    vector::split::kernel_options(options, signature, request, mach, |first, count| {
        if first == 0 && count == tiles {
            return match walked {
                Some((bytes, levels, at)) => quant_walk_kernel(
                    bytes, levels, at, rows, cols, rp, cp, side, groups, nk, batches, mach, finite,
                    schedule,
                ),
                None => quant_kernel_scheduled(
                    src, rows, cols, rp, cp, side, groups, nk, batches, mach, finite, schedule,
                    &read, staged,
                ),
            };
        }
        let key = name(
            "quant_share",
            vec![Py::str(&identity), Py::Int(first), Py::Int(count)],
        );
        build(&key, mach, ports.clone(), given.clone(), |t, b| {
            let pieces = rectangles(first, count, window_rp, cp, groups, nk);
            let record = ktpu_machine::packing::quant_status_bytes(mach.quant_cores()) / 2;
            let status = if vector && !finite && pieces.len() > 1 {
                Some(t.buffer(&[pieces.len() as i64 * record], g::Dtype::F16, vec![])?)
            } else {
                None
            };
            let (r0, c0) = (src.offset / src.pitch, src.offset % src.pitch);
            for (index, [row, height, col, width]) in pieces.into_iter().enumerate() {
                let dst = b[1].at(&[sl(row, row + height), sl(col, col + width)])?;
                let local = row % window_rp;
                let from = r0 + row / window_rp * window_rows + local.min(window_rows);
                let valid_rows = (local + height).min(window_rows) - local.min(window_rows);
                let left = col.min(cols);
                let valid_cols = (col + width).min(cols) - left;
                if vector && (valid_rows == 0 || valid_cols == 0) {
                    let status_view = match &status {
                        Some(s) => {
                            let at = index as i64 * record;
                            Some(s.at(&[sl(at, at + record)])?)
                        }
                        None => None,
                    };
                    t.quant_zeros(&dst, &b[0].whole(), schedule_on, status_view.as_ref())?;
                    continue;
                }
                if let Some(steps) = mapped {
                    let ints =
                        |v: &[i64]| g::Attr::Tuple(v.iter().map(|i| g::Attr::Int(*i)).collect());
                    let attrs = [
                        ("read_map", prepared::Read::steps(steps)),
                        (
                            "read_shape",
                            ints(&[src.pitch, from, c0 + left, valid_rows, valid_cols]),
                        ),
                        ("read_batch", ints(&[1, height])),
                    ];
                    let status_view = match &status {
                        Some(s) => {
                            let at = index as i64 * record;
                            Some(s.at(&[sl(at, at + record)])?)
                        }
                        None => None,
                    };
                    t.quant_mapped_status(
                        &dst,
                        &b[0].whole(),
                        finite,
                        schedule,
                        &attrs,
                        status_view.as_ref(),
                    )?;
                    continue;
                }
                let staged = staged || (!vector && (rows != rp || cols != cp));
                let source = if let Some((_, levels, at)) = walked {
                    let shape = if staged {
                        [height, width]
                    } else {
                        [valid_rows.max(1), valid_cols.max(hw.lanes)]
                    };
                    let z = t.buffer(&shape, g::Dtype::F16, vec![])?;
                    if staged {
                        t.fill_value(&z.whole(), 0, None, None)?;
                    }
                    let pieces = super::destination_walk::rectangle(
                        levels,
                        at,
                        row / window_rp * window_rows + local.min(window_rows),
                        valid_rows,
                        cols / hw.lanes,
                        left / hw.lanes,
                        valid_cols / hw.lanes,
                        hw.word_bytes,
                    )?;
                    super::destination_walk::emit(
                        t,
                        &z,
                        &b[0],
                        &pieces,
                        valid_cols / hw.lanes,
                        shape[1] / hw.lanes,
                        hw.word_bytes,
                    )?;
                    if staged {
                        z.whole()
                    } else {
                        z.at(&[sl(0, valid_rows), sl(0, valid_cols)])?
                    }
                } else if staged {
                    let z = t.buffer(&[height, width], g::Dtype::F16, vec![])?;
                    t.fill_value(&z.whole(), 0, None, None)?;
                    if valid_rows > 0 && valid_cols > 0 {
                        t.copy(
                            &z.at(&[sl(0, valid_rows), sl(0, valid_cols)])?,
                            &b[0].at(&[
                                sl(from, from + valid_rows),
                                sl(c0 + left, c0 + left + valid_cols),
                            ])?,
                            None,
                            None,
                        )?;
                    }
                    z.whole()
                } else {
                    b[0].at(&[
                        sl(from, from + valid_rows),
                        sl(c0 + left, c0 + left + valid_cols),
                    ])?
                };
                match &status {
                    Some(s) => {
                        let at = index as i64 * record;
                        t.quant_deferred(
                            &dst,
                            &source,
                            &s.at(&[sl(at, at + record)])?,
                            schedule_on,
                            vector && !staged,
                        )?;
                    }
                    None => {
                        t.quant_scheduled(&dst, &source, finite, schedule_on, vector && !staged)?
                    }
                }
            }
            if let Some(s) = status {
                t.quant_check(&s.whole())?;
            }
            Ok(())
        })
    })
}
