//! `step` (L2.PASS.007): a vector stream loop some memory view of which steps
//! further a trip than a descriptor's `ainc` reaches becomes a node-scope
//! loop around its `par`, one trip a run (`for t in T..T + 1` inside).

use ktpu_l1::text::{fmt, Env, Expander, Reader, Stmt, Term, Val};

use super::{body_of, head, names, unit_of, walk, write};
use crate::ir::read::instances;
use crate::ir::{Program, Result, StageX};
use crate::lower::vector::{self, Search};
use crate::target::Target;

/// A statement a vector `par` may sit in for a pass to cost it: a node-scope
/// `for` or `par` over nodes with a constant lower bound, or a `when`.
pub fn scope_ok(s: &Stmt) -> bool {
    if s.op == "when" {
        return true;
    }
    if s.op == "for" || (s.op == "par" && unit_of(s).as_deref() == Some("node")) {
        return matches!(head(s), Some((_, Term::Int(..), _)));
    }
    false
}

/// `inner` inside `scopes` (outermost first), each loop narrowed to its first
/// iteration.
pub fn first_of(scopes: &[Stmt], inner: Stmt) -> Stmt {
    let mut inner = inner;
    for s in scopes.iter().rev() {
        let mut s = s.clone();
        if s.op == "for" || s.op == "par" {
            if let Some(Term::Member(lhs, rhs)) = s.positional().first().map(|t| (*t).clone()) {
                if let Term::Span(lo, _) = rhs.as_ref() {
                    let next = Term::int(lo.as_int().unwrap_or(0) + 1);
                    let one = Term::Member(
                        lhs.clone(),
                        Box::new(Term::Span(lo.clone(), Box::new(next))),
                    );
                    let kw: Vec<Term> = s
                        .args
                        .iter()
                        .skip(1)
                        .filter(|a| !matches!(a, Term::Assign(..)))
                        .cloned()
                        .collect();
                    s.args = std::iter::once(one).chain(kw).collect();
                }
            }
        }
        s.body = vec![inner];
        inner = s;
    }
    inner
}

/// The largest byte step a trip of the one vector `par`'s stream loop in
/// `stmts` takes, when past a descriptor's reach (else 0).
fn far(p: &Program, params: &[(String, Term)], stmts: &[Stmt], t: &Target) -> i64 {
    let got = (|| -> Option<i64> {
        let body = write(p, params, stmts).ok()?.body().ok()?;
        let stages = instances(&body).ok()?;
        let x = stages.into_iter().find_map(|s| match s.x {
            StageX::Par(x) => Some(x),
            _ => None,
        })?;
        vector::max_step(&body, x[0].par(), &x[0].env, t).ok()
    })()
    .unwrap_or(0);
    if got >= t.step() {
        got
    } else {
        0
    }
}

/// A trip of `loop` reads a value of its body before making it.
fn carried(lp: &Stmt) -> bool {
    let made: Vec<String> = lp
        .body
        .iter()
        .filter_map(|b| b.target_name().map(str::to_string))
        .collect();
    let mut seen: Vec<String> = Vec::new();
    for b in &lp.body {
        if names(&b.args)
            .iter()
            .any(|n| made.contains(n) && !seen.contains(n))
        {
            return true;
        }
        if let Some(t) = b.target_name() {
            seen.push(t.to_string());
        }
    }
    false
}

