//! Complete row domains over an ordered sysnode grant.

use super::*;
use crate::split::{
    choose_split, partition_tiles, validate_ownership, CostEstimate, CostFeatures, LegalityWitness,
    LocalTiming, OutputLayout, PlannedKernel, Region, ShardRole, ShardTiles, SplitAxis,
    SplitOption, SplitRequest,
};

/// A named operand in the invocation's global coordinate system.
pub struct Tensor {
    pub name: String,
    pub operand: Operand,
    pub width: i64,
    /// Independent planes of block partials, in elements.
    pub planes: Vec<i64>,
}

pub struct Shape {
    pub rows: i64,
    pub quantum: i64,
    pub reads: Vec<Tensor>,
    pub writes: Vec<Tensor>,
    pub tokens: bool,
}

/// Shift by logical rows, preserving scalar and column broadcasts.
pub fn shifted(o: &Operand, row: i64) -> Operand {
    Operand {
        offset: o.offset + row * o.logical_strides().0,
        ..*o
    }
}

fn regions(tensor: &Tensor, first: i64, rows: i64, hw: &Hw) -> Vec<Region> {
    if rows == 0 {
        return vec![];
    }
    let o = &tensor.operand;
    let item = Hw::item(o.dtype);
    let (rs, cs) = o.logical_strides();
    let mut result = Vec::new();
    for &plane in &tensor.planes {
        if o.kind == Kind::Row {
            let lo = plane + o.offset + first;
            let hi = lo + stat_words(rows, hw) * hw.stat_rows();
            result.push(Region {
                allocation: tensor.name.clone(),
                bytes: (lo * item) as u64..(hi * item) as u64,
            });
        } else if o.kind == Kind::Scalar {
            let lo = plane + o.offset.div_euclid(hw.stat_rows()) * hw.stat_rows();
            result.push(Region {
                allocation: tensor.name.clone(),
                bytes: (lo * item) as u64..((lo + hw.stat_rows()) * item) as u64,
            });
        } else if rs == tensor.width && cs == 1 {
            let lo = plane + o.offset + first * rs;
            result.push(Region {
                allocation: tensor.name.clone(),
                bytes: (lo * item) as u64..((lo + rows * rs) * item) as u64,
            });
        } else {
            for row in 0..if rs == 0 { 1 } else { rows } {
                let lo = plane + o.offset + (first + row) * rs;
                let hi = lo + if cs == 0 { 1 } else { tensor.width };
                result.push(Region {
                    allocation: tensor.name.clone(),
                    bytes: (lo * item) as u64..(hi * item) as u64,
                });
            }
        }
    }
    result
}

/// A cut separates physical write lines as well as complete local quantums.
fn legal_cut(shape: &Shape, row: i64, line: i64) -> bool {
    shape.writes.iter().all(|tensor| {
        let o = &tensor.operand;
        let item = Hw::item(o.dtype);
        tensor.planes.iter().all(|&plane| {
            let (left, right) = if o.kind == Kind::Row {
                let boundary = (plane + o.offset + row) * item;
                (boundary, boundary)
            } else {
                let right = plane + o.offset + row * o.pitch;
                ((right - o.pitch + tensor.width) * item, right * item)
            };
            (left - 1).div_euclid(line) < right.div_euclid(line)
        })
    })
}

