//! Spec-vector mutants (`software/tests/spec/runners/l2.py`, `@mutant`):
//! with the `mutants` feature a thread-local switch names the active mutant
//! and each mutated point asks [`on`]; without it [`on`] is `false`. The
//! `l2.banks` runner's switch (bank placement off) is always there. Work a
//! search spreads over threads carries both along ([`current`], [`scoped`]).

use std::cell::Cell;

/// The switches of one thread.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct State {
    pub active: Option<&'static str>,
    pub banks_off: bool,
    /// A [`L1_TIMING`] mutant: ktpu-l1's knob, switched with [`scoped`].
    pub l1: Option<&'static str>,
}

thread_local! {
    static STATE: Cell<State> =
        const { Cell::new(State { active: None, banks_off: false, l1: None }) };
}

/// L2 mutants whose Python patch breaks the core's timing model
/// (`timing.py`), which ktpu-l1's `sched::timing` owns: its knob of the same
/// name, live once ktpu-l1 carries it.
pub const L1_TIMING: &[&str] = &[
    "l1.sched/timing-flat",
    "l2.timing/fill-free",
    "l2.timing/lat-mm-19",
    "l2.timing/loop-passes-2",
];

/// The [`L1_TIMING`] mutants ktpu-l1 carries a knob for.
pub fn l1_knobs() -> Vec<&'static str> {
    L1_TIMING
        .iter()
        .copied()
        .filter(|n| ktpu_l1::mutant::NAMES.contains(n))
        .collect()
}

/// Whether mutant `name` is active on this thread.
#[inline(always)]
pub fn on(name: &str) -> bool {
    #[cfg(feature = "mutants")]
    {
        STATE.with(|s| s.get().active == Some(name))
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = name;
        false
    }
}

/// Whether register-file bank placement is off on this thread.
pub fn banks_off() -> bool {
    STATE.with(|s| s.get().banks_off)
}

/// This thread's switches.
pub fn current() -> State {
    STATE.with(|s| s.get())
}

/// `f()` with the switches `state` on this thread.
pub fn scoped<T>(state: State, f: impl FnOnce() -> T) -> T {
    struct Reset(State);
    impl Drop for Reset {
        fn drop(&mut self) {
            STATE.with(|s| s.set(self.0));
        }
    }
    let _reset = Reset(STATE.with(|s| s.replace(state)));
    #[cfg(feature = "mutants")]
    if let Some(name) = state.l1 {
        return ktpu_l1::mutant::with(name, f);
    }
    f()
}

