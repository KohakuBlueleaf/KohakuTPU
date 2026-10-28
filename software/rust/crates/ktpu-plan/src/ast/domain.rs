//! Indices made affine on the domain where they are read
//! (`ktpugrad/domain.py`): a sum whose support lets through one point of
//! some reduced ranges loses them (`collapse`), a read's index is replaced
//! by the affine index equal to it where its gate lets it through (`fit`),
//! a sum of a sum is one sum (`merge`). Either may split a range into
//! mixed-radix digits first. Every form is checked point for point.

use std::collections::{HashMap, HashSet};

use crate::fold::{self, Arr};
use crate::lower::{affine, build, extent, gated, param_of};
use crate::uop::{dtype, Arg, Graph, Id};

/// Points an index-only expression is evaluated over, at most.
pub const MAX_POINTS: usize = 1 << 21;

pub type Digits = Vec<(Id, Vec<Id>)>;

/// `normalise`: one-point reductions collapsed, every read's index fitted,
/// nested sums merged; `digits` extended by the splits made.
pub fn normalise(g: &mut Graph, ast: Id, digits: Digits) -> (Id, Digits) {
    let ast = unmasked(g, ast);
    let (ast, digits) = collapse(g, ast, digits);
    let (ast, digits) = fit(g, ast, digits);
    let ast = unmasked(g, ast);
    (merge(g, ast), digits)
}

fn is_sum(g: &Graph, u: Id) -> bool {
    g.op(u) == "REDUCE" && matches!(g.arg(u), Arg::Reduce { op, .. } if op == "ADD")
}

fn peel(g: &Graph, mut u: Id) -> Id {
    while g.op(u) == "CAST" && dtype::is_float(g.dtype(u)) && dtype::is_float(g.dtype(g.src(u)[0]))
    {
        u = g.src(u)[0];
    }
    u
}

fn is_zero(g: &Graph, u: Id) -> bool {
    g.op(u) == "CONST"
        && match g.arg(u) {
            Arg::Invalid => true,
            Arg::Int(v) => *v == 0,
            Arg::Bool(b) => !*b,
            Arg::Float(f) => f64::from_bits(*f) == 0.0,
            _ => false,
        }
}

fn index_only(g: &Graph, u: Id) -> bool {
    fold::index_only(g, u, &mut HashMap::new())
}

/// `_unmasked`: a sum's summand masked Invalid adds nothing: that arm a 0.
fn unmasked(g: &mut Graph, ast: Id) -> Id {
    let mut subs = HashMap::new();
    for red in g.toposort(ast) {
        if !is_sum(g, red) {
            continue;
        }
        let mut x = g.src(red)[0];
        while g.op(x) == "CAST" {
            x = g.src(x)[0];
        }
        if g.op(x) == "WHERE"
            && dtype::is_float(g.dtype(x))
            && g.src(x)[1..].iter().any(|a| g.is_invalid(*a))
        {
            let src = g.src(x).to_vec();
            let mut arms = vec![src[0]];
            for a in &src[1..] {
                arms.push(if g.is_invalid(*a) {
                    g.const_like(*a, &Arg::Int(0))
                } else {
                    *a
                });
            }
            let new = g.with_src(x, arms);
            subs.insert(x, new);
        }
    }
    if subs.is_empty() {
        ast
    } else {
        g.substitute(ast, &subs)
    }
}

/// `merge`: a sum of a sum (under zero-else index `where`s and float
/// casts) as one sum over both range sets.
fn merge(g: &mut Graph, mut ast: Id) -> Id {
    'outer: loop {
        for u in g.toposort(ast) {
            if !is_sum(g, u) {
                continue;
            }
            let mut x = peel(g, g.src(u)[0]);
            let mut on: Option<Id> = None;
            while g.op(x) == "WHERE" && index_only(g, g.src(x)[0]) && is_zero(g, g.src(x)[2]) {
                let c = g.src(x)[0];
                on = Some(match on {
                    None => c,
                    Some(o) => g.binop("AND", o, c),
                });
                x = peel(g, g.src(x)[1]);
            }
            if !is_sum(g, x) {
                continue;
            }
            let mut body = g.src(x)[0];
            let inner = g.src(x)[1..].to_vec();
            if let Some(o) = on {
                let z = g.const_like(body, &Arg::Int(0));
                body = g.where_(o, body, z);
            }
            let want = g.dtype(g.src(u)[0]).to_string();
            if g.dtype(body) != want {
                body = g.cast(body, &want);
            }
            let mut src = vec![body];
            src.extend(g.src(u)[1..].to_vec());
            src.extend(inner);
            let new = g.with_src(u, src);
            ast = g.substitute(ast, &HashMap::from([(u, new)]));
            continue 'outer;
        }
        return ast;
    }
}

