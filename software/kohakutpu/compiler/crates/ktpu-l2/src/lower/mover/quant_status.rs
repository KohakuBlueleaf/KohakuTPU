//! A deferred quant's finite status (L2.MOV.019): the caller's buffer holds
//! a record for each core until a `quant_check` checks it.

use super::*;
use crate::lower::sync::{Stage, FENCE};
use ktpu_machine::packing::STATUS_RECORD;

/// A byte range of a status buffer: a quant's region, or a pending record.
#[derive(Clone, Debug)]
pub struct Status {
    pub name: String,
    pub offset: i64,
    pub bytes: i64,
}

impl Status {
    pub fn address(&self) -> String {
        if self.offset == 0 {
            self.name.clone()
        } else {
            format!("{} + {}", self.name, self.offset)
        }
    }

    fn overlaps(&self, name: &str, lo: i64, hi: i64) -> bool {
        self.name == name && lo < self.offset + self.bytes && self.offset < hi
    }
}

/// The region `term` names: contiguous whole records.
pub fn region(body: &Body, st: &Stmt, term: &Term, env: &Env, tg: &Target) -> Result<Status> {
    let (name, offset, levels) = words(body, st, term, env, tg)?;
    let levels = merge(&squeeze(&levels));
    let bytes = levels[0].0 * levels[0].1;
    if levels.len() != 1
        || levels[0].1 != tg.word_bytes
        || offset % STATUS_RECORD != 0
        || bytes % STATUS_RECORD != 0
    {
        return Err(fail(
            body,
            st,
            "L2.MOV.019",
            format!("a quant status is contiguous whole {STATUS_RECORD}-byte records"),
        ));
    }
    Ok(Status {
        name,
        offset,
        bytes,
    })
}

/// The image clearing a core's finite accumulator (v31) and its sticky latch.
pub fn clear_image(name: &str) -> String {
    super::packing::clear(name)
}

/// The image draining a core's finite accumulator into its record (`status`
/// parameter), through L1 words [`DRAIN_L1`].
pub fn drain_image(name: &str) -> String {
    super::packing::status(name)
}

/// The L1 words the drain image packs the accumulator through.
pub const DRAIN_L1: (i64, i64) = (384, 388);

/// The vector register a checked core accumulates its finite status in.
pub const STATUS_V: u32 = 31;

struct Record {
    status: Status,
    node: i64,
    phase: i64,
}

/// The records still pending in a function, with their publication scopes.
#[derive(Default)]
pub struct Pending(Vec<Record>);

impl Pending {
    /// Refuse `accesses` writing a pending record: a write would erase its
    /// evidence before it is checked.
    pub fn protect(&self, body: &Body, st: &Stmt, accesses: &[Access]) -> Result<()> {
        let hit = accesses.iter().any(|a| {
            a.write
                && a.key.0 == "mem"
                && self
                    .0
                    .iter()
                    .any(|p| p.status.overlaps(&a.key.1, a.lo, a.hi))
        });
        if hit {
            return Err(fail(
                body,
                st,
                "L2.MOV.019",
                "a write over a quant status pending its quant_check",
            ));
        }
        Ok(())
    }

    /// A deferred quant `st`'s region `s`: on no operand of its own, over no
    /// pending record.
    pub fn admit(&self, body: &Body, st: &Stmt, env: &Env, s: &Status) -> Result<()> {
        for t in st.positional().into_iter().filter(|t| !t.is_arrow("<-")) {
            if view_name(&value(body, st, &as_view(t), env)?) != s.name {
                continue;
            }
            return Err(fail(
                body,
                st,
                "L2.MOV.019",
                "a quant status is no operand of its quant",
            ));
        }
        self.claim(body, st, s)
    }

    /// Region `s` taken for a new record: over no pending record.
    pub fn claim(&self, body: &Body, st: &Stmt, s: &Status) -> Result<()> {
        if self
            .0
            .iter()
            .any(|p| p.status.overlaps(&s.name, s.offset, s.offset + s.bytes))
        {
            return Err(fail(
                body,
                st,
                "L2.MOV.019",
                "a quant status over a record pending its quant_check",
            ));
        }
        Ok(())
    }

    /// The records a deferred quant into `s` wrote (`accesses`).
    pub fn record(&mut self, s: &Status, accesses: &[Access], node: i64, phase: i64) {
        for a in accesses {
            if a.write
                && a.key.0 == "mem"
                && a.key.1 == s.name
                && a.lo >= s.offset
                && a.hi <= s.offset + s.bytes
            {
                self.0.push(Record {
                    status: Status {
                        name: s.name.clone(),
                        offset: a.lo,
                        bytes: a.hi - a.lo,
                    },
                    node,
                    phase,
                });
            }
        }
    }

    /// `quant_check` of `region`: one barrier, then every pending record in
    /// it asserted zero, in one fenced stage.
    pub fn check(
        &mut self,
        body: &Body,
        st: &Stmt,
        region: &Status,
        node: i64,
        phase: i64,
    ) -> Result<Stage> {
        let inside = |p: &Record| {
            p.status.name == region.name
                && p.status.offset >= region.offset
                && p.status.offset + p.status.bytes <= region.offset + region.bytes
        };
        if self
            .0
            .iter()
            .any(|p| inside(p) && p.node != node && p.phase >= phase)
        {
            return Err(fail(
                body,
                st,
                "L2.MOV.019",
                "a remote quant_check must follow the producer's node scope",
            ));
        }
        let (mut checked, rest): (Vec<Record>, Vec<Record>) =
            std::mem::take(&mut self.0).into_iter().partition(inside);
        self.0 = rest;
        if checked.is_empty() {
            return Err(fail(
                body,
                st,
                "L2.MOV.019",
                "a quant_check of no pending quant status",
            ));
        }
        if on("l2.packing/deferred-last-only") {
            checked.drain(..checked.len() - 1);
        }
        let mut lines = vec!["barrier".to_string()];
        let mut accesses = Vec::new();
        for record in &checked {
            let s = &record.status;
            for offset in (s.offset..s.offset + s.bytes).step_by(STATUS_RECORD as usize) {
                lines.extend([
                    format!("r14 = load {} + {offset}", s.name),
                    "assert_zero r14 code=1".into(),
                ]);
            }
            accesses.push(Access::new(
                FENCE,
                false,
                ("mem", &s.name),
                s.offset,
                s.offset + s.bytes,
            ));
        }
        let mut stage = Stage::new(lines, Vec::new(), accesses);
        stage.fence = true;
        stage.closes = true;
        Ok(stage)
    }

    /// Refuse a function ending with a record unchecked.
    pub fn end(&self, body: &Body, st: &Stmt) -> Result<()> {
        if self.0.is_empty() {
            return Ok(());
        }
        Err(fail(
            body,
            st,
            "L2.MOV.019",
            "a deferred quant status unchecked at the function's end",
        ))
    }
}
