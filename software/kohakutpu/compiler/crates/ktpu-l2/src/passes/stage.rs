//! `stage` (L2.PASS.001): on a mover with silicon flaw F-001
//! (`flaws.F-001.present`), each node-scope gather and scatter some instance
//! of which steps one-word rows, more than one word, and each gather of
//! even-word rows whose destination walk is not proven free of a chained
//! one-beat burst (`MV::burst_safe`), goes through a contiguous scratch (the
//! copy out of it is then L2.MOV.020's); on any machine a copy whose source
//! and destination spans of one buffer overlap does, so the copy reads its
//! whole source before it writes (L2.MOV.021):
//!
//! ```text
//! gather D <- S ..   ->  gather hN <- S ..; copy D <- hN
//! scatter D <- S ..  ->  copy hN <- S; scatter D <- hN ..
//! copy D <- S [walk= at= perm=] [dwalk= dat=]
//!                    ->  copy hN <- S [walk= at= perm=]; copy D <- hN [dwalk= dat=]
//! ```

use std::collections::{BTreeSet, HashMap};

use indexmap::IndexMap;
use ktpu_l1::text::{emit, Stmt, Term};

use super::{body_of, head, unit_of, walk, write};
use crate::ir::read::instance_scopes;
use crate::ir::{value, Body, Program, Result, StageX};
use crate::lower::memory as M;
use crate::lower::mover as MV;
use crate::target::Target;

type Key = (u32, u32, String);
type Plan = (String, Vec<i64>);

/// Why a statement goes through a scratch, if it does.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Why {
    No,
    /// F-001: one-word runs on both sides.
    Flaw,
    /// A copy within one buffer whose spans overlap (L2.MOV.021).
    Alias,
}

/// A statement as the pass's text and the body's tree both hold it.
fn key_of(st: &Stmt) -> Key {
    (st.line, st.col, emit(std::slice::from_ref(st)))
}

/// Every identifier-like word of a text.
fn words(text: &str) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let b = text.as_bytes();
    let w = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let mut i = 0;
    while i < b.len() {
        if w(b[i]) {
            let s = i;
            while i < b.len() && w(b[i]) {
                i += 1;
            }
            if b[s].is_ascii_alphabetic() || b[s] == b'_' {
                out.insert(text[s..i].to_string());
            }
            continue;
        }
        i += 1;
    }
    out
}

fn plan_of(body: &Body, st: &Stmt, env: &ktpu_l1::text::Env, t: &Target) -> Result<(Why, Plan)> {
    let word = t.word_bytes;
    let lead = t.mover_lead || crate::mutant::on("l2.passes/stage-one-word-ignored");
    if st.op == "copy" {
        let ((dn, doff, dl), (sn, soff, sl)) = MV::copy_walks(body, st, env, t)?;
        let buf = &body.buffers[&dn];
        let n: i64 = dl.iter().map(|l| l.0).product();
        let count: i64 = buf.shape.iter().product();
        let (_, dlo, dhi) = MV::span_of(&dn, doff, &dl, word);
        let (_, slo, shi) = MV::span_of(&sn, soff, &sl, word);
        let why =
            if dn == sn && dlo < shi && slo < dhi && !crate::mutant::on("l2.passes/alias-in-place")
            {
                Why::Alias
            } else {
                Why::No
            };
        return Ok((
            why,
            (buf.dtype.clone(), vec![n * word * count / buf.nbytes(t)]),
        ));
    }
    let (rn, roff, rows, per, rl) = MV::rows_walk(body, st, env, t)?;
    let line = i64::from(t.machine.card.dram.line_bytes);
    let cap = i64::from(t.machine.mover.burst_max);
    // A gather's scratch packs its rows from an even word: even rows prove it.
    let packs = st.op == "gather"
        && per % 2 == 0
        && !MV::burst_safe(roff, &rl, word, line, cap)
        && !crate::mutant::on("l2.passes/stage-unproven-rows-kept");
    let one = lead
        && ((per == 1 && MV::run_words(&rl, word) == 1)
            || packs
            || crate::mutant::on("l2.passes/stage-every-move"));
    let pos: Vec<&Term> = st.positional();
    let side = pos[if st.op == "gather" { 0 } else { 2 }];
    let tv = match side {
        Term::Name(n) => Term::View(n.clone(), vec![]),
        other => other.clone(),
    };
    let v = value(body, st, &tv, env)?;
    let buf = &body.buffers[&rn];
    let ax = M::axes_of(&v, &buf.shape).map_err(|e| body.reader.fail(st, "L2.MOV.002", e))?;
    let many = rows * per > 1 || crate::mutant::on("l2.passes/stage-single-word");
    let why = if one && many { Why::Flaw } else { Why::No };
    Ok((why, (buf.dtype.clone(), M::shape_of(&ax))))
}

/// A staged statement: its scratch plan (None when its instances need
/// different ones) and whether some instance overlaps itself.
type Staged = (Option<Plan>, bool);

/// `{statement key: staged}` of each node-scope gather, scatter and copy some
/// instance of which goes through a scratch.
fn plans(body: &Body, t: &Target) -> Result<IndexMap<Key, Staged>> {
    let mut seen: IndexMap<Key, Vec<(Why, Plan)>> = IndexMap::new();
    for s in instance_scopes(body)? {
        let StageX::Op(n, env) = &s.x else { continue };
        let op = n.as_op().unwrap();
        if ["gather", "scatter", "copy"].contains(&op.op.as_str()) {
            let got = plan_of(body, &op.st, env, t)?;
            seen.entry(key_of(&op.st)).or_default().push(got);
        }
    }
    let mut out = IndexMap::new();
    for (k, got) in seen {
        if got.iter().all(|(why, _)| *why == Why::No) {
            continue;
        }
        let alias = got.iter().any(|(why, _)| *why == Why::Alias);
        let distinct: BTreeSet<&Plan> = got.iter().map(|(_, p)| p).collect();
        let plan = if distinct.len() == 1 || crate::mutant::on("l2.passes/stage-first-plan") {
            Some(got[0].1.clone())
        } else if alias {
            // A copy's scratch is its destination's words, one dimension of
            // the buffer's type: the largest instance's holds every one.
            let words = got.iter().map(|(_, p)| p.1[0]).max();
            words.map(|w| (got[0].1 .0.clone(), vec![w]))
        } else {
            None
        };
        out.insert(k, (plan, alias));
    }
    Ok(out)
}