// --------------------------------------------------------------- helpers
fn range_id(g: &Graph, r: Id) -> i64 {
    match g.arg(r) {
        Arg::Range { id, .. } => *id,
        _ => 0,
    }
}

fn axis(g: &Graph, r: Id) -> String {
    match g.arg(r) {
        Arg::Range { axis, .. } => axis.clone(),
        _ => "WEAK".into(),
    }
}

/// `_ranges`: the ranges `u` reads, by axis id.
fn ranges(g: &Graph, u: Id) -> Vec<Id> {
    let mut out: Vec<Id> = g
        .toposort(u)
        .into_iter()
        .filter(|x| g.op(*x) == "RANGE")
        .collect();
    out.sort_by_key(|r| range_id(g, *r));
    out.dedup();
    out
}

fn and(g: &mut Graph, a: Option<Id>, b: Option<Id>) -> Option<Id> {
    match (a, b) {
        (None, b) => b,
        (a, None) => a,
        (Some(a), Some(b)) => Some(g.binop("AND", a, b)),
    }
}

/// `support`: an index-only condition outside which `u` is zero.
pub fn support(g: &mut Graph, u: Id) -> Option<Id> {
    match g.op(u) {
        "CAST" => support(g, g.src(u)[0]),
        "MUL" => {
            let (a, b) = (g.src(u)[0], g.src(u)[1]);
            let sa = support(g, a);
            let sb = support(g, b);
            and(g, sa, sb)
        }
        "WHERE" if index_only(g, g.src(u)[0]) && is_zero(g, g.src(u)[2]) => {
            let c = g.src(u)[0];
            let s = support(g, g.src(u)[1]);
            and(g, Some(c), s)
        }
        "INDEX" if g.src(u).len() > 1 => gated(g, g.src(u)[1]).map(|(gt, _)| gt),
        _ => None,
    }
}

/// `_radices`: divisors of `r`'s extent that `u` divides or reduces by.
fn radices(g: &Graph, u: Id, r: Id) -> Vec<i64> {
    let n = extent(g, r);
    let mut out: Vec<i64> = Vec::new();
    for x in g.toposort(u) {
        if matches!(g.op(x), "FLOORDIV" | "FLOORMOD") && g.op(g.src(x)[1]) == "CONST" {
            if let Arg::Int(c) = g.arg(g.src(x)[1]) {
                if 1 < *c && *c < n && n % c == 0 && !out.contains(c) {
                    out.push(*c);
                }
            }
        }
    }
    out.sort();
    out
}

/// `_split`: `r` as two digits `hi * c + lo` everywhere in `ast`.
fn split(g: &mut Graph, ast: Id, digits: &Digits, r: Id, c: i64) -> (Id, Digits, (Id, Id)) {
    let nxt = 1 + g
        .toposort(ast)
        .iter()
        .filter(|x| g.op(**x) == "RANGE")
        .map(|x| range_id(g, *x))
        .max()
        .unwrap_or(0);
    let ax = axis(g, r);
    let hi = g.range(extent(g, r) / c, nxt, &ax);
    let lo = g.range(c, nxt + 1, &ax);
    let mut reds = HashMap::new();
    for u in g.toposort(ast) {
        if g.op(u) == "REDUCE" && g.src(u)[1..].contains(&r) {
            let mut src = vec![g.src(u)[0]];
            for &s in &g.src(u)[1..] {
                if s == r {
                    src.extend([hi, lo]);
                } else {
                    src.push(s);
                }
            }
            let new = g.with_src(u, src);
            reds.insert(u, new);
        }
    }
    let mut ast = ast;
    if !reds.is_empty() {
        ast = g.substitute(ast, &reds);
    }
    let m = g.binop_py("MUL", hi, &Arg::Int(c));
    let to = g.binop("ADD", m, lo);
    ast = g.substitute(ast, &HashMap::from([(r, to)]));
    let mut new: Digits = digits
        .iter()
        .map(|(k, ds)| {
            (
                *k,
                ds.iter()
                    .flat_map(|d| if *d == r { vec![hi, lo] } else { vec![*d] })
                    .collect(),
            )
        })
        .collect();
    if !digits.iter().any(|(_, ds)| ds.contains(&r)) {
        new.push((r, vec![hi, lo]));
    }
    (ast, new, (hi, lo))
}

