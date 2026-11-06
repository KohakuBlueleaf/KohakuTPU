//! Physical destination rectangles for transpose schedules.

use super::*;
use crate::split::{
    partition_tiles, validate_ownership, CostEstimate, LegalityWitness, OutputLayout,
    PlannedKernel, Region, ShardRole, ShardTiles, SplitAxis, SplitOption, SplitRequest,
};

fn rectangles(first: i64, count: i64, a: i64, b: i64, width: i64) -> Vec<[i64; 5]> {
    let columns = cdiv(a, width);
    let rows = cdiv(b, 32);
    let mut at = first;
    let end = first + count;
    let mut output = Vec::new();
    while at < end {
        let batch = at / (rows * columns);
        let row = at / columns % rows * 32;
        let column = at % columns * width;
        if column == 0 && end - at >= columns {
            let bands = ((end - at) / columns).min(rows - row / 32);
            let height = (bands * 32).min(b - row);
            output.push([batch, row, height, 0, a]);
            at += bands * columns;
        } else {
            let tiles = (end - at).min(columns - at % columns);
            output.push([
                batch,
                row,
                32.min(b - row),
                column,
                (tiles * width).min(a - column),
            ]);
            at += tiles;
        }
    }
    output
}

/// Partition complete hand-tile/GT4 rectangles with line-disjoint destinations.
pub fn kernel(
    nb: i64,
    a: i64,
    b: i64,
    class: Option<&ShapeClass>,
    request: &SplitRequest,
    m: &Machine,
) -> R<PlannedKernel> {
    let signature = match class {
        Some(class) => classed(nb, a, b, class, m)?,
        None => transpose_kernel(nb, a, b, m)?,
    };
    let hw = Hw::of(m);
    let line = m.card.dram.line_bytes as i64;
    let width = if 2 * a % line == 0 {
        32.max(line / 2)
    } else {
        a
    };
    let tiles = nb * cdiv(b, 32) * cdiv(a, width);
    let mut output = OutputLayout {
        form: "producer".into(),
        valid_region: vec![Region {
            allocation: "y".into(),
            bytes: 0..(nb * a * b * 2) as u64,
        }],
        ..OutputLayout::default()
    };
    let mut shards = Vec::new();
    for (node, range) in partition_tiles(tiles, request.nodes.ids().len())?
        .into_iter()
        .enumerate()
    {
        let mut reads = Vec::new();
        let mut writes = Vec::new();
        for [batch, row, height, column, width] in
            rectangles(range.start, range.end - range.start, a, b, width)
        {
            for r in row..row + height {
                let start = ((batch * b + r) * a + column) * 2;
                writes.push(Region {
                    allocation: "y".into(),
                    bytes: start as u64..(start + width * 2) as u64,
                });
            }
            for r in column..column + width {
                let start = ((batch * a + r) * b + row) * 2;
                reads.push(Region {
                    allocation: "x".into(),
                    bytes: start as u64..(start + height * 2) as u64,
                });
            }
        }
        if !writes.is_empty() {
            reads.push(Region {
                allocation: "mask".into(),
                bytes: 0..(swaps(&hw)? * hw.vlmax * 2) as u64,
            });
        }
        output
            .ownership
            .extend(writes.iter().cloned().map(|region| (node, region)));
        shards.push(ShardTiles {
            logical_node: node,
            tile_ranges: vec![range.clone()],
            logical_origin: vec![range.start],
            valid_extent: vec![range.end - range.start],
            padded_extent: vec![range.end - range.start],
            read_regions: reads,
            write_regions: writes,
            scratch_regions: vec![],
            local_schedule: "destination-transpose".into(),
            role: if range.is_empty() {
                ShardRole::Empty
            } else {
                ShardRole::Compute
            },
        });
    }
    validate_ownership(&output)?;
    let option = SplitOption {
        id: "destination-tiles".into(),
        axis: SplitAxis::DestinationTiles,
        p: request.nodes.ids().len(),
        active_nodes: shards
            .iter()
            .filter(|share| share.role != ShardRole::Empty)
            .count(),
        shards,
        output,
        events: vec![],
        conversions: 0,
        cost: CostEstimate::default(),
        legality: LegalityWitness {
            numerics: request.numerics.clone(),
            clauses: vec!["complete hand-tile/GT4 rectangles; one destination-line owner".into()],
        },
    };
    let words = nb * a * b / hw.lanes;
    let xs = [words, hw.lanes];
    let ms = [swaps(&hw)?, hw.vlmax];
    let ports = vec![
        Port::input("x", &xs, g::Dtype::F16),
        Port::input("mask", &ms, g::Dtype::F16),
        Port::output("y", &xs, g::Dtype::F16),
    ];
    let given = signature.given.clone();
    let identity = signature.graph.name.clone();
    crate::gen::vector::split::kernel_options(
        vec![option],
        signature,
        request,
        m,
        |first, count| {
            if first == 0 && count == tiles {
                return match class {
                    Some(class) => classed(nb, a, b, class, m),
                    None => transpose_kernel(nb, a, b, m),
                };
            }
            let key = name(
                "transpose_share",
                vec![Py::str(&identity), Py::Int(first), Py::Int(count)],
            );
            build(&key, m, ports.clone(), given.clone(), |t, bufs| {
                for [batch, row, height, column, width] in rectangles(first, count, a, b, width) {
                    let x = ((batch * a + column) * b + row) / hw.lanes;
                    let y = ((batch * b + row) * a + column) / hw.lanes;
                    let source =
                        bufs[0].at(&[sl(x, x + ((width - 1) * b + height) / hw.lanes), ALL])?;
                    let destination =
                        bufs[2].at(&[sl(y, y + ((height - 1) * a + width) / hw.lanes), ALL])?;
                    emit_window(
                        t,
                        &source,
                        &bufs[1],
                        0,
                        &destination,
                        1,
                        width,
                        height,
                        class.map(|class| class.cores),
                        Some((2 * b, 2 * a)),
                        Some((
                            &bufs[0].whole(),
                            &bufs[2].whole(),
                            x * hw.word_bytes,
                            y * hw.word_bytes,
                        )),
                        &hw,
                    )?;
                }
                Ok(())
            })
        },
    )
}