/// Statement `s` with its `k`-th positional argument `new`.
fn operand(s: &Stmt, k: usize, new: &Term) -> Stmt {
    let mut out = s.clone();
    let mut i = 0;
    out.args = s
        .args
        .iter()
        .map(|a| {
            if a.as_kwarg().is_some() {
                return a.clone();
            }
            let r = if i == k { new.clone() } else { a.clone() };
            i += 1;
            r
        })
        .collect();
    out
}

pub fn stage(p: &Program, t: &Target) -> Result<Program> {
    if crate::mutant::on("l2.passes/stage-off") {
        return Ok(p.clone());
    }
    let body = p.body()?;
    let pl = plans(&body, t)?;
    if pl.is_empty() {
        return Ok(p.clone());
    }
    let (params, stmts) = body_of(p)?;
    let mut used = words(&p.module.source);
    let mut made: Vec<Stmt> = Vec::new();
    let out = walk(&stmts, &mut |s: Stmt, scopes: &[Stmt]| {
        let staged = if ["gather", "scatter", "copy"].contains(&s.op.as_str()) {
            pl.get(&key_of(&s)).cloned()
        } else {
            None
        };
        let Some((plan, alias)) = staged else {
            return Ok(vec![s]);
        };
        let nodes: Vec<&Stmt> = scopes
            .iter()
            .filter(|q| q.op == "par" && unit_of(q).as_deref() == Some("node"))
            .collect();
        let heads: Vec<(String, Term, Term)> = nodes.iter().filter_map(|q| head(q)).collect();
        let literal = heads
            .iter()
            .all(|(_, lo, hi)| matches!(lo, Term::Int(..)) && matches!(hi, Term::Int(..)));
        let (Some((dtype, shape)), true) = (plan, literal) else {
            if alias {
                return Err(body.reader.fail(
                    &s,
                    "L2.MOV.021",
                    "a copy within one buffer whose spans overlap stages through a scratch sized by constant node bounds",
                ));
            }
            return Ok(vec![s]);
        };
        let h = (0..=used.len())
            .map(|i| format!("h{i}"))
            .find(|h| !used.contains(h))
            .unwrap();
        used.insert(h.clone());
        let mut dims: Vec<Term> = heads
            .iter()
            .map(|(_, lo, hi)| Term::int(hi.as_int().unwrap() - lo.as_int().unwrap()))
            .collect();
        dims.extend(shape.iter().map(|d| Term::int(*d as i128)));
        let mut buf = Stmt::new("buffer", vec![]);
        buf.target = Some(Term::name(h.clone()));
        buf.annot = Some(Term::Placed(
            Box::new(Term::View(dtype, dims)),
            Box::new(Term::name("dram")),
        ));
        buf.line = s.line;
        made.push(buf);
        let idx: Vec<Term> = heads
            .iter()
            .map(|(v, lo, _)| {
                if *lo == Term::int(0) {
                    Term::name(v.clone())
                } else {
                    Term::BinOp('-', Box::new(Term::name(v.clone())), Box::new(lo.clone()))
                }
            })
            .collect();
        let hv = if idx.is_empty() {
            Term::name(h.clone())
        } else {
            Term::View(h.clone(), idx)
        };
        let pos: Vec<Term> = s.positional().into_iter().cloned().collect();
        let (dst, src) = (pos[0].clone(), pos[2].clone());
        let at = |op: &str, args: Vec<Term>| {
            let mut x = Stmt::new(op, args);
            x.line = s.line;
            x.col = s.col;
            x
        };
        let arrow = Term::Arrow("<-".into());
        if s.op == "gather" {
            return Ok(vec![operand(&s, 0, &hv), at("copy", vec![dst, arrow, hv])]);
        }
        if s.op == "scatter" {
            return Ok(vec![
                at("copy", vec![hv.clone(), arrow, src]),
                operand(&s, 2, &hv),
            ]);
        }
        let kw: HashMap<&str, &Term> = s.kwargs().into_iter().collect();
        let side: Vec<Term> = ["walk", "at", "perm"]
            .iter()
            .filter_map(|k| {
                kw.get(k)
                    .map(|v| Term::Assign(Box::new(Term::name(*k)), Box::new((*v).clone())))
            })
            .collect();
        let dside: Vec<Term> = ["dwalk", "dat"]
            .iter()
            .filter_map(|k| {
                kw.get(k)
                    .map(|v| Term::Assign(Box::new(Term::name(*k)), Box::new((*v).clone())))
            })
            .collect();
        let mut a1 = vec![hv.clone(), arrow.clone(), src];
        a1.extend(side);
        let mut a2 = vec![dst, arrow, hv];
        a2.extend(dside);
        Ok(vec![at("copy", a1), at("copy", a2)])
    })?;
    if made.is_empty() {
        return Ok(p.clone());
    }
    let top = out
        .iter()
        .position(|s| s.op != "buffer")
        .unwrap_or(out.len());
    let mut all: Vec<Stmt> = out[..top].to_vec();
    all.extend(made);
    all.extend(out[top..].iter().cloned());
    write(p, &params, &all)
}