/// `_grid`: each of `us` over `ranges` (full arrays, flattened), or None.
fn grid(g: &Graph, us: &[Id], rs: &[Id]) -> Option<Vec<Arr>> {
    let shape: Vec<usize> = rs.iter().map(|r| extent(g, *r) as usize).collect();
    if shape.iter().product::<usize>() > MAX_POINTS {
        return None;
    }
    let mut memo = HashMap::new();
    let mut out = Vec::new();
    for u in us {
        let a = fold::evaluate(g, *u, rs, &mut memo).ok()?;
        fold::broadcast_shape(&a.shape, &shape)?;
        out.push(a.broadcast(&shape));
    }
    Some(out)
}

/// `_coords`: every point of `ranges`, row-major, one column a range.
fn coords(g: &Graph, rs: &[Id]) -> Vec<Vec<i64>> {
    let ext: Vec<i64> = rs.iter().map(|r| extent(g, *r)).collect();
    let n: i64 = ext.iter().product();
    let mut out = Vec::with_capacity(n as usize);
    let mut idx = vec![0i64; rs.len()];
    for _ in 0..n {
        out.push(idx.clone());
        for d in (0..rs.len()).rev() {
            idx[d] += 1;
            if idx[d] < ext[d] {
                break;
            }
            idx[d] = 0;
        }
    }
    if rs.is_empty() {
        return vec![Vec::new()];
    }
    out
}

/// The rank of the rows of `a` (exact, fraction-free elimination).
fn rank(rows: &[Vec<i128>]) -> usize {
    let mut basis: Vec<Vec<i128>> = Vec::new();
    for r in rows {
        if let Some(v) = reduce_row(&basis, r.clone()) {
            basis.push(v);
            if basis.len() == r.len() {
                break;
            }
        }
    }
    basis.len()
}

fn gcd(a: i128, b: i128) -> i128 {
    if b == 0 {
        a.abs()
    } else {
        gcd(b, a % b)
    }
}

/// `v` reduced against an echelon `basis`; None when it is in its span.
fn reduce_row(basis: &[Vec<i128>], mut v: Vec<i128>) -> Option<Vec<i128>> {
    for b in basis {
        let p = b.iter().position(|x| *x != 0).unwrap();
        if v[p] != 0 {
            let (bp, vp) = (b[p], v[p]);
            for i in 0..v.len() {
                v[i] = v[i] * bp - b[i] * vp;
            }
            let g = v.iter().fold(0, |acc, x| gcd(acc, *x));
            if g > 1 {
                v.iter_mut().for_each(|x| *x /= g);
            }
        }
    }
    let p = v.iter().position(|x| *x != 0)?;
    let mut out = v;
    if out[p] < 0 {
        out.iter_mut().for_each(|x| *x = -*x);
    }
    let _ = p;
    Some(out)
}

