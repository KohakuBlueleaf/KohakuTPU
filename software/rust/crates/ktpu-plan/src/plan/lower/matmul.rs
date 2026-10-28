//! One sum of a product of two reads as a `matmul` plan (`_matmul`,
//! `_mnk`, `_shift`).

use super::*;

fn peel(g: &Graph, mut u: Id) -> Id {
    use crate::uop::dtype::is_float;
    while g.op(u) == "CAST" && is_float(g.dtype(u)) && is_float(g.dtype(g.src(u)[0])) {
        u = g.src(u)[0];
    }
    u
}

pub(super) fn matmul(
    g: &mut Graph,
    base: &Plan,
    value: Id,
    reader: &mut Reader,
) -> Result<Option<Plan>, Refusal> {
    let reds: Vec<Id> = g
        .toposort(value)
        .into_iter()
        .filter(|u| g.op(*u) == "REDUCE")
        .collect();
    if reds.len() != 1 {
        return Ok(None);
    }
    let red = reds[0];
    let is_add = matches!(g.arg(red), Arg::Reduce { op, .. } if op == "ADD");
    if !is_add || g.src(red).len() < 2 {
        return Ok(None);
    }
    let mul = peel(g, g.src(red)[0]);
    if g.op(mul) != "MUL" {
        return Ok(None);
    }
    let loads: Vec<Id> = g
        .src(mul)
        .to_vec()
        .into_iter()
        .map(|s| peel(g, s))
        .collect();
    if loads.iter().any(|x| g.op(*x) != "INDEX") {
        return Ok(None);
    }
    let mut ks: Vec<Id> = g.src(red)[1..]
        .iter()
        .copied()
        .filter(|r| extent(g, *r) > 1)
        .collect();
    if ks.is_empty() {
        ks = vec![g.src(red)[1]];
    }
    let mut reads = Vec::new();
    for x in &loads {
        reads.push(reader.read(g, *x)?);
    }
    let mut accs = Vec::new();
    for r in &reads {
        match r.as_ref() {
            Expr::Load(i) => accs.push(reader.accesses[*i].clone()),
            _ => return Ok(None),
        }
    }
    let mut got = None;
    for (a, b) in [(&accs[0], &accs[1]), (&accs[1], &accs[0])] {
        if let Some(x) = mnk(g, base, a, b, &ks) {
            got = Some((a.clone(), b.clone(), x));
            break;
        }
    }
    let Some((a, b, (m, n, batch))) = got else {
        return Ok(None);
    };
    let prod = expr::load(0);
    let mut keep: Vec<Access> = Vec::new();
    let mut mapping: Vec<(usize, usize)> = Vec::new();
    let e = shift(
        base.expr.as_ref().unwrap(),
        &reader.accesses,
        &prod,
        &mut keep,
        &mut mapping,
    );
    Ok(Some(Plan {
        kind: "matmul".into(),
        expr: Some(e),
        accesses: keep,
        reduce_ranges: ks.clone(),
        reductions: Vec::new(),
        a: Some(a),
        b: Some(b),
        mnk: Some((m, n, ks)),
        batch,
        ..base.clone()
    }))
}

/// `_mnk`: `(m, n, batch)` when `a` reads [.., M, K] and `b` [.., N, K].
pub fn mnk(g: &Graph, base: &Plan, a: &Access, b: &Access, ks: &[Id]) -> Option<(Id, Id, Vec<Id>)> {
    if ks.iter().any(|k| a.c(*k) == 0 || b.c(*k) == 0) {
        return None;
    }
    let live: Vec<Id> = base
        .out_ranges
        .iter()
        .copied()
        .filter(|r| extent(g, *r) > 1)
        .collect();
    let ms: Vec<Id> = live
        .iter()
        .copied()
        .filter(|r| a.c(*r) != 0 && b.c(*r) == 0)
        .collect();
    let ns: Vec<Id> = live
        .iter()
        .copied()
        .filter(|r| b.c(*r) != 0 && a.c(*r) == 0)
        .collect();
    if ms.is_empty() || ns.len() != 1 || *live.last()? != ns[0] {
        return None;
    }
    let m = *ms.last().unwrap();
    let batch = base
        .out_ranges
        .iter()
        .copied()
        .filter(|r| *r != m && *r != ns[0])
        .collect();
    Some((m, ns[0], batch))
}

/// `_shift`: the reduction replaced by `prod` (Load 0), the other reads
/// renumbered from 1 (a fresh node per visit, as Python's walk builds).
fn shift(
    e: &E,
    accesses: &[Access],
    prod: &E,
    keep: &mut Vec<Access>,
    mapping: &mut Vec<(usize, usize)>,
) -> E {
    match e.as_ref() {
        Expr::Reduce(..) => prod.clone(),
        Expr::Load(i) => {
            let j = match mapping.iter().find(|(x, _)| x == i) {
                Some((_, j)) => *j,
                None => {
                    keep.push(accesses[*i].clone());
                    mapping.push((*i, keep.len()));
                    keep.len()
                }
            };
            expr::load(j)
        }
        Expr::Op(name, args) => Rc::new(Expr::Op(
            name.clone(),
            args.iter()
                .map(|a| shift(a, accesses, prod, keep, mapping))
                .collect(),
        )),
        Expr::Const(_) => e.clone(),
    }
}
