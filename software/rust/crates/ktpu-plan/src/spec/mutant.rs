//! Spec-vector mutants (`software/tests/spec/runners/api.py`, `@mutant`):
//! with the `mutants` feature a thread-local switch names the one active
//! mutant and each mutated point asks [`on`]; without it [`on`] is `false`
//! and the points compile out.

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

/// Every mutant ktpu-plan carries, by its Python name.
pub const NAMES: &[&str] = &[
    "memory/unsafe-bounds",
    "tg.rows/based_column",
    "tg.lower/envelope",
    "tg.lower/refused_other",
    "tg.lower/order",
    "tg.lower/no_unfold",
    "tg.lower/no_matmul",
    "tg.lower/alu_renamed",
    "tg.lower/reduce_all_max",
    "tg.lower/offset",
    "tg.lower/cast_scaled",
    "tg.lower/no_tables",
    "tg.run/no_flat",
    "tg.run/no_bare",
    "tg.run/no_move",
    "tg.run/no_transpose",
    "tg.run/no_flip",
    "tg.run/no_gather",
    "tg.run/no_boxes",
    "tg.run/no_window",
    "tg.run/no_reform",
    "tg.run/laid_free",
    "tg.run/takes_never",
    "tg.run/narrow_more",
    "tg.run/no_split",
    "tg.run/wide_exact",
    "tg.run/out_flat",
    "tg.run/const_no_host",
    "tg.run/copy_as_vector",
    "tg.run/no_padded_out",
    "api.rows/max_block",
    "api.forms/made_drop",
    "num.reduce/max-identity-zero",
    "num.api/int-tensor-accepted",
];
