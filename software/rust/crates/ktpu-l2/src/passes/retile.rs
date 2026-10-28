//! `retile` (L2.PASS.009-.010): each vector stream loop of R rows a trip
//! takes the row tile the core's timing gives the fewest cycles over all its
//! trips, of the multiples 1, 1/4, 1/2, 2, 4 of R and the whole bands
//! dividing R, kept only when 2% better than the best before it (the written
//! tile first). When none lowers, a stream whose ops work on each 32-column
//! GT4 block apart splits into the fewest equal pieces one of which lowers.

use std::collections::{BTreeSet, HashMap};

use ktpu_l1::text::{fmt, IntStyle, Stmt, Term};

use super::step::{first_of, scope_ok};
use super::{body_of, first_line, head, names, unit_of, walk, write};
use crate::ir::read::instances;
use crate::ir::{Program, Result, StageX};
use crate::lower::vector::{self, Search, BINARY, CMP, TERNARY, UNARY, XLANE};
use crate::target::Target;

/// Rows a trip the pass tries, as multiples of the written tile.
const RETILE: [f64; 5] = [1.0, 0.25, 0.5, 2.0, 4.0];

fn int_t(v: i64) -> Term {
    Term::Int(v as i128, IntStyle::Dec)
}

/// `par`'s one stream loop (inside `when`s the step pass wrote), or None.
fn loop_of(par: &Stmt) -> Option<Vec<usize>> {
    fn go(sts: &[Stmt], path: &mut Vec<usize>, out: &mut Vec<Vec<usize>>) {
        for (i, b) in sts.iter().enumerate().rev() {
            path.push(i);
            if b.op == "for" {
                out.push(path.clone());
            } else if b.op == "when" {
                go(&b.body, path, out);
            }
            path.pop();
        }
    }
    let mut out = Vec::new();
    go(&par.body, &mut Vec::new(), &mut out);
    if out.len() == 1 {
        out.pop()
    } else {
        None
    }
}

fn at_path<'a>(par: &'a Stmt, path: &[usize]) -> &'a Stmt {
    let mut s = &par.body[path[0]];
    for &i in &path[1..] {
        s = &s.body[i];
    }
    s
}

fn put_path(par: &Stmt, path: &[usize], new: Stmt) -> Stmt {
    let mut out = par.clone();
    if path.len() == 1 {
        out.body[path[0]] = new;
    } else {
        out.body[path[0]] = put_path(&par.body[path[0]], &path[1..], new);
    }
    out
}

/// The targets of `loop`'s ops reading neither its variable nor anything its
/// trips make: one value every trip.
fn invariant(lp: &Stmt) -> BTreeSet<String> {
    let var = head(lp).map(|h| h.0).unwrap_or_default();
    let mut varying: BTreeSet<String> = lp
        .body
        .iter()
        .filter_map(|b| b.target_name().map(str::to_string))
        .collect();
    varying.insert(var);
    let mut out = BTreeSet::new();
    if crate::mutant::on("l2.passes/retile-invariant-scaled") {
        return out;
    }
    for b in &lp.body {
        if let Some(t) = b.target_name() {
            if names(&b.args).is_disjoint(&varying) {
                out.insert(t.to_string());
                varying.remove(t);
            }
        }
    }
    out
}

/// R when every op of the loop but the invariant ones has a leading
/// dimension of R (or a statistic of R rows).
fn stream_rows(lp: &Stmt) -> Option<i64> {
    let mut rows: BTreeSet<i64> = BTreeSet::new();
    let fixed = invariant(lp);
    for st in &lp.body {
        if !st.body.is_empty() {
            return None;
        }
        if st.target_name().map(|t| fixed.contains(t)).unwrap_or(false) {
            continue;
        }
        if let Some(Term::Placed(x, _)) = &st.annot {
            if let Term::View(_, dims) = x.as_ref() {
                if let Some(Term::Int(v, _)) = dims.first() {
                    rows.insert(*v as i64);
                }
            }
        }
    }
    rows.remove(&1);
    if rows.len() != 1 {
        return None;
    }
    rows.into_iter().next()
}

