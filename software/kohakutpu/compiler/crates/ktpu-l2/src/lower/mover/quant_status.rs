//! A deferred quant's finite status (L2.MOV.019): the caller's buffer holds
//! a record for each core of each piece until a `quant_check` checks it.

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

/// The records still pending in a function.
#[derive(Default)]
pub struct Pending(Vec<Status>);

impl Pending {
    /// Refuse `accesses` writing a pending record: a write would erase its
    /// evidence before it is checked.
    pub fn protect(&self, body: &Body, st: &Stmt, accesses: &[Access]) -> Result<()> {
        let hit = accesses.iter().any(|a| {
            a.write && a.key.0 == "mem" && self.0.iter().any(|p| p.overlaps(&a.key.1, a.lo, a.hi))
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
        if self
            .0
            .iter()
            .any(|p| p.overlaps(&s.name, s.offset, s.offset + s.bytes))
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
    pub fn record(&mut self, s: &Status, accesses: &[Access]) {
        for a in accesses {
            if a.write
                && a.key.0 == "mem"
                && a.key.1 == s.name
                && a.lo >= s.offset
                && a.hi <= s.offset + s.bytes
            {
                self.0.push(Status {
                    name: s.name.clone(),
                    offset: a.lo,
                    bytes: a.hi - a.lo,
                });
            }
        }
    }

    /// `quant_check` of `region`: one barrier, then every pending record in
    /// it asserted zero, in one fenced stage.
    pub fn check(&mut self, body: &Body, st: &Stmt, region: &Status) -> Result<Stage> {
        let inside = |p: &Status| {
            p.name == region.name
                && p.offset >= region.offset
                && p.offset + p.bytes <= region.offset + region.bytes
        };
        let (mut checked, rest): (Vec<Status>, Vec<Status>) =
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
        for s in &checked {
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
