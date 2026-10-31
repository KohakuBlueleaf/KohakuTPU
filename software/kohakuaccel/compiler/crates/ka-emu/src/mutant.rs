//! The emulator's own mutants: with the `mutants` feature, `KA_EMU_MUTANT` names the
//! one a whole run carries, which `tests/run.py --emu-mutants` must see fail.

/// Every mutant a `mutants` build carries.
pub const NAMES: [&str; 9] = [
    "emu.vm/no-dirty",
    "emu.vm/no-user",
    "emu.vm/page-offset-only",
    "emu.trap/no-delegation",
    "emu.ret/drops-mie",
    "emu.atomic/sc-always",
    "emu.atomic/amo-add-only",
    "emu.timer/never",
    "emu.wfi/runs-on",
];

/// Whether this run carries mutant `name`.
#[cfg(feature = "mutants")]
pub(crate) fn on(name: &str) -> bool {
    static ACTIVE: std::sync::OnceLock<Option<String>> = std::sync::OnceLock::new();
    ACTIVE
        .get_or_init(|| std::env::var("KA_EMU_MUTANT").ok())
        .as_deref()
        == Some(name)
}

/// Whether this run carries mutant `name`.
#[cfg(not(feature = "mutants"))]
#[inline(always)]
pub(crate) fn on(_: &str) -> bool {
    false
}