/// Bound `x` times rows / new.
fn times(x: &Term, rows: i64, new: i64) -> Term {
    if crate::mutant::on("l2.passes/retile-bounds-unscaled") {
        return x.clone();
    }
    match x {
        Term::Int(v, _) => int_t(*v as i64 * rows / new),
        _ => Term::BinOp('*', Box::new(x.clone()), Box::new(int_t(rows / new))),
    }
}

fn scales(lo: &Term, hi: &Term, rows: i64, new: i64) -> bool {
    if rows % new == 0 {
        return true;
    }
    [lo, hi]
        .iter()
        .all(|x| matches!(x, Term::Int(v, _) if (*v as i64 * rows) % new == 0))
}

/// `loop` with R rows a trip made `new`.
#[doc(hidden)]
pub fn scaled(lp: &Stmt, var: &str, rows: i64, new: i64) -> Stmt {
    fn term(t: &Term, var: &str, rows: i64, new: i64) -> Term {
        let n = Term::name(var);
        if let Term::BinOp('*', a, b) = t {
            if **a == n || **b == n {
                let k = if **a == n { b } else { a };
                if let Term::Int(kv, _) = k.as_ref() {
                    if *kv as i64 == rows {
                        return Term::BinOp('*', Box::new(n), Box::new(int_t(new)));
                    }
                }
            }
        }
        if let Term::Pos(x) = t {
            if **x == int_t(rows) {
                return Term::Pos(Box::new(int_t(new)));
            }
        }
        let r = |x: &Term| Box::new(term(x, var, rows, new));
        match t {
            Term::BinOp(op, a, b) => Term::BinOp(*op, r(a), r(b)),
            Term::Offset(a, b) => Term::Offset(r(a), r(b)),
            Term::Slice(lo, hi) => {
                Term::Slice(lo.as_ref().map(|x| r(x)), hi.as_ref().map(|x| r(x)))
            }
            Term::View(n, sl) if !sl.is_empty() => {
                let all = crate::mutant::on("l2.passes/retile-columns-scaled");
                let s = sl
                    .iter()
                    .enumerate()
                    .map(|(k, x)| match k == 0 || all {
                        true => term(x, var, rows, new),
                        false => x.clone(),
                    })
                    .collect();
                Term::View(n.clone(), s)
            }
            other => other.clone(),
        }
    }
    let fixed = invariant(lp);
    let mut body = Vec::new();
    for st in &lp.body {
        if st.target_name().map(|t| fixed.contains(t)).unwrap_or(false) {
            body.push(st.clone());
            continue;
        }
        let mut s = st.clone();
        s.args = st
            .args
            .iter()
            .map(|a| {
                if matches!(a, Term::Assign(..)) {
                    a.clone()
                } else {
                    term(a, var, rows, new)
                }
            })
            .collect();
        if let Some(Term::Placed(x, sp)) = &st.annot {
            if let Term::View(n, dims) = x.as_ref() {
                let mut d = dims.clone();
                let all = crate::mutant::on("l2.passes/retile-columns-scaled");
                for (k, x) in d.iter_mut().enumerate() {
                    if (k == 0 || all) && *x == int_t(rows) {
                        *x = int_t(new);
                    }
                }
                s.annot = Some(Term::Placed(Box::new(Term::View(n.clone(), d)), sp.clone()));
            }
        }
        body.push(s);
    }
    let Some((_, lo, hi)) = head(lp) else {
        return lp.clone();
    };
    let h = Term::Member(
        Box::new(Term::name(var)),
        Box::new(Term::Span(
            Box::new(times(&lo, rows, new)),
            Box::new(times(&hi, rows, new)),
        )),
    );
    let mut out = lp.clone();
    out.args = std::iter::once(h)
        .chain(lp.args.iter().skip(1).cloned())
        .collect();
    out.body = body;
    out
}

/// `par` with its stream loop at `new` rows a trip.
fn tiled(par: &Stmt, path: &[usize], rows: i64, new: i64) -> Stmt {
    if new == rows {
        return par.clone();
    }
    let lp = at_path(par, path);
    let var = head(lp).map(|h| h.0).unwrap_or_default();
    put_path(par, path, scaled(lp, &var, rows, new))
}

