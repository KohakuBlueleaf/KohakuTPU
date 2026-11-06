//! The image scheduler's issue model under RUL.063: the timing module's core
//! (`timing::Core`, the same parameters `timing::cycles` reads), each op
//! dispatching the words the encoder writes for it -- its configuration
//! words only where the encoder emits them. A run is timed from a fresh core
//! and an unknown encoder state.

use std::cell::Cell;
use std::collections::BTreeSet;
use std::rc::Rc;

use crate::mutant;

thread_local! {
    /// Calls of `Ruled::order`, for the `l1.sched/unstable` mutant.
    static CALLS: Cell<u64> = const { Cell::new(0) };
}

use ktpu_machine::Machine;

use super::schedule::{movable, preds, MIN_GAIN};
use super::timing::{Core, MOp};
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

    /// `st` (decoded `op`) issued on `core`, or with `peek` only timed: the
    /// encoder state after it, and its start.
    fn step(
        enc: &mut Encoder<'_>,
        core: &mut Core,
        state: State,
        st: &Stmt,
        op: &mut (String, Option<Rc<MOp>>),
        peek: bool,
    ) -> Result<(State, i64)> {
        let config = enc.config;
        let (words, state) = enc.stmt(st, state, 1)?;
        let mut n = words.len() as i64;
        if mutant::on("l1.sched/cfg-cost") {
            n -= (enc.config - config) as i64;
        }
        let decoded = op.1.get_or_insert_with(|| core.mop(&op.0));
        let start = if peek {
            core.start_of(decoded, n)
        } else {
            core.op_decoded(decoded, n)
        };
        Ok((state, start))
    }

    fn decoded(&self, run: &[Stmt]) -> Vec<(String, Option<Rc<MOp>>)> {
        run.iter()
            .map(|st| {
                let text = emit_at(std::slice::from_ref(st), 0, 48);
                (text.trim().to_string(), None)
            })
            .collect()
    }

    /// The model's cycles for `run` issued in `order`.
    pub fn cycles(&self, run: &[Stmt], order: &[usize]) -> Result<i64> {
        let mut decoded = self.decoded(run);
        self.cycles_decoded(run, order, &mut decoded)
    }

    fn cycles_decoded(
        &self,
        run: &[Stmt],
        order: &[usize],
        decoded: &mut [(String, Option<Rc<MOp>>)],
    ) -> Result<i64> {
        let mut enc = self.encoder();
        let mut core = self.core();
        let mut state = UNKNOWN;
        for &k in order {
            state = Self::step(&mut enc, &mut core, state, &run[k], &mut decoded[k], false)?.0;
        }
        Ok(core.end())
    }

    /// The list scheduler's order of `run` (L1.SCHED.004): repeatedly the
    /// ready statement that starts earliest, ties to the written order; the
    /// written order stands unless the new one is MIN_GAIN faster.
    pub fn order(&self, run: &[Stmt]) -> Result<Vec<usize>> {
        self.priced(run, 0)
    }

    /// [`Ruled::order`] with each configuration word a statement takes added
    /// to its start at `price` cycles (L1.SCHED.007).
    pub fn priced(&self, run: &[Stmt], price: i64) -> Result<Vec<usize>> {
        let mut got = self.list_order(run, price)?;
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

    fn list_order(&self, run: &[Stmt], price: i64) -> Result<Vec<usize>> {
        let n = run.len();
        let written: Vec<usize> = (0..n).collect();
        if n < 3 {
            return Ok(written);
        }
        let preds = preds(run, &self.isa, &self.facts);
        let mut decoded = self.decoded(run);
        let mut enc = self.encoder();
        let mut core = self.core();
        let mut state = UNKNOWN;
        let mut waiting: Vec<usize> = preds.iter().map(|p| p.len()).collect();
        let mut after = vec![Vec::new(); n];
        for (k, p) in preds.iter().enumerate() {
            for &q in p {
                after[q].push(k);
            }
        }
        let mut ready: BTreeSet<usize> = (0..n).filter(|&k| waiting[k] == 0).collect();
        let mut out = Vec::with_capacity(n);
        while out.len() < n {
            let mut best: Option<(i64, usize)> = None;
            for &k in &ready {
                let config = enc.config;
                let (_, start) =
                    Self::step(&mut enc, &mut core, state, &run[k], &mut decoded[k], true)?;
                let start = start + price * (enc.config - config) as i64;
                if best.is_none_or(|(s, _)| start < s) {
                    best = Some((start, k));
                }
            }
            let (_, k) = best.expect("a ready statement");
            state = Self::step(&mut enc, &mut core, state, &run[k], &mut decoded[k], false)?.0;
            ready.remove(&k);
            for &s in &after[k] {
                waiting[s] -= 1;
                if waiting[s] == 0 {
                    ready.insert(s);
                }
            }
            out.push(k);
        }
        let gain = if mutant::on("l1.sched/min-gain") {
            0.5
        } else {
            MIN_GAIN
        };
        if self.cycles_decoded(run, &out, &mut decoded)? as f64
            > self.cycles_decoded(run, &written, &mut decoded)? as f64 * (1.0 - gain)
        {
            return Ok(written);
        }
        Ok(out)
    }

    /// Every run of movable statements of `stmts` reordered, blocks recursed.
    pub fn block(&self, stmts: &[Stmt]) -> Result<Vec<Stmt>> {
        self.block_at(stmts, 0)
    }

    /// [`Ruled::block`] at a configuration word's `price`.
    pub fn block_at(&self, stmts: &[Stmt], price: i64) -> Result<Vec<Stmt>> {
        let mut out = Vec::with_capacity(stmts.len());
        let mut start = 0;
        for (i, st) in stmts.iter().enumerate() {
            if movable(st) {
                continue;
            }
            let run = &stmts[start..i];
            let order = self.priced(run, price)?;
            out.extend(order.into_iter().map(|k| run[k].clone()));
            start = i + 1;
            let body = if st.body.is_empty() {
                Vec::new()
            } else {
                self.block_at(&st.body, price)?
            };
            out.push(st.with_body(body));
        }
        let run = &stmts[start..];
        let order = self.priced(run, price)?;
        out.extend(order.into_iter().map(|k| run[k].clone()));
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
