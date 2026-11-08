//! The checker's work so far in this process: abstract-machine runs and the
//! events they took.

use std::cell::Cell;
use std::sync::atomic::{AtomicU64, Ordering};

pub(crate) static RUNS: AtomicU64 = AtomicU64::new(0);
pub(crate) static EVENTS: AtomicU64 = AtomicU64::new(0);

/// `(runs, events)` so far.
pub fn counts() -> (u64, u64) {
    (RUNS.load(Ordering::Relaxed), EVENTS.load(Ordering::Relaxed))
}

thread_local! {
    static RETIREMENT_VISITS: Cell<u64> = const { Cell::new(0) };
}

/// Physical histories visited by retirement on the calling checker thread.
pub fn retirement_visits() -> u64 {
    RETIREMENT_VISITS.with(Cell::get)
}

pub(crate) fn visit_retirement() {
    RETIREMENT_VISITS.with(|count| count.set(count.get() + 1));
}