/// `(cycles, None)` of the first instance of the one vector `par` in `stmts`,
/// or `(None, why)` where it does not lower.
fn lowered_cost(
    p: &Program,
    params: &[(String, Term)],
    stmts: &[Stmt],
    t: &Target,
    search: &Search,
) -> (Option<i64>, String) {
    let got = (|| -> std::result::Result<i64, String> {
        let body = write(p, params, stmts)
            .map_err(|e| first_line(&e))?
            .body()
            .map_err(|e| first_line(&e))?;
        let stages = instances(&body).map_err(|e| first_line(&e))?;
        let x = stages
            .into_iter()
            .find_map(|s| match s.x {
                StageX::Par(x) => Some(x),
                _ => None,
            })
            .ok_or_else(|| "StopIteration".to_string())?;
        vector::total(&body, x[0].par(), &x[0].env, t, search).map_err(|e| first_line(&e))
    })();
    match got {
        Ok(c) => (Some(c), String::new()),
        Err(w) => (None, w),
    }
}

fn is_columnwise(op: &str) -> bool {
    [&UNARY[..], &BINARY[..], &TERNARY[..], &CMP[..], &XLANE[..]]
        .iter()
        .any(|t| t.iter().any(|(k, _)| *k == op))
        || ["sel", "load", "unpack", "pack", "store"].contains(&op)
}

/// `par`'s ops, its stream loop's (and `when`s') included.
fn ops_of(par: &Stmt) -> Vec<&Stmt> {
    let mut out = Vec::new();
    for b in &par.body {
        if b.op == "for" || b.op == "when" {
            out.extend(ops_of(b));
        } else {
            out.push(b);
        }
    }
    out
}

fn ty_view(t: &Term) -> Option<(&String, &Vec<Term>)> {
    match t {
        Term::Placed(x, _) => match x.as_ref() {
            Term::View(n, d) => Some((n, d)),
            _ => None,
        },
        _ => None,
    }
}

fn stored(st: &Stmt) -> Option<&str> {
    let pos = st.positional();
    if pos.len() == 3 {
        pos[2].as_name()
    } else {
        None
    }
}

/// W: the columns every GT4 value of `par` but its 32-wide statistics
/// spans, when each op works on each column apart; else 0.
fn width(par: &Stmt, shapes: &HashMap<String, Vec<Term>>) -> i64 {
    let ops = ops_of(par);
    let types: HashMap<&str, &Term> = ops
        .iter()
        .filter_map(|b| match (&b.annot, b.target_name()) {
            (Some(a @ Term::Placed(..)), Some(t)) => Some((t, a)),
            _ => None,
        })
        .collect();
    let mut widths: BTreeSet<i64> = BTreeSet::new();
    let mut views: Vec<&Term> = Vec::new();
    for b in &ops {
        if !is_columnwise(&b.op) || !b.body.is_empty() {
            return 0;
        }
        let tt = if b.op != "store" {
            b.annot.as_ref()
        } else {
            stored(b).and_then(|n| types.get(n).copied())
        };
        let Some(tt @ Term::Placed(..)) = tt else {
            return 0;
        };
        let (dt, dims) = ty_view(tt).unwrap();
        let Some(Term::Int(w, _)) = dims.last() else {
            continue;
        };
        let w = *w as i64;
        if w == 32 {
            continue;
        }
        if !fmt(tt).contains("gt4") || (dt != "f16" && dt != "f32") {
            return 0;
        }
        widths.insert(w);
        if b.op == "load" || b.op == "store" {
            views.push(&b.args[0]);
        }
    }
    if widths.len() != 1 {
        return 0;
    }
    let w = *widths.iter().next().unwrap();
    for v in views {
        let (name, sl) = match v {
            Term::View(n, s) => (n.clone(), s.clone()),
            Term::Name(n) => (n.clone(), vec![]),
            _ => return 0,
        };
        let nd = shapes.get(&name).map(|d| d.len()).unwrap_or(0);
        if sl.len() + 1 == nd {
            continue;
        }
        let last = if sl.len() == nd { sl.last() } else { None };
        match last {
            Some(Term::Slice(_, Some(hi))) if **hi == Term::Pos(Box::new(int_t(w))) => {}
            _ => return 0,
        }
    }
    w
}