/// Coalesce adjacent quantums until every output has a disjoint line owner.
pub fn split_options(shape: &Shape, request: &SplitRequest, m: &Machine) -> R<Vec<SplitOption>> {
    let hw = Hw::of(m);
    if shape.rows < 0 || shape.quantum <= 0 {
        return fail(
            "API.ROW.002",
            "a row domain needs nonnegative rows and a positive quantum",
        );
    }
    // Line alignment repeats after at most line_bytes local quantums.
    let period = m.card.dram.line_bytes as i64;
    let residues: Vec<_> = (1..=period)
        .filter(|step| legal_cut(shape, step * shape.quantum, period))
        .collect();
    let steps = (shape.rows - 1).max(0) / shape.quantum;
    let internal = steps / period * residues.len() as i64
        + residues
            .iter()
            .filter(|&&step| step <= steps % period)
            .count() as i64;
    let tiles = if shape.rows == 0 { 0 } else { internal + 1 };
    let cut = |tile: i64| {
        if tile == 0 {
            0
        } else if tile == tiles {
            shape.rows
        } else {
            let index = tile - 1;
            (index / residues.len() as i64 * period + residues[index as usize % residues.len()])
                * shape.quantum
        }
    };
    let p = request.nodes.ids().len();
    let mut output = OutputLayout {
        form: "producer".into(),
        ..OutputLayout::default()
    };
    for tensor in &shape.writes {
        output
            .valid_region
            .extend(regions(tensor, 0, shape.rows, &hw));
    }
    let mut shards = Vec::new();
    for (node, tiles) in partition_tiles(tiles, p)?.into_iter().enumerate() {
        let first = cut(tiles.start);
        let end = cut(tiles.end);
        let mut reads = Vec::new();
        let mut writes = Vec::new();
        for tensor in &shape.reads {
            reads.extend(regions(tensor, first, end - first, &hw));
        }
        for tensor in &shape.writes {
            writes.extend(regions(tensor, first, end - first, &hw));
        }
        output
            .ownership
            .extend(writes.iter().cloned().map(|r| (node, r)));
        shards.push(ShardTiles {
            logical_node: node,
            tile_ranges: vec![tiles],
            logical_origin: vec![first],
            valid_extent: vec![end - first],
            padded_extent: vec![cdiv(end - first, shape.quantum) * shape.quantum],
            read_regions: reads,
            write_regions: writes,
            scratch_regions: vec![],
            local_schedule: "complete-rows".into(),
            role: if first == end {
                ShardRole::Empty
            } else {
                ShardRole::Compute
            },
        });
    }
    validate_ownership(&output)?;
    Ok(vec![SplitOption {
        id: if shape.tokens { "tokens" } else { "rows" }.into(),
        axis: if shape.tokens {
            SplitAxis::Tokens
        } else {
            SplitAxis::Rows
        },
        p,
        active_nodes: shards.iter().filter(|s| s.role != ShardRole::Empty).count(),
        shards,
        output,
        events: vec![],
        conversions: 0,
        cost: CostEstimate::default(),
        legality: LegalityWitness {
            numerics: request.numerics.clone(),
            clauses: vec![
                "complete reduction widths; global operand strides; disjoint destination lines"
                    .into(),
            ],
        },
    }])
}

/// Trace the existing local generator at each global origin, then price its
/// complete schedule, including prologues, scratch copies and exact tails.
pub fn kernel(
    shape: &Shape,
    signature: Kernel,
    request: &SplitRequest,
    m: &Machine,
    mut local: impl FnMut(i64, i64) -> R<Kernel>,
) -> R<PlannedKernel> {
    let mut candidates = Vec::new();
    let mut emitted = Vec::new();
    for mut option in split_options(shape, request, m)? {
        let mut kernels = Vec::new();
        for share in &mut option.shards {
            if share.role == ShardRole::Empty {
                continue;
            }
            for region in &mut share.read_regions {
                if let Some((_, spec)) = signature
                    .given
                    .iter()
                    .find(|(n, _)| *n == region.allocation)
                {
                    let end = (spec.shape.iter().product::<i64>() * Hw::item(spec.dtype)) as u64;
                    region.bytes.end = region.bytes.end.min(end);
                    region.bytes.start = region.bytes.start.min(region.bytes.end);
                }
            }
            let k = crate::kernel::on_node(request.nodes.ids()[share.logical_node], || {
                local(share.logical_origin[0], share.valid_extent[0])
            })?;
            share.local_schedule = k.graph.name.clone();
            share.scratch_regions = k
                .graph
                .scratch
                .iter()
                .map(|scratch| Region {
                    allocation: format!("share{}_{}", kernels.len(), scratch.name),
                    bytes: 0..(scratch.spec.shape.iter().product::<i64>()
                        * Hw::item(scratch.spec.dtype)) as u64,
                })
                .collect();
            kernels.push(k);
        }
        option.output.tensors = signature
            .graph
            .ports
            .iter()
            .filter(|p| p.role == g::Role::Out)
            .map(|p| (p.name.clone(), p.spec.clone()))
            .collect();
        let features = CostFeatures::of_shares(&kernels, m)?;
        let mut kernel = crate::gen::chain::shares(&signature, kernels)?;
        kernel.graph.name = name(
            "row_shares",
            vec![
                Py::str(&signature.graph.name),
                Py::ints(request.nodes.ids()),
                Py::str(&option.id),
            ],
        );
        emitted.push((option.id.clone(), kernel));
        candidates.push((option, features));
    }
    let split = choose_split(candidates, &LocalTiming, request.force.as_deref())?;
    let Some((_, kernel)) = emitted.into_iter().find(|(id, _)| *id == split.id) else {
        return fail("DSL.KERN.020", "selected row schedule was not emitted");
    };
    Ok(PlannedKernel {
        kernel,
        l1: None,
        split,
        request: request.clone(),
    })
}
