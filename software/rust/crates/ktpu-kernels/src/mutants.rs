//! Spec mutants (`software/tests/spec/runners/api.py`): a thread-local
//! switch read where the mutated decision is made. Without the `mutants`
//! feature every switch is off and compiled out.

/// The generator mutants, by their spec names.
pub const NAMES: &[&str] = &[
    "memory/flat-overrun",
    "memory/reverse-overrun",
    "api.vector/split_one",
    "api.vector/bands_one",
    "api.vector/stages_whole",
    "api.vector/l1_small",
    "api.vector/port_pad",
    "api.vector/stat_rows",
    "api.rows/max_block",
    "l2.lanes/rot-negated",
    "l2.lanes/keep-ignored",
    "l2.lanes/masks-inverted",
    "dsl.ops/gt4_off",
    "dsl.ops/lane-lost",
    "isa.cluster/plan",
    "num.stat/32-a-word",
    "num.expr/round-constant-2-15",
    "dsl.kernel/name",
];

/// Mutants no spec vector names, witnessed by this crate's own tests: the
/// unit facts read as the constants they replaced; a statistic packed in
/// the [R, 32] form RUL.030 refuses; a reduction read twice taken twice;
/// the contraction knobs unrecorded.
pub const LOCAL: &[&str] = &[
    crate::gen::matmul::UNRECORDED_MUTANT,
    "kernels.quant/no-schedule",
    "kernels.hw/unit_constants",
    "kernels.vector/stat_rows_form",
    "kernels.expr/reduces_dup",
    crate::expr::intern::UNORDERED_MUTANT,
];

#[cfg(feature = "mutants")]
thread_local! {
    static ACTIVE: std::cell::RefCell<Vec<&'static str>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// Whether mutant `name` is on in this thread.
#[inline]
pub fn on(name: &str) -> bool {
    #[cfg(feature = "mutants")]
    {
        ACTIVE.with(|a| a.borrow().contains(&name))
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = name;
        false
    }
}

/// `f` with mutant `name` on.
#[cfg(feature = "mutants")]
pub fn with<T>(name: &'static str, f: impl FnOnce() -> T) -> T {
    assert!(
        NAMES.contains(&name) || LOCAL.contains(&name),
        "no mutant {name}"
    );
    ACTIVE.with(|a| a.borrow_mut().push(name));
    let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(f));
    ACTIVE.with(|a| a.borrow_mut().retain(|n| *n != name));
    match got {
        Ok(v) => v,
        Err(p) => std::panic::resume_unwind(p),
    }
}
