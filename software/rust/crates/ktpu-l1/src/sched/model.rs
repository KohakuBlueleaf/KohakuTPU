//! The image scheduler's issue model under RUL.063: the timing module's core
//! (`timing::Core`, the same parameters `timing::cycles` reads), each op
//! dispatching the words the encoder writes for it -- its configuration
//! words only where the encoder emits them. A run is timed from a fresh core
//! and an unknown encoder state.

use std::cell::Cell;
use std::collections::BTreeSet;

use crate::mutant;

thread_local! {
    /// Calls of `Ruled::order`, for the `l1.sched/unstable` mutant.
    static CALLS: Cell<u64> = const { Cell::new(0) };
}

use ktpu_machine::Machine;

use super::schedule::{movable, preds, MIN_GAIN};
use super::timing::Core;
use crate::error::Result;
use crate::lower::image::{isa_for, CoreFacts, Encoder, State, UNKNOWN};
use crate::text::{emit_at, Reader, Stmt};
use ktpu_isa::VecIsa;

/// The issue model of one module's images: its reader, the core's ISA and
/// facts at the module's register count, its register-file read copies.
pub struct Ruled<'a> {
    reader: &'a Reader,
    machine: &'a Machine,
    isa: VecIsa,
    facts: CoreFacts,
    copies: u32,
}

impl<'a> Ruled<'a> {
    pub fn new(
        reader: &'a Reader,
        machine: &'a Machine,
        vregs: u32,
        copies: u32,
    ) -> Result<Ruled<'a>> {
        Ok(Ruled {
            reader,
            machine,
            isa: isa_for(machine, vregs)?,
            facts: CoreFacts::of(machine, vregs),
            copies,
        })
    }

    fn encoder(&self) -> Encoder<'_> {
        Encoder::new(self.reader, &self.isa, &self.facts, UNKNOWN, None)
    }

    fn core(&self) -> Core {
        Core::new(self.copies, (0, 0), self.machine)
    }

    /// `st` (printed `text`) issued on `core`, or with `peek` only timed: the
    /// encoder state after it, and its start.
    fn step(
        enc: &mut Encoder<'_>,
        core: &mut Core,
        state: State,
        st: &Stmt,
        text: &str,
        peek: bool,
    ) -> Result<(State, i64)> {
        let config = enc.config;
        let (words, state) = enc.stmt(st, state, 1)?;
        let mut n = words.len() as i64;
        if mutant::on("l1.sched/cfg-cost") {
            n -= (enc.config - config) as i64;
        }
        let start = if peek {
            core.peek(text, n)
        } else {
            core.op(text, n)
        };
        Ok((state, start))
    }

    fn texts(run: &[Stmt]) -> Vec<String> {
        run.iter()
            .map(|st| emit_at(std::slice::from_ref(st), 0, 48).trim().to_string())
            .collect()
    }

    /// The model's cycles for `run` issued in `order`.
    pub fn cycles(&self, run: &[Stmt], order: &[usize]) -> Result<i64> {
        let texts = Self::texts(run);
        let mut enc = self.encoder();
        let mut core = self.core();
        let mut state = UNKNOWN;
        for &k in order {
            state = Self::step(&mut enc, &mut core, state, &run[k], &texts[k], false)?.0;
        }
        Ok(core.end())
    }

    /// The list scheduler's order of `run` (L1.SCHED.004): repeatedly the
    /// ready statement that starts earliest, ties to the written order; the
    /// written order stands unless the new one is MIN_GAIN faster.
    pub fn order(&self, run: &[Stmt]) -> Result<Vec<usize>> {
        let mut got = self.list_order(run)?;
        if mutant::on("l1.sched/unstable") {
            let calls = CALLS.with(|c| {
                c.set(c.get() + 1);
                c.get()
            });
            if calls.is_multiple_of(2) && got.len() >= 3 {
                got.reverse();
            }
        }
        Ok(got)
    }

    fn list_order(&self, run: &[Stmt]) -> Result<Vec<usize>> {
        let n = run.len();
        let written: Vec<usize> = (0..n).collect();
        if n < 3 {
            return Ok(written);
        }
        let preds = preds(run, super::timing::chunks(self.machine));
        let texts = Self::texts(run);
        let mut enc = self.encoder();
        let mut core = self.core();
        let mut state = UNKNOWN;
        let mut done = BTreeSet::new();
        let mut out = Vec::with_capacity(n);
        while out.len() < n {
            let mut best: Option<(i64, usize)> = None;
            for k in 0..n {
                if done.contains(&k) || !preds[k].is_subset(&done) {
                    continue;
                }
                let (_, start) = Self::step(&mut enc, &mut core, state, &run[k], &texts[k], true)?;
                if best.is_none_or(|(s, _)| start < s) {
                    best = Some((start, k));
                }
            }
            let (_, k) = best.expect("a ready statement");
            state = Self::step(&mut enc, &mut core, state, &run[k], &texts[k], false)?.0;
            done.insert(k);
            out.push(k);
        }
        let gain = if mutant::on("l1.sched/min-gain") {
            0.5
        } else {
            MIN_GAIN
        };
        if self.cycles(run, &out)? as f64 > self.cycles(run, &written)? as f64 * (1.0 - gain) {
            return Ok(written);
        }
        Ok(out)
    }

    /// Every run of movable statements of `stmts` reordered, blocks recursed.
    pub fn block(&self, stmts: &[Stmt]) -> Result<Vec<Stmt>> {
        let mut out = Vec::new();
        let mut run: Vec<Stmt> = Vec::new();
        for st in stmts {
            if movable(st) {
                run.push(st.clone());
                continue;
            }
            out.extend(self.order(&run)?.into_iter().map(|k| run[k].clone()));
            run.clear();
            let mut st = st.clone();
            if !st.body.is_empty() {
                st.body = self.block(&st.body)?;
            }
            out.push(st);
        }
        out.extend(self.order(&run)?.into_iter().map(|k| run[k].clone()));
        Ok(out)
    }
}

/// The runs `block` schedules, in its order (blocks recursed).
pub fn runs(stmts: &[Stmt]) -> Vec<Vec<Stmt>> {
    let mut out = Vec::new();
    let mut run: Vec<Stmt> = Vec::new();
    for st in stmts {
        if movable(st) {
            run.push(st.clone());
            continue;
        }
        out.push(std::mem::take(&mut run));
        if !st.body.is_empty() {
            out.extend(runs(&st.body));
        }
    }
    out.push(run);
    out.retain(|r| !r.is_empty());
    out
}
