//! Spec-vector mutants (`software/tests/spec/runners/l0.py`, `@mutant`): with
//! the `mutants` feature a thread-local switch names the one active mutant
//! and each mutated point asks [`on`]; without it [`on`] is `false` and the
//! points compile out.

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

/// Every mutant ktpu-l0 carries, by its Python name.
pub const NAMES: &[&str] = &[
    "l0.program/static-loop-wait",
    "l0.program/unrelocated-literal",
    "l0.format/unbounded_drift",
    "dbg/no_cut",
    "l0.mmu/identity",
    "l0.program/repeat-count",
    "l0.program/advance-replace",
    "l0.bind/repeat-wrap",
    "l0.template/step",
    "l0.template/late-repeat-check",
    "l0.template/step_extent",
    "l0.template/register_target",
    "l0.template/cut_sites",
    "l0.template/affine",
    "l0.template/split",
    "l0.template/width",
    "l0.template/class",
    "l0.template/environment",
    "l0.template/misadmit",
    "l0.template/extent",
    "l0.template/identity",
    "l0.pkg/magic",
    "l0.pkg/version",
    "l0.pkg/header_bytes",
    "l0.pkg/regs",
    "l0.pkg/align16",
    "l0.pkg/rx_flag",
    "l0.pkg/rx_always",
    "l0.pkg/count_fields",
    "l0.pkg/checksum_flag",
    "l0.pkg/check_word",
    "l0.pkg/unit_word",
    "l0.pkg/type_code",
    "l0.pkg/payload_order",
    "l0.pkg/step_fields",
    "l0.pkg/table_order",
    "l0.pkg/reader_in_order",
    "l0.pkg/read_step_fields",
    "l0.bind/unchecked",
    "l0.bind/zero_unbound",
    "l0.pkg/unlimited_units",
    "l0.pkg/unlimited_buffers",
    "l0.build/reloc_past_firmware",
    "l0.program/no_signature",
    "l0.bind/default_wins",
    "l0.bind/zero_binds",
    "l0.bind/no_shift",
    "l0.bind/clobber",
    "l0.bind/addr_off",
    "l0.bind/keeps_relocs",
    "l0.build/drop-address64",
    "l0.build/default_credit",
    "l0.build/unit_per_use",
    "l0.build/port_swapped",
    "l0.build/bindings_reversed",
    "l0.build/double_barrier",
    "l0.build/await_plus_one",
    "l0.build/never_posted",
    "l0.build/flags_dropped",
    "l0.build/count_arg_swapped",
    "l0.build/pairs_unpadded",
    "l0.build/patch_arg",
    "l0.build/patch_field_off",
    "l0.build/signature_default",
    "l0.build/mover_swapped",
    "l0.program/inst_depth",
    "l0.program/tmp_reg",
    "l0.lower/wait_code",
    "l0.lower/posted_flag",
    "l0.lower/no_rel",
    "l0.lower/fetch_depth",
    "l0.lower/ctr_mask",
    "l0.lower/src_mover",
    "l0.lower/untyped_dst",
    "l0.lower/no_dedupe",
    "l0.lower/dedupe_across_steps",
    "l0.lower/wait_always",
    "l0.lower/mover_code",
    "l0.lower/ring_code",
    "l0.lower/ring_value",
    "l0.lower/pad_code",
    "l0.lower/no_barrier",
    "l0.lower/gos_ignored",
    "l0.lower/await_absolute",
    "l0.lower/run_cap",
    "l0.lower/jump_lowered",
    "l0.lower/steps_dropped",
    "l0.lower/meta_lost",
    "l0.check/off",
    "l0.lower/rings_in_engine",
    "l0.boot/magic",
    "l0.boot/defaults",
    "l0.boot/args_after_boot",
    "l0.boot/max_units",
    "l0.boot/region_order",
    "l0.boot/raw_default",
];
