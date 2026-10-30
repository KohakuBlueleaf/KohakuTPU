//! The index printers' mutant: with the `mutants` feature a thread-local
//! switch names the one active mutant and the printers ask [`on`]; without
//! it [`on`] is `false` and the point compiles out.

#[cfg(feature = "mutants")]
use std::cell::Cell;

#[cfg(feature = "mutants")]
thread_local! {
    static ACTIVE: Cell<Option<&'static str>> = const { Cell::new(None) };
}

/// A sum that starts with an atom is left unbracketed inside another atom
/// (`text::index`, `term::index`; Python `dsl.mutant.ATOM_SUM_SHORTCUT`).
pub const ATOM_SUM_SHORTCUT: &str = "graph.text/atom-sum-shortcut";

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

/// Every mutant ktpu-graph carries.
pub const NAMES: &[&str] = &[ATOM_SUM_SHORTCUT];
