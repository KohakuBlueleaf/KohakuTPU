//! Ranges read through divisions split into mixed-radix digits
//! (`_unfold`, `_affine`, `_build`).

use super::*;

/// `_quotient`: `(range, a)` when `u` is `r // a`.
fn quotient(g: &Graph, u: Id) -> Option<(Id, i64)> {
    if g.op(u) == "RANGE" {
        return fold::extent(g, u).map(|_| (u, 1));
    }
    if g.op(u) == "FLOORDIV" && g.op(g.src(u)[1]) == "CONST" {
        let inner = quotient(g, g.src(u)[0])?;
        let c = const_int(g, g.src(u)[1])?;
        return Some((inner.0, inner.1 * c));
    }
    None
}

pub(super) type Form = (Vec<(Id, i64)>, i64);

/// `_affine`: `u` as `(terms, constant)`, a split range as its digits.
fn affine_split(g: &Graph, u: Id, split: &[(Id, Vec<(Id, i64)>)]) -> Option<Form> {
    let found = |r: Id| split.iter().find(|(x, _)| *x == r).map(|(_, t)| t.clone());
    match g.op(u) {
        "CONST" => return const_int(g, u).map(|v| (Vec::new(), v)),
        "RANGE" => return Some(found(u).map(|t| (t, 0)).unwrap_or((vec![(u, 1)], 0))),
        "ADD" | "MUL" => {
            let a = affine_split(g, g.src(u)[0], split)?;
            let b = affine_split(g, g.src(u)[1], split)?;
            if g.op(u) == "ADD" {
                let mut terms = a.0;
                for (r, v) in b.0 {
                    match terms.iter_mut().find(|(x, _)| *x == r) {
                        Some(s) => s.1 += v,
                        None => terms.push((r, v)),
                    }
                }
                return Some((terms, a.1 + b.1));
            }
            if !a.0.is_empty() && !b.0.is_empty() {
                return None;
            }
            let ((terms, c0), k) = if !a.0.is_empty() { (a, b.1) } else { (b, a.1) };
            return Some((terms.into_iter().map(|(r, v)| (r, v * k)).collect(), c0 * k));
        }
        "FLOORDIV" | "FLOORMOD" => {}
        _ => return None,
    }
    let c = const_int(g, g.src(u)[1])?;
    let x = affine_split(g, g.src(u)[0], split)?;
    if c <= 0 {
        return None;
    }
    let (q, rem) = (x.1.div_euclid(c), x.1.rem_euclid(c));
    let high: Vec<(Id, i64)> =
        x.0.iter()
            .filter(|(_, v)| v % c == 0)
            .map(|(r, v)| (*r, v / c))
            .collect();
    let low: Vec<(Id, i64)> = x.0.iter().filter(|(_, v)| v % c != 0).cloned().collect();
    let lo = rem
        + low
            .iter()
            .map(|(r, v)| (v * (extent(g, *r) - 1)).min(0))
            .sum::<i64>();
    let hi = rem
        + low
            .iter()
            .map(|(r, v)| (v * (extent(g, *r) - 1)).max(0))
            .sum::<i64>();
    if lo < 0 || hi >= c {
        return None;
    }
    Some(if g.op(u) == "FLOORDIV" {
        (high, q)
    } else {
        (low, rem)
    })
}

/// `_build`: `sum(range * coefficient) + constant` as an index expression.
pub fn build(g: &mut Graph, form: &Form, like: Id) -> Id {
    let mut out: Option<Id> = None;
    for (r, v) in &form.0 {
        if *v == 0 {
            continue;
        }
        let t = if *v != 1 {
            g.binop_py("MUL", *r, &Arg::Int(*v))
        } else {
            *r
        };
        out = Some(match out {
            None => t,
            Some(o) => g.binop("ADD", o, t),
        });
    }
    match out {
        None => g.const_like(like, &Arg::Int(form.1)),
        Some(o) if form.1 != 0 => g.binop_py("ADD", o, &Arg::Int(form.1)),
        Some(o) => o,
    }
}

