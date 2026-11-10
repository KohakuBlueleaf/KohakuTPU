//! The checker's work so far: abstract-machine runs/events and retained-history
//! operations. The history census includes copies and search/tree visits, not
//! just simulated events; it never changes a proof budget or verdict.

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
    static HISTORY_VISITS: Cell<u64> = const { Cell::new(0) };
}

/// Physical histories visited by retirement on the calling checker thread.
pub fn retirement_visits() -> u64 {
    RETIREMENT_VISITS.with(Cell::get)
}

pub(crate) fn visit_retirement() {
    RETIREMENT_VISITS.with(|count| count.set(count.get() + 1));
}

/// Counter marks/handles copied or frozen, and persistent-tree query visits on
/// the calling thread. Snapshot deltas isolate a single proof from other tests.
pub fn history_visits() -> u64 {
    HISTORY_VISITS.with(Cell::get)
}

pub(crate) fn visit_history(n: usize) {
    HISTORY_VISITS.with(|count| count.set(count.get().saturating_add(n as u64)));
}
