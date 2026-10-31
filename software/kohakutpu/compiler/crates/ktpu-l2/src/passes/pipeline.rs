//! `pipeline` (L2.PASS.008): a node-scope `for` whose body runs pars of two
//! unit kinds gets `pipe=2`; when its first `par` runs on units a later `par`
//! also runs on, the loop is rotated -- that `par` of iteration i + 1 ahead
//! of the rest of iteration i -- where no memory view of it meets one of the
//! pars it passes (one writing).

use std::collections::BTreeSet;
use std::sync::Arc;

use ktpu_l1::text::{Env, Stmt, Term, Val};

use super::{body_of, head, subst_stmt, unit_of, walk, write};
use crate::ir::read::instances_in;
use crate::ir::{value, Body, Instance, Node, Program, Result, StageX};
use crate::lower::memory as M;

/// Iterations a pipelined node loop keeps in flight.
const PIPE: i128 = 2;

fn kinds(s: &Stmt) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for b in &s.body {
        if b.op == "par" {
            if let Some(u) = unit_of(b).filter(|u| u != "node") {
                out.insert(u);
            }
        } else if b.op == "for" || b.op == "when" {
            out.extend(kinds(b));
        }
    }
    out
}

pub fn pipeline(p: &Program) -> Result<Program> {
    if crate::mutant::on("l2.passes/pipeline-off") {
        return Ok(p.clone());
    }
    let (params, stmts) = body_of(p)?;
    let out = walk(&stmts, &mut |s: Stmt, scopes: &[Stmt]| {
        let unit = scopes
            .iter()
            .any(|q| q.op == "par" && unit_of(q).as_deref() != Some("node"));
        if s.op != "for" || unit || s.kw("pipe").is_some() || kinds(&s).len() < 2 {
            return Ok(vec![s]);
        }
        let mut piped = s.clone();
        piped.args.push(Term::Assign(
            Box::new(Term::name("pipe")),
            Box::new(Term::int(PIPE)),
        ));
        Ok(rotated(p, &params, &piped)?.unwrap_or_else(|| vec![piped]))
    })?;
    write(p, &params, &out)
}

fn rotated(p: &Program, params: &[(String, Term)], lp: &Stmt) -> Result<Option<Vec<Stmt>>> {
    let Some((var, Term::Int(lo, _), Term::Int(hi, _))) = head(lp) else {
        return Ok(None);
    };
    let (lo, hi) = (lo as i64, hi as i64);
    let all_units = lp
        .body
        .iter()
        .all(|b| b.op == "par" && unit_of(b).map(|u| u != "node").unwrap_or(false));
    if hi - lo < 2 || !all_units {
        return Ok(None);
    }
    let (first, rest) = (&lp.body[0], &lp.body[1..]);
    let body = match write(p, params, std::slice::from_ref(lp)).and_then(|q| q.body()) {
        Ok(b) => b,
        Err(_) => return Ok(None),
    };
    let units: Vec<BTreeSet<(String, Option<i64>)>> = lp
        .body
        .iter()
        .map(|b| {
            stage_insts(&body, b, &var, lo)
                .unwrap_or_default()
                .iter()
                .map(|x| (x.kind.clone(), x.index))
                .collect()
        })
        .collect();
    if !units[1..].iter().any(|u| !units[0].is_disjoint(u)) {
        return Ok(None);
    }
    for i in lo..hi - 1 {
        let Some(moved) = stage_views(&body, first, &var, i + 1)? else {
            return Ok(None);
        };
        for b in rest {
            match stage_views(&body, b, &var, i)? {
                Some(got) if !meets(&moved, &got) => {}
                _ => return Ok(None),
            }
        }
    }
    let plus = Term::BinOp(
        '+',
        Box::new(Term::name(var.clone())),
        Box::new(Term::int(1)),
    );
    let nxt = subst_stmt(first, &[(var.clone(), plus)]);
    let span = Term::Member(
        Box::new(Term::name(var.clone())),
        Box::new(Term::Span(
            Box::new(Term::int(lo as i128)),
            Box::new(Term::int((hi - 1) as i128)),
        )),
    );
    let mut body_loop = lp.clone();
    body_loop.args = std::iter::once(span)
        .chain(lp.args.iter().skip(1).cloned())
        .collect();
    body_loop.body = std::iter::once(nxt).chain(rest.iter().cloned()).collect();
    let mut out = vec![
        subst_stmt(first, &[(var.clone(), Term::int(lo as i128))]),
        body_loop,
    ];
    out.extend(
        rest.iter()
            .map(|b| subst_stmt(b, &[(var.clone(), Term::int((hi - 1) as i128))])),
    );
    Ok(Some(out))
}

fn tree(stmts: &[Arc<Node>], out: &mut Vec<Arc<Node>>) {
    for s in stmts {
        out.push(s.clone());
        tree(s.body(), out);
    }
}

fn stage_insts(body: &Body, par: &Stmt, var: &str, i: i64) -> Option<Vec<Instance>> {
    let mut all = Vec::new();
    tree(&body.stmts, &mut all);
    let node = all
        .into_iter()
        .find(|n| matches!(n.as_ref(), Node::Par(_)) && n.st().line == par.line)?;
    let mut env = Env::new();
    env.insert(var.to_string(), Val::Int(i as i128));
    let got = instances_in(body, &[node], &env).ok()?;
    Some(
        got.into_iter()
            .filter_map(|s| match s.x {
                StageX::Par(x) => Some(x),
                _ => None,
            })
            .flatten()
            .collect(),
    )
}

type View = (String, bool, Vec<(i64, i64)>);

/// `[(buffer, write, box)]` of every memory view a stage's instances read or
/// write at iteration `i`; None when a view does not evaluate.
fn stage_views(body: &Body, par: &Stmt, var: &str, i: i64) -> Result<Option<Vec<View>>> {
    let Some(insts) = stage_insts(body, par, var, i) else {
        return Ok(None);
    };
    let mut out = Vec::new();
    for inst in &insts {
        for (n, env) in &inst.items {
            let Node::Op(op) = n.as_ref() else { continue };
            let pos = op.st.positional();
            let (t, wr) = match op.op.as_str() {
                "fill" | "load" if !pos.is_empty() => (pos[0], false),
                "drain" if pos.len() == 3 => (pos[2], true),
                "store" if !pos.is_empty() => (pos[0], true),
                _ => continue,
            };
            let v = match value(body, &op.st, t, env)? {
                Term::Name(n) => Term::View(n, vec![]),
                other => other,
            };
            let Term::View(name, _) = &v else { continue };
            let Some(buf) = body.buffers.get(name) else {
                continue;
            };
            let Ok(ax) = M::axes_of(&v, &buf.shape) else {
                return Ok(None);
            };
            let bx = ax
                .iter()
                .filter(|a| a.kept.is_some())
                .map(|a| (a.lo, a.lo + a.size))
                .collect();
            out.push((name.clone(), wr, bx));
        }
    }
    Ok(Some(out))
}

fn meets(xs: &[View], ys: &[View]) -> bool {
    for (bx, wx, box_x) in xs {
        for (by, wy, box_y) in ys {
            if bx != by || !(*wx || *wy) {
                continue;
            }
            if box_x
                .iter()
                .zip(box_y)
                .all(|((a0, a1), (b0, b1))| a0 < b1 && b0 < a1)
            {
                return true;
            }
        }
    }
    false
}
