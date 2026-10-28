//! Software pipelining of a vector core's stream loop (L2.VC.014): one trip's
//! ops become an iteration started every II cycles, registers rotating over K
//! copies, each load and store a ring of K L1 slots, every VSYNC's slack the
//! exact count it may leave outstanding.
//!
//! The model is the V2 core's (L2.SEARCH.002): a math beat a cycle, a
//! dependent math op `vc.timing.math_math` after its producer's last beat; an
//! unpack or pack a word a cycle; on a two-copy register file a beat reading
//! three distinct registers of one bank is held a cycle.

mod emit;
mod ops;
mod sched;

use ktpu_l1::sched::timing::Params;

#[cfg(feature = "witness")]
use super::pat;
use crate::target::Target;

#[cfg(feature = "witness")]
pub use emit::emit;
pub(super) use emit::typed as emit_typed;
pub use emit::{Ring, TAILS};
use ops::link;
#[cfg(feature = "witness")]
pub use ops::parse;
pub use ops::typed::derive;
pub use ops::MOp;
pub use sched::{plan, Plan};

/// The scheduler's steps one at a time, for tests that check each against
/// a reference: dependences, placement at one II, register colouring, and
/// the last II `plan` tried on this thread.
#[doc(hidden)]
pub mod internals {
    pub use super::ops::link;
    pub use super::sched::{colour, last_ii, place, Life};
}

/// The core facts the model reads, from the machine: `vc.timing` (the image
/// scheduler's own table, RUL.063) and the register file's shape.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Core {
    pub timing: Params,
    /// Lanes a math beat covers (`vc.lanes`).
    pub lanes: i64,
    /// A register's chunks (`vc.chunks`): the words a whole unpack or pack moves.
    pub chunks: i64,
    /// Register-file banks a chunk index alternates over (`vc.rf_banks`).
    pub rf_banks: u64,
    /// Predicate registers the K copies rotate over: `vc.preds` less p0.
    pub preds: i64,
    /// The scalar register of the loop count.
    pub loop_s: i64,
}

impl Core {
    pub fn of(t: &Target) -> Core {
        let mut timing = Params::of(&t.machine);
        if crate::mutant::on("l2.vector/timing-constant") {
            (
                timing.math_math,
                timing.xbar,
                timing.unpack_math,
                timing.math_pack,
            ) = (20, 7, 12, 20);
        }
        Core {
            timing,
            lanes: t.lanes,
            chunks: t.chunks,
            rf_banks: t.machine.vc.rf_banks as u64,
            preds: t.machine.vc.preds as i64 - 1,
            loop_s: t.sregs - 1,
        }
    }

    /// Beats of a math op over `vl` lanes (a whole register without `vl`).
    pub fn beats(&self, vl: Option<u64>) -> u64 {
        match vl {
            Some(vl) => 1.max(vl.div_ceil(self.lanes as u64)),
            None => self.chunks as u64,
        }
    }
}

/// a value a pack reads keeps its register this much longer than its last read
const PAD_MIN: i64 = 64;
/// the predicate a compare writes, as a dependence name
pub const PRED: &str = "#p1";
/// placement attempts an op may take in `place`, beyond its first
const PLACE_BUDGET: usize = 6;

/// Math mnemonics reading a second source (b).
pub const READS_B: &[&str] = &[
    "vmul", "vfma", "vfnma", "vmax", "vmin", "vsel", "vcmplt", "vcmpgt", "vcmpeq", "vexp2d",
];
/// Math mnemonics reading a third source (c).
pub const READS_C: &[&str] = &["vadd", "vsub", "vfma", "vfnma", "vsel"];
/// Math mnemonics writing a predicate.
pub const COMPARE: &[&str] = &["vcmplt", "vcmpgt", "vcmpeq"];

pub fn floor_div(a: i64, b: i64) -> i64 {
    a.div_euclid(b)
}

pub fn floor_mod(a: i64, b: i64) -> i64 {
    a.rem_euclid(b)
}

/// Fills of `words` L1 words issue this far ahead of the iteration's first
/// unpack: `fill` = (latency, cycles a word).
pub fn lead(words: i64, fill: (i64, i64)) -> i64 {
    if crate::mutant::on("l2.vector/lead-latency-only") {
        return fill.0;
    }
    fill.0 + fill.1 * words
}

/// Predicate registers the K copies rotate over: the most, up to `preds`,
/// dividing K.
pub fn pred_copies(k: i64, preds: i64) -> i64 {
    if crate::mutant::on("l2.vector/pred-one-copy") {
        return 1;
    }
    (1..=preds).filter(|d| k % d == 0).max().unwrap()
}