/// `_affine_fit`: integer `(coefficients, constant)` with `x @ c + c0 ==
/// y` on every row, or None. Columns equal on these rows share one weight:
/// independent ones solved, the constant first, the rest weight 0.
fn affine_fit(x: &[Vec<i64>], y: &[i128]) -> Option<(Vec<i64>, i64)> {
    let cols = x.first().map(|r| r.len()).unwrap_or(0);
    if y.is_empty() {
        return Some((vec![0; cols], 0));
    }
    let a: Vec<Vec<i128>> = x
        .iter()
        .map(|r| {
            let mut v: Vec<i128> = r.iter().map(|c| *c as i128).collect();
            v.push(1);
            v
        })
        .collect();
    let mut keep: Vec<usize> = Vec::new();
    let order: Vec<usize> = std::iter::once(cols).chain(0..cols).collect();
    for j in order {
        let mut want = keep.clone();
        want.push(j);
        let sub: Vec<Vec<i128>> = a
            .iter()
            .map(|r| want.iter().map(|c| r[*c]).collect())
            .collect();
        if rank(&sub) > keep.len() {
            keep.push(j);
        }
    }
    // [a_keep | y]: an echelon basis, then back substitution.
    let mut basis: Vec<Vec<i128>> = Vec::new();
    for (r, yv) in a.iter().zip(y) {
        let mut v: Vec<i128> = keep.iter().map(|c| r[*c]).collect();
        v.push(*yv);
        if let Some(b) = reduce_row(&basis, v) {
            basis.push(b);
            if basis.len() > keep.len() {
                break;
            }
        }
    }
    let k = keep.len();
    // A pivot in the y column: inconsistent.
    if basis
        .iter()
        .any(|b| b.iter().position(|x| *x != 0) == Some(k))
    {
        return None;
    }
    let mut sol = vec![(0i128, 1i128); k];
    let mut rows: Vec<&Vec<i128>> = basis.iter().collect();
    rows.sort_by_key(|b| std::cmp::Reverse(b.iter().position(|x| *x != 0).unwrap()));
    for b in rows {
        let p = b.iter().position(|x| *x != 0).unwrap();
        // b[p] * s_p + sum(b[j] s_j, j > p) = b[k]
        let (mut num, mut den) = (b[k], 1i128);
        for j in p + 1..k {
            let (sn, sd) = sol[j];
            num = num * sd - b[j] * sn * den;
            den *= sd;
            let g = gcd(num, den);
            if g > 1 {
                num /= g;
                den /= g;
            }
        }
        den *= b[p];
        if den < 0 {
            num = -num;
            den = -den;
        }
        let g = gcd(num, den).max(1);
        sol[p] = (num / g, den / g);
    }
    let mut c = vec![0i64; cols + 1];
    for (i, j) in keep.iter().enumerate() {
        let (n, d) = sol[i];
        c[*j] = (n as f64 / d as f64).round() as i64;
    }
    for (r, yv) in a.iter().zip(y) {
        let v: i128 = r.iter().zip(&c).map(|(x, w)| x * *w as i128).sum();
        if v != *yv {
            return None;
        }
    }
    let c0 = c.pop().unwrap();
    Some((c, c0))
}

fn build_on(g: &mut Graph, rs: &[Id], coef: &[i64], c0: i64, like: Id) -> Id {
    let terms: Vec<(Id, i64)> = rs.iter().copied().zip(coef.iter().copied()).collect();
    build(g, &(terms, c0), like)
}

type Solve<'a, T> = &'a mut dyn FnMut(&mut Graph, &[Id], &[Id]) -> Option<T>;

/// `_with_splits`: `solve(exprs, ranges)` as they are, else after splitting
/// one of `ranges` (then each at its first radix) where `exprs` divide.
#[allow(clippy::type_complexity)]
fn with_splits<T>(
    g: &mut Graph,
    ast: Id,
    digits: &Digits,
    exprs: &[Id],
    rs: &[Id],
    solve: Solve<T>,
) -> Option<(Id, Digits, Vec<Id>, Vec<Id>, T, Vec<(Id, Id)>)> {
    if let Some(got) = solve(g, exprs, rs) {
        return Some((
            ast,
            digits.clone(),
            exprs.to_vec(),
            rs.to_vec(),
            got,
            Vec::new(),
        ));
    }
    let mut cands: Vec<(Id, i64)> = Vec::new();
    for r in rs {
        for e in exprs {
            for c in radices(g, *e, *r) {
                if !cands.contains(&(*r, c)) {
                    cands.push((*r, c));
                }
            }
        }
    }
    let mut tries: Vec<Vec<(Id, i64)>> = cands.iter().map(|rc| vec![*rc]).collect();
    let mut first: Vec<(Id, i64)> = Vec::new();
    for (r, c) in &cands {
        if !first.iter().any(|(x, _)| x == r) {
            first.push((*r, *c));
        }
    }
    if first.len() > 1 {
        tries.push(first);
    }
    for t in tries {
        let (mut a2, mut d2, mut rs2, mut ex2) = (ast, digits.clone(), rs.to_vec(), exprs.to_vec());
        let mut made: Vec<(Id, Id)> = Vec::new();
        for (r, c) in t {
            let (na, nd, (hi, lo)) = split(g, a2, &d2, r, c);
            a2 = na;
            d2 = nd;
            let m = g.binop_py("MUL", hi, &Arg::Int(c));
            let to = g.binop("ADD", m, lo);
            made.push((r, to));
            ex2 = ex2
                .iter()
                .map(|e| g.substitute(*e, &HashMap::from([(r, to)])))
                .collect();
            let at = rs2.iter().position(|x| *x == r).unwrap();
            rs2.splice(at..at + 1, [hi, lo]);
        }
        if let Some(got) = solve(g, &ex2, &rs2) {
            return Some((a2, d2, ex2, rs2, got, made));
        }
    }
    None
}

