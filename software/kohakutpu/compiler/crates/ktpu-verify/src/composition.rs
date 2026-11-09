//! Causal decomposition for retained compositions. Ordinary checks deliberately
//! keep their original (including resource-limit) verdicts. This scope changes
//! only the organization of the physical-memory proof, never execution clocks,
//! visibility, symbolic state, dispatch ordering, or the refusing policy.
//!
//! Run the synchronous scheduling/checking operation inside `prove`. The scope
//! is thread-local and unwind-safe; it is not an option for `check_many`'s worker
//! threads. No certificate, program name, or join annotation is trusted here.

use std::cell::Cell;

thread_local! {
    static ACTIVE: Cell<bool> = const { Cell::new(false) };
    static PEAK: Cell<usize> = const { Cell::new(0) };
}

#[doc(hidden)]
pub fn active() -> bool {
    ACTIVE.with(Cell::get)
}

pub(crate) fn charge(work: usize) {
    PEAK.with(|peak| peak.set(peak.get().max(work)));
}

/// Check a composition using the unchanged DRAM budget per causally connected
/// obligation. Returns the result and peak obligation work (not total work).
/// A real join separates obligations; a missing join cannot manufacture an edge.
/// All accesses still go through the byte/line proof, including package fetches.
pub fn prove<T>(f: impl FnOnce() -> T) -> (T, usize) {
    struct Restore(bool, usize);
    impl Drop for Restore {
        fn drop(&mut self) {
            ACTIVE.with(|active| active.set(self.0));
            PEAK.with(|peak| peak.set(self.1));
        }
    }
    let restore = Restore(
        ACTIVE.with(|active| active.replace(true)),
        PEAK.with(|peak| peak.replace(0)),
    );
    let result = f();
    let work = PEAK.with(Cell::get);
    drop(restore);
    (result, work)
}
