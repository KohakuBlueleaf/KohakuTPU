//! Spec-vector mutants (`software/tests/spec/runners/l1.py`, `@mutant`):
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

/// `f()` with mutant `name` active on this thread, and the point it breaks
/// in ktpu-isa or ktpu-l0 when it names one ([`ALIASES`]).
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
    match ALIASES.iter().find(|a| a.0 == name) {
        Some((_, Crate::Isa, other)) => ktpu_isa::mutant::with(other, f),
        Some((_, Crate::L0, other)) => ktpu_l0::mutant::with(other, f),
        None => f(),
    }
}

/// The crate a mutated point below ktpu-l1 lives in.
#[derive(Clone, Copy, Debug)]
pub enum Crate {
    Isa,
    L0,
}

/// L1 mutants whose Python patch replaces a function ktpu-isa or ktpu-l0
/// owns: that crate's knob for the same function, switched with it.
pub const ALIASES: &[(&str, Crate, &str)] = &[
    (
        "l1.lower/static-loop-wait",
        Crate::L0,
        "l0.program/static-loop-wait",
    ),
    ("l1.mg/peer-bytes", Crate::Isa, "isa.cluster/coord"),
    ("l1.lower/await-count", Crate::L0, "l0.build/await_plus_one"),
    ("l1.lower/post-blocks", Crate::L0, "l0.build/never_posted"),
    ("l1.lower/tmp-reg", Crate::L0, "l0.program/tmp_reg"),
];

