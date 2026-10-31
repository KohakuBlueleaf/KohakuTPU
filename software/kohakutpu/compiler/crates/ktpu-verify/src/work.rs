//! The checker's work so far in this process: abstract-machine runs and the
//! events they took.

use std::sync::atomic::{AtomicU64, Ordering};

pub(crate) static RUNS: AtomicU64 = AtomicU64::new(0);
pub(crate) static EVENTS: AtomicU64 = AtomicU64::new(0);

/// `(runs, events)` so far.
pub fn counts() -> (u64, u64) {
    (RUNS.load(Ordering::Relaxed), EVENTS.load(Ordering::Relaxed))
}