// ----------------------------------------------------------------- collapse
/// `collapse`: every one-point reduction collapsed.
fn collapse(g: &mut Graph, mut ast: Id, mut digits: Digits) -> (Id, Digits) {
    'outer: loop {
        for red in g.toposort(ast) {
            if !is_sum(g, red) {
                continue;
            }
            if let Some((a, d)) = collapse_one(g, ast, &digits, red) {
                ast = a;
                digits = d;
                continue 'outer;
            }
        }
        return (ast, digits);
    }
}

fn collapse_one(g: &mut Graph, ast: Id, digits: &Digits, red: Id) -> Option<(Id, Digits)> {
    let sup = support(g, g.src(red)[0])?;
    let reduced = g.src(red)[1..].to_vec();
    let inside = ranges(g, sup);
    let rs: Vec<Id> = reduced
        .iter()
        .copied()
        .filter(|r| inside.contains(r))
        .collect();
    let ro: Vec<Id> = inside
        .iter()
        .copied()
        .filter(|r| !reduced.contains(r))
        .collect();
    if rs.is_empty() {
        return None;
    }
    let rs2 = rs.clone();
    let mut solve =
        move |g: &mut Graph, exprs: &[Id], outer: &[Id]| -> Option<Vec<(Vec<i64>, i64)>> {
            let mut all = outer.to_vec();
            all.extend(rs2.iter().copied());
            let grid = grid(g, &exprs[..1], &all)?;
            let xo = coords(g, outer);
            let pts_all = coords(g, &rs2);
            let per = pts_all.len();
            let mut count = vec![0usize; xo.len()];
            let mut first = vec![0usize; xo.len()];
            for (i, cnt) in count.iter_mut().enumerate() {
                for j in 0..per {
                    if grid[0].f(i * per + j) != 0.0 {
                        if *cnt == 0 {
                            first[i] = j;
                        }
                        *cnt += 1;
                    }
                }
            }
            if count.iter().any(|c| *c > 1) {
                return None;
            }
            let rows: Vec<usize> = (0..xo.len()).filter(|i| count[*i] == 1).collect();
            let x: Vec<Vec<i64>> = rows.iter().map(|i| xo[*i].clone()).collect();
            let mut fits = Vec::new();
            for (j, r) in rs2.iter().enumerate() {
                let y: Vec<i128> = rows.iter().map(|i| pts_all[first[*i]][j] as i128).collect();
                let f = affine_fit(&x, &y)?;
                let e = extent(g, *r);
                for p in &xo {
                    let v: i64 = p.iter().zip(&f.0).map(|(a, b)| a * b).sum::<i64>() + f.1;
                    if v < 0 || v >= e {
                        return None;
                    }
                }
                fits.push(f);
            }
            Some(fits)
        };
    let (ast, digits, _, ro, fits, _) = with_splits(g, ast, digits, &[sup], &ro, &mut solve)?;
    let arg = g.arg(red).clone();
    let red = g.toposort(ast).into_iter().find(|u| {
        g.op(*u) == "REDUCE" && rs.iter().all(|r| g.src(*u)[1..].contains(r)) && *g.arg(*u) == arg
    })?;
    let mut subs = HashMap::new();
    for (r, (c, c0)) in rs.iter().zip(&fits) {
        let b = build_on(g, &ro, c, *c0, *r);
        subs.insert(*r, b);
    }
    let mut body = g.substitute(g.src(red)[0], &subs);
    let mut masks = HashMap::new();
    for u in g.toposort(body) {
        if g.op(u) == "WHERE" && dtype::is_float(g.dtype(u)) && g.is_invalid(g.src(u)[2]) {
            let (c, a) = (g.src(u)[0], g.src(u)[1]);
            let z = g.const_like(a, &Arg::Int(0));
            let w = g.where_(c, a, z);
            masks.insert(u, w);
        }
    }
    body = g.substitute(body, &masks);
    let left: Vec<Id> = g.src(red)[1..]
        .iter()
        .copied()
        .filter(|r| !rs.contains(r))
        .collect();
    let to = if left.is_empty() {
        body
    } else {
        let mut src = vec![body];
        src.extend(left);
        g.with_src(red, src)
    };
    Some((g.substitute(ast, &HashMap::from([(red, to)])), digits))
}