/// `par` over columns `j w .. (j + 1) w` of its W-wide values, renamed apart.
fn piece(par: &Stmt, wide_w: i64, w: i64, j: i64, shapes: &HashMap<String, Vec<Term>>) -> Stmt {
    let ops = ops_of(par);
    let made: BTreeSet<String> = ops
        .iter()
        .filter_map(|b| b.target_name().map(str::to_string))
        .collect();
    let types: HashMap<String, Term> = ops
        .iter()
        .filter_map(|b| match (&b.annot, b.target_name()) {
            (Some(a @ Term::Placed(..)), Some(t)) => Some((t.to_string(), a.clone())),
            _ => None,
        })
        .collect();
    fn ren(x: &Term, made: &BTreeSet<String>, j: i64) -> Term {
        let r = |y: &Term| Box::new(ren(y, made, j));
        let v = |ys: &[Term]| ys.iter().map(|y| ren(y, made, j)).collect::<Vec<_>>();
        match x {
            Term::Name(n) if made.contains(n) => Term::Name(format!("{n}_{j}")),
            Term::View(n, sl) if made.contains(n) => Term::View(format!("{n}_{j}"), v(sl)),
            Term::Placed(..) => x.clone(),
            Term::Neg(a) => Term::Neg(r(a)),
            Term::Not(a) => Term::Not(r(a)),
            Term::Pos(a) => Term::Pos(r(a)),
            Term::Offset(a, b) => Term::Offset(r(a), r(b)),
            Term::BinOp(op, a, b) => Term::BinOp(*op, r(a), r(b)),
            Term::Compare(op, a, b) => Term::Compare(op.clone(), r(a), r(b)),
            Term::Computed(p, i, sl) => Term::Computed(p.clone(), r(i), sl.as_ref().map(|s| v(s))),
            Term::AddAssign(a, b) => Term::AddAssign(r(a), r(b)),
            Term::Span(a, b) => Term::Span(r(a), r(b)),
            Term::Typed(n, a) => Term::Typed(n.clone(), r(a)),
            Term::Kw(n, a) => Term::Kw(n.clone(), r(a)),
            Term::Call(n, a) => Term::Call(n.clone(), v(a)),
            Term::Slice(lo, hi) => {
                Term::Slice(lo.as_ref().map(|y| r(y)), hi.as_ref().map(|y| r(y)))
            }
            Term::View(n, a) => Term::View(n.clone(), v(a)),
            Term::Tuple(a) => Term::Tuple(v(a)),
            Term::Assign(a, b) => Term::Assign(r(a), r(b)),
            Term::Member(a, b) => Term::Member(r(a), r(b)),
            other => other.clone(),
        }
    }
    let run = |v: &Term| -> Term {
        let (name, sl) = match v {
            Term::View(n, s) => (n.clone(), s.clone()),
            Term::Name(n) => (n.clone(), vec![]),
            other => return other.clone(),
        };
        let nd = shapes.get(&name).map(|d| d.len()).unwrap_or(0);
        if sl.len() < nd {
            let mut s = sl.clone();
            s.push(Term::Slice(
                Some(Box::new(int_t(j * w))),
                Some(Box::new(Term::Pos(Box::new(int_t(w))))),
            ));
            return Term::View(name, s);
        }
        let (rest, last) = sl.split_at(sl.len() - 1);
        let lo = match &last[0] {
            Term::Slice(Some(lo), _) => (**lo).clone(),
            _ => Term::name("None"),
        };
        let lo = match lo {
            Term::Int(v, _) => int_t(v as i64 + j * w),
            other => Term::BinOp('+', Box::new(other), Box::new(int_t(j * w))),
        };
        let mut s = rest.to_vec();
        s.push(Term::Slice(
            Some(Box::new(lo)),
            Some(Box::new(Term::Pos(Box::new(int_t(w))))),
        ));
        Term::View(name, s)
    };
    let wide = |t: Option<&Term>| -> bool {
        t.and_then(ty_view)
            .map(|(_, d)| d.last() == Some(&int_t(wide_w)))
            .unwrap_or(false)
    };
    fn op(
        b: &Stmt,
        made: &BTreeSet<String>,
        types: &HashMap<String, Term>,
        j: i64,
        w: i64,
        wide: &dyn Fn(Option<&Term>) -> bool,
        run: &dyn Fn(&Term) -> Term,
    ) -> Stmt {
        if b.op == "for" || b.op == "when" {
            let mut out = b.clone();
            out.body = b
                .body
                .iter()
                .map(|x| op(x, made, types, j, w, wide, run))
                .collect();
            return out;
        }
        let mut args = b.args.clone();
        let mut annot = b.annot.clone();
        let ty = if b.op != "store" {
            b.annot.as_ref()
        } else {
            stored(b).and_then(|n| types.get(n))
        };
        if wide(ty) {
            if b.op == "load" || b.op == "store" {
                args[0] = run(&args[0]);
            }
            if b.op != "store" {
                if let Some(Term::Placed(x, sp)) = &annot {
                    if let Term::View(n, d) = x.as_ref() {
                        let mut d = d.clone();
                        let k = d.len() - 1;
                        d[k] = int_t(w);
                        annot = Some(Term::Placed(Box::new(Term::View(n.clone(), d)), sp.clone()));
                    }
                }
            }
        }
        let mut out = b.clone();
        out.target = b.target_name().map(|t| Term::name(format!("{t}_{j}")));
        out.args = args
            .iter()
            .enumerate()
            .map(|(k, a)| {
                if k == 0 && (b.op == "load" || b.op == "store") {
                    a.clone()
                } else {
                    ren(a, made, j)
                }
            })
            .collect();
        out.annot = annot;
        out
    }
    let mut out = par.clone();
    out.body = par
        .body
        .iter()
        .map(|b| op(b, &made, &types, j, w, &wide, &run))
        .collect();
    out
}