/// Every mutant ktpu-l2 carries, by its Python name.
pub const NAMES: &[&str] = &[
    "l2.predicate-select/off",
    "l2.predicate-select/wrong-arm",
    "l2.packing/overlap-no-reuse-wait",
    "l2.packing/pad-nonzero",
    "l2.packing/batch-no-pack-wait",
    "l2.packing/absolute-zero",
    "l2.packing/no-tile-wait",
    "l2.packing/alias-slots",
    "l2.packing/drop-finite-check",
    "l2.packing/ignore-policy",
    "l2.packing/wrong-order",
    "l2.packing/wrong-layout",
    "l2.packing/wrong-scale",
    "l2.packing/no-drain-wait",
    "l2.mover/word-streams",
    "l2.mover/unaligned-row-cut",
    "l2.vector/word-row-count",
    "l2.vector/no-line-mask-cycle",
    "l2.class/squeeze-walk",
    "l2.class/carry-init",
    "l2.cluster/no-idle-post",
    "l2.mover/unchecked-class-depth",
    "l2.class/no-idle",
    "l2.class/no-mask",
    "l2.class/share-images",
    "l2.body/scratch-dropped",
    "l2.body/loops-unrolled",
    "l2.body/span-short",
    "l2.body/span-reversed",
    "l2.body/type-mode-dropped",
    "l2.body/no-node-kind",
    "l2.body/pipe-dropped",
    "l2.body/phase-per-stage",
    "l2.buffer/no-f16",
    "l2.buffer/no-f32",
    "l2.buffer/mx7-layout-unread",
    "l2.memory/kblock-16",
    "l2.memory/subtile-16",
    "l2.memory/lo-ignored",
    "l2.cluster/bank-128",
    "l2.cluster/acc-4095",
    "l2.cluster/max-fill-256",
    "l2.cluster/max-fill-100",
    "l2.cluster/zero-ignored",
    "l2.cluster/no-fuse",
    "l2.cluster/bank0-only",
    "l2.cluster/drain-uncounted",
    "l2.lower/scratch-dropped",
    "l2.lower/pool-128",
    "l2.lower/no-close",
    "l2.lower/no-shared-image",
    "l2.lower/no-merge",
    "l2.lower/share-unpaired",
    "l2.lower/share-hoist-fills",
    "l2.lower/added-into-ignored",
    "l2.lower/adds-unordered",
    "l2.lower/far-adds-at-once",
    "l2.vector/no-serial-loop",
    "l2.vector/search-each",
    "l2.vector/colour-one",
    "l2.passes/step-near",
    "l2.passes/step-carried-ignored",
    "l2.passes/step-when-dropped",
    "l2.passes/retile-invariant-scaled",
    "l2.passes/retile-no-columns",
    "l2.passes/retile-keeps-unlowered",
    "l2.passes/space-off",
    "l2.passes/pipeline-off",
    "l2.passes/stage-off",
    "l2.passes/stage-one-word-ignored",
    "l2.passes/stage-every-move",
    "l2.lower/recv-unsent",
    "l2.lower/unreceived-ignored",
    "l2.lower/fill-alone",
    "l2.vector/imem-unbounded",
    "l2.vector/registers-unrefused",
    "l2.vector/search-again",
    "l2.vector/pin-unforced",
    "l2.vector/sticky-unchecked",
    "l2.vector/view-size-unchecked",
    "l2.vector/staged-across-blocks",
    "l2.vector/pack-first-dest",
    "l2.vector/band-no-broadcast",
    "l2.vector/lead-latency-only",
    "l2.vector/pred-one-copy",
    "l2.vector/place-keeps-early",
    "l2.vector/xreads-dropped",
    "l2.vector/ii-unbounded",
    "l2.passes/split-off",
    "l2.banks/rotates-none",
    "l2.vector/hold-unforced",
    "l2.vector/pinned-row-fixed",
    "l2.vector/place-overlaps",
    "l2.passes/stage-first-plan",
    "l2.passes/stage-single-word",
    "l2.lower/handshake-always",
    "l2.passes/retile-columns-scaled",
    "l2.lower/landing-bank1",
    "l2.lower/spend-split-128",
    "l2.sync/never-clash",
    "l2.sync/whole-buffer",
    "l2.sync/flow-clashes",
    "l2.sync/own-clashes",
    "l2.sync/every-mark",
    "l2.sync/forget-done",
    "l2.sync/no-ahead",
    "l2.sync/mark-counter",
    "l2.mover/no-fill",
    "l2.mover/no-merge",
    "l2.mover/max-index-8",
    "l2.mover/no-cut",
    "l2.mover/pitch-doubled",
    "l2.mover/ndim-7",
    "l2.mover/const-zero",
    "l2.mover/tuples-unread",
    "l2.pipeline/reordered",
    "l2.target/vregs-default",
    "l2.passes/select-off",
    "l2.passes/retile-bounds-unscaled",
    "l2.passes/any-on-absent-kind",
    "l2.vector/shape-unchecked",
    "l2.vector/timing-constant",
    "l2.vector/slowest-first",
    "l2.banks/drops-op",
    "l2.banks/rotates-predicated",
    "l2.vector/l1-384",
    "l2.vector/unary-swapped",
    "l2.vector/binary-swapped",
    "l2.vector/ternary-swapped",
    "l2.vector/cmp-swapped",
    "l2.vector/reduce-swapped",
    "l2.vector/xlane-rot",
    "l2.vector/row-64",
    "l2.vector/descs-1",
    "l2.vector/loop-s13",
    "l2.vector/stk-s13",
    "l2.vector/no-prefetch",
    "l2.vector/no-twin",
    "l2.vector/tile-loads-rows",
    "l2.vector/no-kreg",
    "l2.vector/no-modulo",
    "l2.vector/broadcast-unread",
    "l2.vector/sel-arms-swapped",
    "l2.vector/no-fusion",
    "l2.vector/no-quantiser-order",
    "l2.vector/statistic-band-pack",
    "l2.vector/compact-reversed",
    "l2.vector/pack-walk-plain",
    "l2.vector/send-uncounted",
    "isa.vector/links",
    "isa.vector/lower",
];
