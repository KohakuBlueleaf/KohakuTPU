//! Logical attention ownership, independent of physical motif scheduling.
//!
//! Logical assignments are independent of a motif's packing. The reference
//! flash adapter supplies executable split options and physical ownership.

pub mod flash;
pub mod flash_split;
pub mod motif;

use std::ops::Range;

use crate::split::{partition_tiles, NodeSet};
use crate::trace::{fail, R};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AttentionMask {
    None,
    /// A key is visible when key <= global query + offset.
    Causal {
        offset: i64,
    },
    Explicit,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AttentionPrecision {
    Mx7,
    PreciseScores,
}

#[derive(Clone, Debug)]
pub struct AttentionSpec {
    /// Batch and heads flattened in batch-major order.
    pub heads: i64,
    pub queries: i64,
    pub keys: i64,
    pub dk: i64,
    pub dv: i64,
    pub mask: AttentionMask,
    pub precision: AttentionPrecision,
}

impl AttentionSpec {
    pub fn validate(&self) -> R<()> {
        if self.heads < 0 || self.queries < 0 || self.keys < 0 || self.dk <= 0 || self.dv <= 0 {
            return fail("DSL.KERN.020", "invalid logical attention extents");
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AttentionPartition {
    Heads,
    QueryRows,
    HeadsQuery {
        ph: usize,
        pq: usize,
    },
    /// Contiguous ranges in lexicographic (head, query tile) order.
    Balanced,
}

/// Global coordinates; every span reads the complete key domain of its head.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AttentionSpan {
    pub head: i64,
    pub query_tiles: Range<i64>,
    pub queries: Range<i64>,
    pub keys: Range<i64>,
}

#[derive(Clone, Debug)]
pub struct AttentionAssignment {
    pub partition: AttentionPartition,
    /// Indexed by logical node rank, including empty participants.
    pub shares: Vec<Vec<AttentionSpan>>,
}

fn span(spec: &AttentionSpec, head: i64, tiles: Range<i64>, tq: i64) -> AttentionSpan {
    // The last tile can extend beyond the logical query domain. Saturation
    // avoids forming that padded endpoint outside the index representation.
    let end = tiles.end.saturating_mul(tq).min(spec.queries);
    AttentionSpan {
        head,
        queries: tiles.start * tq..end,
        query_tiles: tiles,
        keys: 0..spec.keys,
    }
}

/// Enumerate logical ownership without claiming physical-write legality.
/// `tq` is the adapter's complete query ownership unit, not its PV microtile.
/// Tails describe valid queries only; padding remains an adapter obligation.
pub fn assignments(spec: &AttentionSpec, tq: i64, nodes: &NodeSet) -> R<Vec<AttentionAssignment>> {
    spec.validate()?;
    if tq <= 0 {
        return fail("DSL.KERN.020", "attention ownership tile must be positive");
    }
    let tiles = spec.queries / tq + i64::from(spec.queries % tq != 0);
    let Some(jobs) = spec.heads.checked_mul(tiles) else {
        return fail("DSL.KERN.020", "attention tile count exceeds index range");
    };
    let p = nodes.ids().len();
    let mut result = Vec::new();
    let mut rectangular = |partition, ph, pq| -> R<()> {
        let heads = partition_tiles(spec.heads, ph)?;
        let queries = partition_tiles(tiles, pq)?;
        let mut shares = Vec::with_capacity(p);
        for hr in &heads {
            for qr in &queries {
                let mut share = Vec::new();
                if !qr.is_empty() {
                    for head in hr.clone() {
                        share.push(span(spec, head, qr.clone(), tq));
                    }
                }
                shares.push(share);
            }
        }
        result.push(AttentionAssignment { partition, shares });
        Ok(())
    };
    rectangular(AttentionPartition::Heads, p, 1)?;
    rectangular(AttentionPartition::QueryRows, 1, p)?;
    for ph in 1..=p {
        if p % ph == 0 {
            let pq = p / ph;
            rectangular(AttentionPartition::HeadsQuery { ph, pq }, ph, pq)?;
        }
    }
    let mut shares = Vec::with_capacity(p);
    for range in partition_tiles(jobs, p)? {
        let mut share = Vec::new();
        let mut cursor = range.start;
        while cursor < range.end {
            let head = cursor / tiles;
            let first = cursor % tiles;
            let count = (tiles - first).min(range.end - cursor);
            share.push(span(spec, head, first..first + count, tq));
            cursor += count;
        }
        shares.push(share);
    }
    result.push(AttentionAssignment {
        partition: AttentionPartition::Balanced,
        shares,
    });
    Ok(result)
}