// --------------------------------------------------------------------- fit
/// A condition: everywhere (`All`) or an index-only bool node.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Cond {
    All,
    At(Id),
}

fn either(g: &mut Graph, a: Option<Cond>, b: Cond) -> Cond {
    match (a, b) {
        (Some(Cond::All), _) | (_, Cond::All) => Cond::All,
        (None, b) => b,
        (Some(Cond::At(x)), Cond::At(y)) if x == y => b,
        (Some(Cond::At(x)), Cond::At(y)) => Cond::At(g.binop("OR", x, y)),
    }
}

fn conjuncts(g: &Graph, c: Id) -> Vec<Id> {
    if g.op(c) == "AND" {
        g.src(c).iter().flat_map(|s| conjuncts(g, *s)).collect()
    } else {
        vec![c]
    }
}

fn both(g: &mut Graph, c: Cond, s: Option<Id>) -> Cond {
    let Some(s) = s else { return c };
    let Cond::At(c) = c else { return Cond::At(s) };
    let mut terms: Vec<Id> = Vec::new();
    for t in conjuncts(g, c).into_iter().chain(conjuncts(g, s)) {
        if !terms.contains(&t) {
            terms.push(t);
        }
    }
    let mut out = terms[0];
    for t in &terms[1..] {
        out = g.binop("AND", out, *t);
    }
    Cond::At(out)
}

/// `used`: the condition outside which each node's value does not reach
/// the output.
fn used(g: &mut Graph, ast: Id) -> HashMap<Id, Cond> {
    let mut cond: HashMap<Id, Cond> = HashMap::from([(ast, Cond::All)]);
    let order = g.toposort(ast);
    for p in order.into_iter().rev() {
        let Some(c) = cond.get(&p).copied() else {
            continue;
        };
        let src = g.src(p).to_vec();
        let kids: Vec<(Id, Cond)> =
            if g.op(p) == "WHERE" && index_only(g, src[0]) && g.dtype(src[0]) == "bool" {
                let on = src[0];
                let k1 = both(g, c, Some(on));
                let ne = g.binop_py("CMPNE", on, &Arg::Bool(true));
                let k2 = both(g, c, Some(ne));
                vec![(on, c), (src[1], k1), (src[2], k2)]
            } else {
                src.iter().map(|s| (*s, c)).collect()
            };
        for (s, k) in kids {
            let prev = cond.get(&s).copied();
            let v = either(g, prev, k);
            cond.insert(s, v);
        }
    }
    cond
}

/// `fit`: every read whose index is not affine given the affine index
/// equal to it where its gate lets it through and its value is used.
fn fit(g: &mut Graph, mut ast: Id, mut digits: Digits) -> (Id, Digits) {
    'outer: loop {
        let reach = used(g, ast);
        for x in g.toposort(ast) {
            if g.op(x) != "INDEX" || g.src(x).len() < 2 || param_of(g, x).is_none() {
                continue;
            }
            let (gate, inner) = match gated(g, g.src(x)[1]) {
                Some((gt, i)) => (Some(gt), i),
                None => (None, g.src(x)[1]),
            };
            if affine(g, inner).is_some() || !index_only(g, inner) {
                continue;
            }
            let ctx = match reach.get(&x).copied().unwrap_or(Cond::All) {
                Cond::All => None,
                Cond::At(c) => Some(c),
            };
            if let Some((a, d)) = fit_one(g, ast, &digits, x, gate, inner, ctx) {
                ast = a;
                digits = d;
                continue 'outer;
            }
        }
        return (ast, digits);
    }
}

fn terms(g: &Graph, u: Id) -> Vec<Id> {
    if g.op(u) == "ADD" {
        g.src(u).iter().flat_map(|s| terms(g, *s)).collect()
    } else {
        vec![u]
    }
}

