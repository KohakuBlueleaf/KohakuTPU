//! The machine-file check's mutant: with the `mutants` feature a
//! thread-local switch names the one active mutant and the check asks
//! [`on`]; without it [`on`] is `false` and the point compiles out.

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

/// Every mutant ktpu-machine carries.
pub const NAMES: &[&str] = &[
    "machine.check/off",
    "machine.card/stage-banks",
    "machine.card/mesh-bits",
    "machine.card/mesh-shift",
    "rul.001/registers",
    "rul.001/unbuilt",
    "rul.001/missing-core",
    "machine.pairing/mover-paired",
];
