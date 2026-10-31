//! The mover's and the field tables' mutants: with the `mutants` feature a
//! thread-local switch names the one active mutant and each mutated point
//! asks [`on`]; without it [`on`] is `false` and the points compile out.

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

/// Every mutant ka-isa carries.
pub const NAMES: &[&str] = &[
    "rul.049/reserved-copy",
    "rul.050/unaligned",
    "num.mover/quant-id",
    "num.mover/gt4-id",
    "num.mover/quant-b-dropped",
    "isa.mover/regs",
    "isa.mover/ctrl",
    "isa.mover/hdr",
    "isa.mover/fields",
    "isa.mover/modes",
    "isa.mover/limits",
    "isa.mover/xform",
    "isa.layout/fit_wraps",
];

/// The active mover layout mutant applied to the tables `MoverIsa::new` built.
#[cfg(feature = "mutants")]
pub(crate) fn mover(m: &mut crate::MoverIsa) {
    if on("isa.mover/limits") {
        m.levels = 7;
        m.move_words = 1 << 13;
        m.max_index = 4096;
        m.word_bytes = 16;
        m.addr_bits = 41;
    } else if on("isa.mover/modes") {
        m.ewidths = [1, 0, 3];
        let m = &mut m.modes;
        m.copy = 7;
        m.fill = 3;
        m.generate = 4;
        m.gather = 6;
        m.scatter = 2;
        m.transform = 1;
    }
}

#[cfg(not(feature = "mutants"))]
#[inline(always)]
pub(crate) fn mover(_: &mut crate::MoverIsa) {}