/// `_groups`: terms grouped by the ranges they share (transitively).
fn groups(g: &Graph, ts: &[Id]) -> Vec<(HashSet<Id>, Vec<Id>)> {
    let mut out: Vec<(HashSet<Id>, Vec<Id>)> = Vec::new();
    for t in ts {
        let rs: HashSet<Id> = ranges(g, *t).into_iter().collect();
        let (hit, rest): (Vec<_>, Vec<_>) = out.into_iter().partition(|(r, _)| !r.is_disjoint(&rs));
        let mut merged: Vec<Id> = hit.iter().flat_map(|(_, v)| v.clone()).collect();
        merged.push(*t);
        let mut all = rs;
        for (r, _) in &hit {
            all.extend(r.iter().copied());
        }
        out = rest;
        out.push((all, merged));
    }
    out
}

type Fit = ((Vec<i64>, i64), Vec<Id>);

/// `_solve`: the affine form equal to `ex[0]` wherever `ex[1]` holds for
/// some value of the other ranges.
fn solve(g: &mut Graph, ex: &[Id], rs: &[Id]) -> Option<Fit> {
    let mine = ranges(g, ex[0]);
    let own: Vec<Id> = rs.iter().copied().filter(|r| mine.contains(r)).collect();
    let grid = grid(g, &ex[..1], &own)?;
    let pts = coords(g, &own);
    let ok: Vec<bool> = if ex.len() > 1 {
        let others: Vec<Id> = rs.iter().copied().filter(|r| !own.contains(r)).collect();
        held(g, ex[1], &own, &others)?
    } else {
        vec![true; pts.len()]
    };
    let x: Vec<Vec<i64>> = pts
        .iter()
        .zip(&ok)
        .filter(|(_, k)| **k)
        .map(|(p, _)| p.clone())
        .collect();
    let y: Vec<i128> = (0..pts.len())
        .filter(|i| ok[*i])
        .map(|i| grid[0].i(i))
        .collect();
    let got = affine_fit(&x, &y)?;
    Some((got, own))
}

/// `_held`: `cond` over `own` (row-major): where it holds for some value of
/// `others`; past `MAX_POINTS` a block of the largest other at a time.
fn held(g: &mut Graph, cond: Id, own: &[Id], others: &[Id]) -> Option<Vec<bool>> {
    let n: usize = own.iter().map(|r| extent(g, *r) as usize).product();
    let size = n * others
        .iter()
        .map(|r| extent(g, *r) as usize)
        .product::<usize>();
    if size <= MAX_POINTS {
        let mut all = own.to_vec();
        all.extend_from_slice(others);
        let grid = grid(g, &[cond], &all)?;
        let per = size.checked_div(n).unwrap_or(0);
        return Some(
            (0..n)
                .map(|i| (0..per).any(|j| grid[0].f(i * per + j) != 0.0))
                .collect(),
        );
    }
    if others.is_empty() {
        return None;
    }
    let big = *others.iter().max_by_key(|r| extent(g, **r)).unwrap();
    let rest: Vec<Id> = others.iter().copied().filter(|r| *r != big).collect();
    let e = extent(g, big) as usize;
    let step = (MAX_POINTS / (size / e)).max(1);
    let nxt = 1 + g
        .toposort(cond)
        .iter()
        .filter(|x| g.op(**x) == "RANGE")
        .map(|x| range_id(g, *x))
        .max()
        .unwrap_or(0);
    let ax = axis(g, big);
    let mut out = vec![false; n];
    let mut s = 0;
    while s < e {
        let k = step.min(e - s);
        let sub = g.range(k as i64, nxt, &ax);
        let shifted = g.binop_py("ADD", sub, &Arg::Int(s as i64));
        let c2 = g.substitute(cond, &HashMap::from([(big, shifted)]));
        let mut others2 = rest.clone();
        others2.push(sub);
        let got = held(g, c2, own, &others2)?;
        for (o, v) in out.iter_mut().zip(got) {
            *o |= v;
        }
        s += step;
    }
    Some(out)
}

