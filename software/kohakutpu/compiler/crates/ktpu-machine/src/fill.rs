//! The completion queue's rule, for the lowering that keeps it and the checker.

/// Whether a fill of `words` that takes the completion slot of `shadowed`,
/// with `held` fills unlanded counting itself, loses an entry (ISA.VC.193).
pub fn fill_lost(depth: u32, held: u32, shadowed: i64, words: i64) -> bool {
    held > depth && (shadowed != words || held >= 2 * depth)
}