/// `(span, conditions)`: the trips `dom` of every instance, and the `when`s
/// keeping each instance's trip to its own.
fn over_instances(r: &Reader, par: &Stmt, dom: &Term, trip: &Term) -> Result<(Term, Vec<Term>)> {
    let Some((var, plo, phi)) = head(par) else {
        return Ok((dom.clone(), vec![]));
    };
    let Term::Span(dlo, dhi) = dom else {
        return Ok((dom.clone(), vec![]));
    };
    if !names(std::slice::from_ref(dom)).contains(&var) {
        return Ok((dom.clone(), vec![]));
    }
    let macros = Default::default();
    let ex = Expander::new(&macros, r, false);
    let at = |x: &Term, env: &Env| -> Result<i64> {
        match ex.term(x, env, par)? {
            Term::Int(v, _) => Ok(v as i64),
            _ => Err(r.fail(
                par,
                "L2.PASS.007",
                format!(
                    "the trips of a stream loop moved out of its `par`: {}",
                    fmt(x)
                ),
            )),
        }
    };
    let (lo, hi) = (at(&plo, &Env::new())?, at(&phi, &Env::new())?);
    let mut ends = Vec::new();
    for c in lo..hi {
        let mut env = Env::new();
        env.insert(var.clone(), Val::Int(c as i128));
        ends.push((at(dlo, &env)?, at(dhi, &env)?));
    }
    let span = Term::Span(
        Box::new(Term::int(
            ends.iter().map(|e| e.0).min().unwrap_or(0) as i128
        )),
        Box::new(Term::int(
            ends.iter().map(|e| e.1).max().unwrap_or(0) as i128
        )),
    );
    let mut conds = Vec::new();
    if names(&[(**dlo).clone()]).contains(&var) {
        conds.push(Term::Compare(
            ">=".into(),
            Box::new(trip.clone()),
            dlo.clone(),
        ));
    }
    if names(&[(**dhi).clone()]).contains(&var) {
        conds.push(Term::Compare(
            "<".into(),
            Box::new(trip.clone()),
            dhi.clone(),
        ));
    }
    Ok((span, conds))
}

pub fn step(p: &Program, t: &Target, _search: &Search) -> Result<Program> {
    let (params, stmts) = body_of(p)?;
    let r = p.module.reader();
    let top: Vec<Stmt> = stmts.iter().filter(|s| s.op == "buffer").cloned().collect();
    let out = walk(&stmts, &mut |s: Stmt, scopes: &[Stmt]| {
        if s.op != "par" || unit_of(&s).as_deref() != Some("vc") || !scopes.iter().all(scope_ok) {
            return Ok(vec![s]);
        }
        let loops: Vec<usize> = s
            .body
            .iter()
            .enumerate()
            .filter(|(_, b)| b.op == "for")
            .map(|(i, _)| i)
            .collect();
        if loops.len() != 1 {
            return Ok(vec![s]);
        }
        let at = loops[0];
        let lp = s.body[at].clone();
        let mut probe = top.clone();
        probe.push(first_of(scopes, s.clone()));
        let far = far(p, &params, &probe, t);
        if far == 0 || crate::mutant::on("l2.passes/step-near") {
            return Ok(vec![s]);
        }
        let carries = carried(&lp) && !crate::mutant::on("l2.passes/step-carried-ignored");
        if s.body.len() > at + 1 || carries {
            return Err(r.fail(
                &lp,
                "L2.PASS.007",
                format!(
                    "a stream loop stepping {far} bytes a trip (ainc reaches {}) carries values from trip to trip",
                    t.step() - 1
                ),
            ));
        }
        let Some(Term::Member(lhs, rhs)) = lp.positional().first().map(|x| (*x).clone()) else {
            return Ok(vec![s]);
        };
        let trip = Term::name(format!("{}_trip", lhs.as_name().unwrap_or("")));
        let one = Term::Member(
            lhs.clone(),
            Box::new(Term::Span(
                Box::new(trip.clone()),
                Box::new(Term::BinOp(
                    '+',
                    Box::new(trip.clone()),
                    Box::new(Term::int(1)),
                )),
            )),
        );
        let mut run = lp.clone();
        run.args = std::iter::once(one)
            .chain(lp.args.iter().skip(1).cloned())
            .collect();
        let (span, mut conds) = over_instances(&r, &s, &rhs, &trip)?;
        if crate::mutant::on("l2.passes/step-when-dropped") {
            conds.clear();
        }
        for cond in conds {
            let mut w = lp.clone();
            w.op = "when".into();
            w.args = vec![cond];
            w.target = None;
            w.annot = None;
            w.body = vec![run];
            run = w;
        }
        let mut inner = s.clone();
        inner.body = s.body[..at]
            .iter()
            .cloned()
            .chain(std::iter::once(run))
            .collect();
        let kw: Vec<Term> = lp
            .args
            .iter()
            .skip(1)
            .filter(|a| !matches!(a, Term::Assign(..)))
            .cloned()
            .collect();
        let mut outer = lp.clone();
        outer.args = std::iter::once(Term::Member(Box::new(trip), Box::new(span)))
            .chain(kw)
            .collect();
        outer.body = vec![inner];
        Ok(vec![outer])
    })?;
    write(p, &params, &out)
}
