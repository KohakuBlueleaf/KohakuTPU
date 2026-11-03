//! Silicon flaw F-001's proof (`ktpu_l1::flaw`, L1.MOV.019) as the L2
//! lowering reads it to choose a vector copy or a staged gather first.

use super::*;

/// Whether one mover command writing `levels` from byte `off` of its buffer
/// makes no one-beat write burst at any page offset (`ktpu_l1::flaw::burst_safe`).
pub fn burst_safe(off: i64, levels: &[(i64, i64)], word: i64, line: i64, cap: i64) -> bool {
    if on("l2.mover/burst-run-only") {
        return count(levels) == 1 || run_words(levels, word) > 1;
    }
    ktpu_l1::flaw::burst_safe(off, levels, word, line, cap)
}
