//! Physical inputs to a whole-card proof, fixed at preparation, never per step.
//! Package allocations owned by the runtime are private before placement; an
//! explicitly placed package uses its actual section addresses, so aliases with
//! data and other packages participate in the same physical proof.

use std::collections::BTreeMap;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackageStorage {
    pub steps: u64,
    pub payloads: u64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MemoryContext {
    /// (sysnode, run ordinal) -> final physical section addresses. Omission
    /// denotes a fresh allocator-owned package span, disjoint from data spans.
    pub packages: BTreeMap<(u32, usize), PackageStorage>,
    /// Initialized bytes, before the first package is submitted. Subsequent
    /// writes replace or invalidate these values in the simulated memory.
    pub initial: BTreeMap<u64, Vec<u8>>,
}