/// Every mutant ktpu-l1 carries, by its Python name.
pub const NAMES: &[&str] = &[
    "l1.lower/static-loop-wait",
    "l1.layout/word-aligned-arena",
    "l1.img/parameter-elision",
    "rul.003/q1",
    "l1.text/no-annotation",
    "l1.text/flat-indent",
    "l1.text/newline-in-brackets",
    "l1.text/no-comment",
    "l1.text/no-commas",
    "l1.text/kwargs-any-lhs",
    "l1.text/size-decimal",
    "l1.text/call-spaced",
    "l1.text/plus-binop",
    "l1.text/no-left-arrow",
    "l1.text/placed-first",
    "l1.module/levels",
    "l1.module/target-name",
    "l1.module/no-params",
    "l1.expand/no-unroll",
    "l1.expand/drop-expand",
    "l1.expand/lt-inverted",
    "l1.body/unbound",
    "l1.body/nbytes",
    "l1.body/one-mesh",
    "l1.node/reg-off-by-one",
    "l1.node/val-const-only",
    "l1.node/alu-table",
    "l1.node/ctrl-offsets",
    "l1.node/goto-signed",
    "l1.node/ring-unfenced",
    "l1.node/slot-keeps",
    "l1.node/no-wait-slot",
    "l1.node/signal-swapped",
    "l1.node/push-len-bit",
    "l1.node/recv-any",
    "l1.node/query-index-bit",
    "l1.node/wait-ignores-mark",
    "l1.node/no-wait",
    "l1.node/no-barrier",
    "l1.node/mark-token",
    "l1.node/post-blocks",
    "l1.unit/unsorted",
    "l1.mg/side",
    "l1.mg/port",
    "l1.mg/node-coord",
    "l1.mg/gemm-no-acc",
    "l1.mg/gemm-anchor",
    "l1.mg/no-fuse",
    "l1.mg/fill-no-recv",
    "l1.mg/peer-bytes",
    "l1.mg/collect-newest",
    "l1.vc/forget-resident",
    "l1.vc/desc-given-dims",
    "l1.vc/peer-count",
    "l1.img/reads",
    "l1.img/regs32",
    "l1.img/dma-rel",
    "l1.img/compare-pred",
    "l1.img/pred-unless",
    "l1.img/chunk-stride",
    "l1.img/xmodes",
    "l1.img/vl-default",
    "l1.img/chunks",
    "l1.img/no-stale",
    "l1.img/waiters",
    "l1.img/vbar-q",
    "l1.img/ainc-abs",
    "l1.img/pointers",
    "l1.img/e8m15",
    "l1.img/getstk",
    "l1.img/loop-len",
    "l1.img/no-hoist",
    "l1.img/no-tracking",
    "l1.img/halt",
    "l1.img/args-reversed",
    "l1.img/desc-none",
    "l1.img/key-all-params",
    "l1.img/skipz-meet",
    "l1.img/peer-xy",
    "l1.mov/levels-reversed",
    "l1.mov/windows-dropped",
    "l1.mov/ew",
    "l1.mov/gen-is-fill",
    "l1.mov/xform-ids",
    "l1.mov/gather-fp",
    "l1.mov/seed-by-base",
    "l1.mov/row-pitch",
    "l1.lower/credit",
    "l1.ruled/before",
    "l1.lower/no-hook",
    "l1.lower/hook-without-resident",
    "l1.lower/hit-evicted-first",
    "imr/key-identity",
    "imr/predict-external",
    "imr/union-image",
    "imr/dead-payload",
    "imr/first-halt",
    "imr/patch-is-constant",
    "imr/forget-patch-alias",
    "imr/first-iteration-only",
    "imr/engine-stale-target",
    "imr/prefix-stale-alu",
    "l1.lower/no-engine",
    "l1.lower/desc-always",
    "l1.lower/await-count",
    "l1.lower/alu-early",
    "l1.lower/tmp-reg",
    "l1.lower/post-blocks",
    "l1.lower/stream-reversed",
    "l1.lower/no-spans",
    "l1.lower/jump-unresolved",
    "l1.lower/no-bind",
    "l1.sched/sync-movable",
    "l1.sched/l1-words",
    "l1.sched/latency",
    "l1.sched/cfg-cost",
    "l1.sched/min-gain",
    "l1.sched/timing-flat",
    "l1.sched/unstable",
    "l1.sched/fixed-shape",
    "l1.facts/mx7-fixed",
    "l1.facts/word-32",
    "l1.facts/mx7-byte",
    "l1.layout/no-arena",
    "l1.img/flit-pc",
    "l1.body/wrong-reason",
    "l1.text/col-off",
    "l1.text/no-right-parens",
    "l1.layout/subtile-transposed",
    "l1.layout/partial-tile",
    "l1.layout/slot-order",
    "l1.mg/sink-unchecked",
    "l1.mg/ack-swapped",
    "l1.img/vwait-any",
    "l1.img/drain-any-mesh",
    "l1.mov/plain-unchecked",
    "l1.node/compare-unsigned",
    "l1.node/load-index-dropped",
    "l1.verify/no-residents",
    "l2.timing/fill-free",
    "l2.timing/lat-mm-19",
    "l2.timing/loop-passes-2",
    "isa.vector/pred",
    "isa.vector/compare",
    "isa.vector/sync_codes",
    "isa.cluster/node",
    "isa.cluster/units",
    "isa.vector/resident",
    "isa.vector/limits",
    "isa.vector/desc_dims",
    "isa.vector/no_setup",
    "isa.vector/no_stale",
    "num.mx7/scale-bias-21",
    "num.mx7/scale-one-up",
    "num.mx7/e8m0-reciprocal",
    "num.mx7/recip-shift-13",
    "num.mx7/scale-power-of-two",
    "num.layout/a-as-b",
    "num.layout/b-as-a",
    "num.layout/words-big-endian",
    "num.layout/entries-group-major",
    "num.layout/tiles-column-major",
    "num.buffer/f16-big-endian",
    "num.buffer/f32-as-f16",
    "num.buffer/mx7-two-bytes",
    "num.buffer/no-key-check",
    "num.image/modes-swapped",
    "num.vector/mx7-mode-2",
    "num.vector/mx7-four-chunks",
    "num.fp16/consumer-ignored",
];
