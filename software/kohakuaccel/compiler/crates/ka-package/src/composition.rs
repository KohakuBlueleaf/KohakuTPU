//! Build a composition with frontier decomposition at proven quiescent cuts.
//! The scope requests decomposition; it does NOT certify any boundary. The
//! frontier pass derives cuts from actual awaits, barriers, branches and marks.
//! Ordinary Program::build outside this scope keeps whole-program semantics.
use std::cell::Cell;
thread_local! { static ACTIVE: Cell<bool> = const { Cell::new(false) }; }
/// Cache identity for callers that retain lowered packages.
#[doc(hidden)]
pub fn active() -> bool {
    ACTIVE.with(Cell::get)
}

/// Synchronous, thread-local and unwind-safe, like the retained verifier scope.
/// No environment variable, program-name test, certificate or join annotation.
pub fn build<T>(f: impl FnOnce() -> T) -> T {
    struct Restore(bool);
    impl Drop for Restore {
        fn drop(&mut self) {
            ACTIVE.with(|v| v.set(self.0));
        }
    }
    let restore = Restore(ACTIVE.with(|v| v.replace(true)));
    let result = f();
    drop(restore);
    result
}

impl crate::Program {
    /// A retained composition. Its real drain/control/token structure determines
    /// whether it can be split; unsafe boundaries keep the original analysis.
    pub fn build_composed(
        &self,
        facts: &crate::Facts,
        opts: crate::BuildOpts<'_>,
    ) -> crate::Result<crate::PackageBuilder> {
        build(|| self.build(facts, opts))
    }
}