#[allow(clippy::too_many_arguments)]
fn fit_one(
    g: &mut Graph,
    ast: Id,
    digits: &Digits,
    x: Id,
    gate: Option<Id>,
    inner: Id,
    ctx: Option<Id>,
) -> Option<(Id, Digits)> {
    let idx = g.src(x)[1];
    let ts = terms(g, inner);
    let on: Option<Id> = match ctx {
        None => gate,
        Some(c) => match both(g, Cond::At(c), gate) {
            Cond::At(v) => Some(v),
            Cond::All => None,
        },
    };
    let mut made: Vec<(Id, Id)> = Vec::new();
    let mut new: Option<Id> = None;
    let rest: Vec<Id> = ts
        .iter()
        .copied()
        .filter(|t| affine(g, *t).is_none())
        .collect();
    let mut sink_src = rest.clone();
    if let Some(o) = on {
        sink_src.push(o);
    }
    let sink = g.make("SINK", "void", sink_src, Arg::None);
    let joint = ranges(g, sink);
    let points: usize = joint.iter().map(|r| extent(g, *r) as usize).product();
    let grps: Vec<Vec<Id>> = if points <= MAX_POINTS {
        vec![rest.clone()]
    } else {
        groups(g, &rest).into_iter().map(|(_, v)| v).collect()
    };
    let (mut ast, mut digits) = (ast, digits.clone());
    for group in &grps {
        let mut part = group[0];
        for t in &group[1..] {
            part = g.binop("ADD", part, *t);
        }
        let mmap: HashMap<Id, Id> = made.iter().copied().collect();
        if !made.is_empty() {
            part = g.substitute(part, &mmap);
        }
        let mine: HashSet<Id> = ranges(g, part).into_iter().collect();
        let conds: Vec<Id> = match on {
            None => Vec::new(),
            Some(o) => {
                let o2 = if made.is_empty() {
                    o
                } else {
                    g.substitute(o, &mmap)
                };
                conjuncts(g, o2)
            }
        };
        let mut cond: Option<Id> = None;
        for c in conds {
            let rc: HashSet<Id> = ranges(g, c).into_iter().collect();
            if grps.len() == 1 || !mine.is_disjoint(&rc) {
                cond = Some(match cond {
                    None => c,
                    Some(p) => g.binop("AND", p, c),
                });
            }
        }
        let exprs: Vec<Id> = match cond {
            None => vec![part],
            Some(c) => vec![part, c],
        };
        let sk = g.make("SINK", "void", exprs.clone(), Arg::None);
        let rs = ranges(g, sk);
        let mut s = |g: &mut Graph, ex: &[Id], r: &[Id]| solve(g, ex, r);
        let (a2, d2, ex2, _, ((coef, c0), own), more) =
            with_splits(g, ast, &digits, &exprs, &rs, &mut s)?;
        ast = a2;
        digits = d2;
        for (k, v) in more {
            match made.iter_mut().find(|(x, _)| *x == k) {
                Some(slot) => slot.1 = v,
                None => made.push((k, v)),
            }
        }
        let term = build_on(g, &own, &coef, c0, ex2[0]);
        new = Some(match new {
            None => term,
            Some(n) => g.binop("ADD", n, term),
        });
    }
    let mmap: HashMap<Id, Id> = made.iter().copied().collect();
    let mut new = g.substitute(new?, &mmap);
    for t in &ts {
        if affine(g, *t).is_some() {
            let t2 = g.substitute(*t, &mmap);
            new = g.binop("ADD", new, t2);
        }
    }
    let on = match on {
        Some(o) if !made.is_empty() => Some(g.substitute(o, &mmap)),
        o => o,
    };
    let to = match on {
        None => new,
        Some(o) => g.valid(new, o),
    };
    if ctx.is_some() {
        let now_x = if made.is_empty() {
            x
        } else {
            g.substitute(x, &mmap)
        };
        let mut src = g.src(now_x).to_vec();
        src[1] = to;
        let r = g.with_src(now_x, src);
        return Some((g.substitute(ast, &HashMap::from([(now_x, r)])), digits));
    }
    let now = g.substitute(idx, &mmap);
    let mut subs = HashMap::new();
    for u in g.toposort(ast) {
        if g.op(u) == "INDEX" && g.src(u).len() > 1 && g.src(u)[1] == now {
            let mut src = g.src(u).to_vec();
            src[1] = to;
            let r = g.with_src(u, src);
            subs.insert(u, r);
        }
    }
    if subs.is_empty() {
        return None;
    }
    Some((g.substitute(ast, &subs), digits))
}