/// `_unfold`: a range read as `r // d` / `r % d` split into mixed-radix
/// digits, every division over them folded. `(ast, digits)`.
pub(super) fn unfold(g: &mut Graph, ast: Id) -> (Id, Vec<(Id, Vec<Id>)>) {
    if mutant::on("tg.lower/no_unfold") {
        return (ast, Vec::new());
    }
    let mut divs: Vec<(Id, Vec<i64>)> = Vec::new();
    for u in g.toposort(ast) {
        if !matches!(g.op(u), "FLOORDIV" | "FLOORMOD") || g.op(g.src(u)[1]) != "CONST" {
            continue;
        }
        let Some((r, a)) = quotient(g, g.src(u)[0]) else {
            continue;
        };
        let Some(c) = const_int(g, g.src(u)[1]) else {
            continue;
        };
        let at = match divs.iter().position(|(x, _)| *x == r) {
            Some(i) => i,
            None => {
                divs.push((r, Vec::new()));
                divs.len() - 1
            }
        };
        for d in [a, a * c] {
            if d > 1 && !divs[at].1.contains(&d) {
                divs[at].1.push(d);
            }
        }
    }
    let mut nxt = 1 + g
        .toposort(ast)
        .iter()
        .filter(|r| g.op(**r) == "RANGE")
        .map(|r| range_id(g, *r).0)
        .max()
        .unwrap_or(0);
    let mut split: Vec<(Id, Vec<(Id, i64)>)> = Vec::new();
    let mut digits: Vec<(Id, Vec<Id>)> = Vec::new();
    for (r, ds) in divs {
        let n = extent(g, r);
        let mut chain: Vec<i64> = ds.into_iter().filter(|d| 1 < *d && *d < n).collect();
        chain.sort();
        if chain.is_empty()
            || chain.iter().any(|d| n % d != 0)
            || chain.windows(2).any(|w| w[1] % w[0] != 0)
        {
            continue;
        }
        let axis = range_id(g, r).1;
        let mut pts = vec![1];
        pts.extend(chain);
        pts.push(n);
        let mut terms: Vec<(Id, i64)> = Vec::new();
        for w in pts.windows(2) {
            let (lo, hi) = (w[0], w[1]);
            let d = g.range(hi / lo, nxt, &axis);
            terms.insert(0, (d, lo));
            nxt += 1;
        }
        digits.push((r, terms.iter().map(|(d, _)| *d).collect()));
        split.push((r, terms));
    }
    if split.is_empty() {
        return (ast, Vec::new());
    }
    let digit_of = |s: Id| digits.iter().find(|(x, _)| *x == s).map(|(_, d)| d.clone());
    let mut reduces = HashMap::new();
    for u in g.toposort(ast) {
        if g.op(u) == "REDUCE" && g.src(u)[1..].iter().any(|s| digit_of(*s).is_some()) {
            let mut src = vec![g.src(u)[0]];
            for &s in &g.src(u)[1..] {
                match digit_of(s) {
                    Some(ds) => src.extend(ds),
                    None => src.push(s),
                }
            }
            let new = g.with_src(u, src);
            reduces.insert(u, new);
        }
    }
    let mut ast = ast;
    if !reduces.is_empty() {
        ast = g.substitute(ast, &reduces);
    }
    let mut folds: Vec<(Id, Id)> = Vec::new();
    for u in g.toposort(ast) {
        if matches!(g.op(u), "FLOORDIV" | "FLOORMOD")
            && g.ranges(u)
                .iter()
                .any(|r| split.iter().any(|(x, _)| x == r))
        {
            if let Some(form) = affine_split(g, u, &split) {
                let b = build(g, &form, u);
                folds.push((u, b));
            }
        }
    }
    let mut inner: Vec<Id> = Vec::new();
    for (u, _) in &folds {
        for v in g.toposort(*u) {
            if v != *u {
                inner.push(v);
            }
        }
    }
    let subs: HashMap<Id, Id> = folds
        .iter()
        .filter(|(u, _)| !inner.contains(u))
        .cloned()
        .collect();
    ast = g.substitute(ast, &subs);
    let mut bare = HashMap::new();
    for (r, t) in &split {
        let b = build(g, &(t.clone(), 0), *r);
        bare.insert(*r, b);
    }
    (g.substitute(ast, &bare), digits)
}
