//! What the lowering reads off the UOp graph (`uops.py`): the envelope, a
//! range's extent, affine indices, gates, the PARAM an INDEX reads.

use super::*;

/// `uops.envelope`: `(store, ranges)` of a kernel ast.
pub(super) fn envelope(g: &Graph, ast: Id) -> Option<(Id, Vec<Id>)> {
    if mutant::on("tg.lower/envelope") {
        return None;
    }
    if g.op(ast) != "SINK" || g.src(ast).len() != 1 {
        return None;
    }
    let top = g.src(ast)[0];
    if g.op(top) == "STORE" {
        return Some((top, Vec::new()));
    }
    if g.op(top) != "END" || g.src(top).is_empty() {
        return None;
    }
    let store = g.src(top)[0];
    if g.op(store) != "STORE" {
        return None;
    }
    let ranges = g.src(top)[1..].to_vec();
    if ranges.iter().any(|r| fold::extent(g, *r).is_none()) {
        return None;
    }
    Some((store, ranges))
}

pub fn extent(g: &Graph, r: Id) -> i64 {
    fold::extent(g, r).expect("a range of an accepted envelope")
}

pub(super) fn const_int(g: &Graph, u: Id) -> Option<i64> {
    match g.arg(u) {
        Arg::Int(v) if g.op(u) == "CONST" => Some(*v),
        _ => None,
    }
}

/// `uops.affine`: `({range: coefficient}, offset)` of an affine index.
pub fn affine(g: &Graph, idx: Id) -> Option<(Vec<(Id, i64)>, i64)> {
    let got = affine_of(g, idx);
    if mutant::on("tg.lower/offset") {
        return got.map(|(c, o)| (c, o + 1));
    }
    got
}

fn affine_of(g: &Graph, idx: Id) -> Option<(Vec<(Id, i64)>, i64)> {
    match g.op(idx) {
        "RANGE" => return Some((vec![(idx, 1)], 0)),
        "CONST" => {
            if let Some(v) = const_int(g, idx) {
                return Some((Vec::new(), v));
            }
        }
        "ADD" if g.src(idx).len() == 2 => {
            let (a, oa) = affine(g, g.src(idx)[0])?;
            let (b, ob) = affine(g, g.src(idx)[1])?;
            let mut out = a;
            for (r, c) in b {
                match out.iter_mut().find(|(x, _)| *x == r) {
                    Some(slot) => slot.1 += c,
                    None => out.push((r, c)),
                }
            }
            out.retain(|(_, c)| *c != 0);
            return Some((out, oa + ob));
        }
        "MUL" if g.src(idx).len() == 2 => {
            let s = g.src(idx).to_vec();
            for (held, k) in [(s[0], s[1]), (s[1], s[0])] {
                if let Some(k) = const_int(g, k) {
                    let (t, o) = affine(g, held)?;
                    let t = t
                        .into_iter()
                        .map(|(r, c)| (r, c * k))
                        .filter(|(_, c)| *c != 0)
                        .collect();
                    return Some((t, o * k));
                }
            }
        }
        _ => {}
    }
    None
}

/// `uops.gated`: `(gate, idx)` of `WHERE(gate, idx, Invalid)`.
pub fn gated(g: &Graph, u: Id) -> Option<(Id, Id)> {
    if g.op(u) == "WHERE" && g.src(u).len() == 3 && g.is_invalid(g.src(u)[2]) {
        return Some((g.src(u)[0], g.src(u)[1]));
    }
    None
}

/// `uops.param_of`: the PARAM an INDEX reads.
pub fn param_of(g: &Graph, mut u: Id) -> Option<Id> {
    while !matches!(g.op(u), "PARAM" | "BUFFER") && !g.src(u).is_empty() {
        u = g.src(u)[0];
    }
    (g.op(u) == "PARAM").then_some(u)
}

pub(super) fn summarise(g: &Graph, ast: Id) -> String {
    let mut ops: Vec<String> = g
        .toposort(ast)
        .iter()
        .map(|u| g.op(*u).to_string())
        .collect();
    ops.sort();
    ops.dedup();
    ops.join(", ")
}

pub(super) fn range_id(g: &Graph, r: Id) -> (i64, String) {
    match g.arg(r) {
        Arg::Range { id, axis, .. } => (*id, axis.clone()),
        _ => (0, String::new()),
    }
}