pub fn retile(p: &Program, t: &Target, search: &Search) -> Result<Program> {
    let (params, stmts) = body_of(p)?;
    let r = p.module.reader();
    let top: Vec<Stmt> = stmts.iter().filter(|s| s.op == "buffer").cloned().collect();
    let mut shapes: HashMap<String, Vec<Term>> = HashMap::new();
    for (n, ty) in &params {
        if let Some((_, d)) = ty_view(ty) {
            shapes.insert(n.clone(), d.clone());
        }
    }
    for s in &top {
        if let (Some(tn), Some(a)) = (s.target_name(), &s.annot) {
            if let Some((_, d)) = ty_view(a) {
                shapes.insert(tn.to_string(), d.clone());
            }
        }
    }
    // `(par at the fastest tile, rows) ` or `(None, the written tile's why)`.
    let tiled_best =
        |s: &Stmt, scopes: &[Stmt], path: &[usize], rows: i64| -> (Option<(Stmt, i64)>, String) {
            let lp = at_path(s, path);
            let (lo, hi) = head(lp)
                .map(|h| (h.1, h.2))
                .unwrap_or((Term::int(0), Term::int(0)));
            let gt4 = lp.body.iter().any(|b| {
                b.annot
                    .as_ref()
                    .map(|a| fmt(a).contains("gt4"))
                    .unwrap_or(false)
            });
            let q = if gt4 { 4 } else { 1 };
            let mut tiles: Vec<f64> = RETILE.iter().map(|m| rows as f64 * m).collect();
            let mut n = rows - q;
            while n > 0 {
                if rows % n == 0 {
                    tiles.push(n as f64);
                }
                n -= q;
            }
            let mut seen: Vec<f64> = Vec::new();
            let (mut best, mut cost, mut why): (Option<(Stmt, i64)>, Option<i64>, String) =
                (None, None, String::new());
            let mut why_set = false;
            for new in tiles {
                if seen.contains(&new) {
                    continue;
                }
                seen.push(new);
                if new != new.trunc() || (new as i64) % q != 0 {
                    continue;
                }
                let new = new as i64;
                if !scales(&lo, &hi, rows, new) {
                    continue;
                }
                let cand = tiled(s, path, rows, new);
                let mut probe = top.clone();
                probe.push(first_of(scopes, cand.clone()));
                let (got, err) = lowered_cost(p, &params, &probe, t, search);
                if new == rows {
                    why = err;
                    why_set = true;
                }
                if let Some(g) = got {
                    if cost.map(|c| (g as f64) < c as f64 * 0.98).unwrap_or(true) {
                        best = Some((cand, new));
                        cost = Some(g);
                    }
                }
            }
            if !why_set {
                why = "None".into();
            }
            (best, why)
        };
    let mut ordinal = 0;
    let out = walk(&stmts, &mut |s: Stmt, scopes: &[Stmt]| {
        if s.op != "par" || unit_of(&s).as_deref() != Some("vc") || !scopes.iter().all(scope_ok) {
            return Ok(vec![s]);
        }
        if s.kw("classed").is_some() {
            return Ok(vec![s]);
        }
        let Some(path) = loop_of(&s) else {
            return Ok(vec![s]);
        };
        let Some(rows) = stream_rows(at_path(&s, &path)) else {
            return Ok(vec![s]);
        };
        let index = ordinal;
        ordinal += 1;
        if search.frozen() {
            let fail = |message: &str| ktpu_l1::Error::new("L2.TEMPLATE.CHOICE", message);
            let (columns, new) = search
                .retile_choice(index)
                .ok_or_else(|| fail("no representative retile choice; split the class"))?;
            let lp = at_path(&s, &path);
            let (_, lo, hi) = head(lp).ok_or_else(|| fail("pinned loop has no bounds"))?;
            if columns < 1 || new < 1 || !scales(&lo, &hi, rows, new) {
                return Err(fail("representative row tile does not divide this loop"));
            }
            if columns == 1 {
                return Ok(vec![tiled(&s, &path, rows, new)]);
            }
            let wd = width(&s, &shapes);
            if wd <= 0 || wd % (32 * columns) != 0 {
                return Err(fail(
                    "representative column split does not divide this width",
                ));
            }
            return (0..columns)
                .map(|j| {
                    let pc = piece(&s, wd, wd / columns, j, &shapes);
                    let pp = loop_of(&pc).ok_or_else(|| fail("pinned column piece has no loop"))?;
                    Ok(tiled(&pc, &pp, rows, new))
                })
                .collect();
        }
        let (best, why) = tiled_best(&s, scopes, &path, rows);
        if let Some((b, new)) = best {
            search.remember_retile(index, 1, new);
            return Ok(vec![b]);
        }
        let wd = match crate::mutant::on("l2.passes/retile-no-columns") {
            true => 0,
            false => width(&s, &shapes),
        };
        let ks: Vec<i64> = (2..=wd / 32).filter(|k| wd % (32 * k) == 0).collect();
        let (mut lo, mut hi) = (0usize, ks.len());
        let mut found: Option<Vec<Stmt>> = None;
        while lo < hi {
            let mid = (lo + hi) / 2;
            let k = ks[mid];
            let pieces: Vec<Stmt> = (0..k).map(|j| piece(&s, wd, wd / k, j, &shapes)).collect();
            let Some(ppath) = loop_of(&pieces[0]) else {
                lo = mid + 1;
                continue;
            };
            let (got, _) = tiled_best(&pieces[0], scopes, &ppath, rows);
            let Some((g0, new)) = got else {
                lo = mid + 1;
                continue;
            };
            let mut all = vec![g0];
            for pc in &pieces[1..] {
                let pp = loop_of(pc).unwrap();
                all.push(tiled(pc, &pp, rows, new));
            }
            hi = mid;
            found = Some(all);
            search.remember_retile(index, k, new);
        }
        match found {
            Some(f) => Ok(f),
            None if crate::mutant::on("l2.passes/retile-keeps-unlowered") => Ok(vec![s]),
            None => {
                let lp = at_path(&s, &path);
                Err(r.fail(
                    lp,
                    "L2.PASS.010",
                    format!("no row tile of the stream lowers: {why}"),
                ))
            }
        }
    })?;
    if search.frozen() && ordinal != search.retile_count() {
        return Err(ktpu_l1::Error::new(
            "L2.TEMPLATE.CHOICE",
            "retile loop count differs from representative",
        ));
    }
    write(p, &params, &out)
}
