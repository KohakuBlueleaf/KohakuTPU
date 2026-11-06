//! Explicit single-owner options for schedules with shared destination state.

use crate::gen::Hw;
use crate::kernel::Kernel;
use crate::split::{validate_ownership, Region, ShardRole, SplitAxis, SplitOption};
use crate::trace::R;
use ktpu_graph as g;

/// Preserve the complete local schedule and certify its allocation ownership.
/// Indexed write locations are supplied by table ports at binding time, so the
/// single owner reserves the entire destination allocation, including kept data.
pub fn certify(option: &mut SplitOption, kernel: &Kernel, reason: &str) -> R<()> {
    option.axis = SplitAxis::DestinationTiles;
    option.legality.clauses = vec![
        "P=1: complete local schedule; all destination state has one owner".into(),
        reason.into(),
    ];
    let mut reads = Vec::new();
    let mut writes = Vec::new();
    for port in &kernel.graph.ports {
        let bytes = port.spec.shape.iter().product::<i64>() * Hw::item(port.spec.dtype);
        if bytes == 0 {
            continue;
        }
        let region = Region {
            allocation: port.name.clone(),
            bytes: 0..bytes as u64,
        };
        match port.role {
            g::Role::In => reads.push(region),
            g::Role::Out => {
                // Kept and layered destinations may also be read locally.
                reads.push(region.clone());
                writes.push(region);
            }
        }
    }
    option.output.valid_region = writes.clone();
    option.output.ownership = writes.iter().cloned().map(|region| (0, region)).collect();
    for shard in &mut option.shards {
        if shard.role == ShardRole::Empty {
            continue;
        }
        shard.tile_ranges = vec![0..1];
        shard.logical_origin = vec![0];
        shard.valid_extent = vec![1];
        shard.padded_extent = vec![1];
        shard.read_regions = reads.clone();
        shard.write_regions = writes.clone();
        shard.local_schedule = format!("p1:{}", kernel.graph.name);
        shard.scratch_regions = kernel
            .graph
            .scratch
            .iter()
            .map(|scratch| Region {
                allocation: scratch.name.clone(),
                bytes: 0..(scratch.spec.shape.iter().product::<i64>()
                    * Hw::item(scratch.spec.dtype)) as u64,
            })
            .collect();
    }
    validate_ownership(&option.output)
}
