//! The spec vectors' checker mutants (`software/tests/spec/runners/isa.py`,
//! `l1.py`, `@mutant`): with the `mutants` feature a thread-local switch
//! names the one active mutant and each mutated point asks [`on`]; without
//! it [`on`] is `false` and the points compile out.

#[cfg(feature = "mutants")]
use std::cell::Cell;

#[cfg(feature = "mutants")]
thread_local! {
    static ACTIVE: Cell<Option<&'static str>> = const { Cell::new(None) };
}

/// Whether mutant `name` is active on this thread.
#[inline(always)]
pub fn on(name: &str) -> bool {
    #[cfg(feature = "mutants")]
    {
        ACTIVE.with(|a| a.get() == Some(name))
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = name;
        false
    }
}

/// `f()` with mutant `name` active on this thread.
#[cfg(feature = "mutants")]
pub fn with<T>(name: &'static str, f: impl FnOnce() -> T) -> T {
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            ACTIVE.with(|a| a.set(None));
        }
    }
    ACTIVE.with(|a| a.set(Some(name)));
    let _reset = Reset;
    f()
}

/// Every mutant the checker carries, by its Python name.
pub const NAMES: &[&str] = &[
    "verify.conversion/ignore-policy",
    "verify.template/mover-no-summary",
    "verify.template/mover-no-guards",
    "verify.template/mover-no-projection",
    "verify.template/no-residues",
    "verify.template/repeat-data",
    "verify.lines/off",
    "verify.cluster/off",
    "verify.vector/off",
    "verify.vector/no-fill-track",
    "verify.lines/no-dynamic-full-lines",
    "verify.lines/recv-is-payload",
    "verify.lines/word-runs",
    "verify.lint/one-bank",
    "rul.003/queues-folded",
    "rul.051/zero-skips",
    "rul.052/dst-is-peer",
    "isa.vector/model_queues",
    "isa.vector/model_walk",
    "isa.vector/model_slack",
    "isa.vector/model_ainc",
    "isa.vector/model_order",
    "isa.vector/model_peer",
    "isa.vector/model_strides",
    "isa.vector/model_no_sync",
    "isa.cluster/checker",
    "l1.verify/reverse-segment",
    "verify.check/totals-prepass",
];
