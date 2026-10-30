//! The vector lowering's work so far in this process, counted where it is
//! done: band images lowered, images scheduled and encoded for their IMEM
//! fit, and distinct candidate texts timed.

use std::sync::atomic::{AtomicU64, Ordering};

pub(crate) static LOWERED: AtomicU64 = AtomicU64::new(0);
pub(crate) static FITTED: AtomicU64 = AtomicU64::new(0);
pub(crate) static TIMED: AtomicU64 = AtomicU64::new(0);

/// `(lowered, fitted, timed)` so far.
pub fn counts() -> (u64, u64, u64) {
    (
        LOWERED.load(Ordering::Relaxed),
        FITTED.load(Ordering::Relaxed),
        TIMED.load(Ordering::Relaxed),
    )
}
