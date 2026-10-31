//! `select` (L2.PASS.002, .013): `o = sel m, P, Q` where `m` compares a value
//! x with 0 and the arms are x and `s = mul x, a` (a >= 0) becomes one
//! `max x, s` or `min x, s`; the compare goes when nothing else reads it.
//! The values are the select's bit for bit.

use std::collections::{BTreeSet, HashMap};

use ktpu_l1::text::{Stmt, Term};

use super::{body_of, names, walk, write};
use crate::ir::{Program, Result};

fn num(t: &Term) -> Option<f64> {
    match t {
        Term::Float(f) => Some(*f),
        Term::Int(v, _) => Some(*v as f64),
        _ => None,
    }
}

/// `(compare, the 0's operand)` -> whether the compare holds where x > 0.
fn sign(cmp: &str, zero: usize) -> bool {
    matches!((cmp, zero), ("cmpgt", 1) | ("cmplt", 0))
}

/// `st` as a max / min where it is a select of x and a nonnegative scale of
/// x on x's sign.
fn minmax(st: &Stmt, defs: &HashMap<String, Stmt>) -> Option<Stmt> {
    if st.op != "sel" || st.target.is_none() {
        return None;
    }
    let pos = st.positional();
    if pos.len() != 3 || !pos.iter().all(|p| matches!(p, Term::Name(_))) {
        return None;
    }
    let (m, p, q) = (pos[0].as_name()?, pos[1].as_name()?, pos[2].as_name()?);
    let cmp = defs.get(m)?;
    if cmp.op != "cmpgt" && cmp.op != "cmplt" {
        return None;
    }
    let cargs = cmp.positional();
    let zero: Vec<usize> = cargs
        .iter()
        .enumerate()
        .filter(|(_, a)| num(a) == Some(0.0))
        .map(|(i, _)| i)
        .collect();
    if cargs.len() != 2 || zero.len() != 1 {
        return None;
    }
    let x = cargs[1 - zero[0]].as_name()?;
    let positive = if sign(&cmp.op, zero[0]) { p } else { q };
    let other = if positive == p { q } else { p };
    if (x != p && x != q) || other == positive {
        return None;
    }
    let scaled = if positive == x { other } else { positive };
    let mul = defs.get(scaled)?;
    if mul.op != "mul" {
        return None;
    }
    let margs = mul.positional();
    let consts: Vec<f64> = margs.iter().filter_map(|a| num(a)).collect();
    if margs.len() != 2
        || consts.len() != 1
        || !margs.iter().any(|a| a.as_name() == Some(x))
        || consts[0] < 0.0
    {
        return None;
    }
    let a = consts[0];
    let op = if (positive == x) == (a <= 1.0) {
        "max"
    } else {
        "min"
    };
    let mut out = st.clone();
    out.op = op.into();
    out.args = vec![Term::name(x), Term::name(scaled)];
    Some(out)
}

pub fn select(p: &Program) -> Result<Program> {
    let (params, stmts) = body_of(p)?;
    let out = walk(&stmts, &mut |s: Stmt, _scopes: &[Stmt]| {
        if (s.op != "par" && s.op != "for") || s.body.is_empty() {
            return Ok(vec![s]);
        }
        let mut defs: HashMap<String, Stmt> = HashMap::new();
        for b in &s.body {
            if let Some(t) = b.target_name() {
                defs.insert(t.to_string(), b.clone());
            }
        }
        let body: Vec<Stmt> = s
            .body
            .iter()
            .map(|b| minmax(b, &defs).unwrap_or_else(|| b.clone()))
            .collect();
        let mut read: BTreeSet<String> = BTreeSet::new();
        let mut todo: Vec<&Stmt> = body.iter().collect();
        while let Some(b) = todo.pop() {
            read.extend(names(&b.args));
            for a in &b.args {
                if let Term::View(n, _) = a {
                    read.insert(n.clone());
                }
            }
            todo.extend(b.body.iter());
        }
        let body = body
            .into_iter()
            .filter(|b| {
                !((b.op == "cmpgt" || b.op == "cmplt")
                    && b.target_name().map(|t| !read.contains(t)).unwrap_or(false))
            })
            .collect();
        let mut s = s;
        s.body = body;
        Ok(vec![s])
    })?;
    write(p, &params, &out)
}
