//! L2 -> L2 passes (L2.PASS): each narrows a choice the body left open and
//! prints L2 text with it written in, read back before the next pass;
//! nothing computed changes.
//!
//! | pass | decides |
//! |---|---|
//! | select | `sel` of x and a nonnegative scale of x on x's sign: `max` / `min` |
//! | place | `mg[any]` / `vc[any]`: instance i on unit i mod the count |
//! | space | `@any` / `@mem` scratch: a cluster -> core transfer, staging or DRAM |
//! | share | a fill every cluster of a `par` makes on one view: `share=cross` |
//! | split | `gemm ... split` over operands larger than a bank: the K-chunk loop |
//! | step | a stream loop stepping further than `ainc` reaches: a node-scope loop |
//! | pipeline | a node-scope `for` running pars of two unit kinds: `pipe=2` |
//! | retile | a vector stream loop's rows a trip: the fastest tile that lowers |
//! | stage | a move of one-word runs on both sides: two moves through a scratch |

pub mod pipeline;
pub mod place;
pub mod retile;
pub mod select;
pub mod share;
pub mod space;
pub mod split;
pub mod stage;
pub mod step;

use std::collections::BTreeSet;

use ktpu_l1::text::{Env, Expander, Stmt, Term};

use crate::ir::read::write_text;
use crate::ir::{Program, Result};
use crate::lower::vector::Search;
use crate::target::Target;

/// Complete candidate cost, including every lifted node-dispatched trip.
pub fn retile_cost(p: &Program, t: &Target, search: &Search) -> Result<i64> {
    let (params, stmts) = body_of(p)?;
    let (cost, why) = retile::lowered_cost(p, &params, &stmts, t, search);
    cost.ok_or_else(|| ktpu_l1::Error::new("L2.PASS.010", why))
}

/// The passes `compile_l1` runs, in order (L2.PASS.001).
pub const PASSES: [&str; 9] = [
    "select", "place", "space", "share", "split", "step", "pipeline", "retile", "stage",
];

/// The passes in the order `compile_l1` runs them (the active mutant's).
pub fn pipeline_passes() -> Vec<&'static str> {
    let mut out = PASSES.to_vec();
    if crate::mutant::on("l2.pipeline/reordered") {
        out.reverse();
    }
    out
}

/// One pass by name.
pub fn pass(name: &str, p: &Program, t: &Target, search: &Search) -> Result<Program> {
    if name == "select" && crate::mutant::on("l2.passes/select-off") {
        return Ok(p.clone());
    }
    match name {
        "select" => select::select(p),
        "place" => place::place(p, t),
        "space" => space::space(p, t),
        "share" => share::share(p, t),
        "split" => split::split(p, t),
        "step" => step::step(p, t, search),
        "pipeline" => pipeline::pipeline(p),
        "retile" => retile::retile(p, t, search),
        "stage" => stage::stage(p, t),
        other => Err(ktpu_l1::Error::new(
            "L2.PASS.001",
            format!("no L2 pass {other}"),
        )),
    }
}

/// `p` rewritten by `passes` in order, each result read back before the next.
pub fn run(p: &Program, passes: &[&str], t: &Target, search: &Search) -> Result<Program> {
    let mut p = p.clone();
    for name in passes {
        p = pass(name, &p, t, search)?;
    }
    Ok(p)
}

/// A fn's `(name, type)` parameters.
pub type Params = Vec<(String, Term)>;

/// The fn's parameters and its statements, read-time expanded.
pub fn body_of(p: &Program) -> Result<(Params, Vec<Stmt>)> {
    let src = p.module.body(&p.name, "l2")?;
    let r = p.module.reader();
    let macros = p.module.macro_table();
    let stmts = Expander::new(&macros, &r, false).stmts(&src.stmts, &Env::new())?;
    Ok((src.params.clone(), stmts))
}

/// The program of `stmts`: the module's target line and `fn name.l2`, read
/// back.
pub fn write(p: &Program, params: &[(String, Term)], stmts: &[Stmt]) -> Result<Program> {
    let text = write_text(&p.module, &p.name, params, stmts);
    let module = ktpu_l1::text::read(&text, &p.module.file)?;
    Ok(Program {
        module,
        name: p.name.clone(),
    })
}

/// `stmts` with `f(stmt, scopes)` applied to every statement, depth first;
/// `scopes` the enclosing statements, outermost first.
pub fn walk<F>(stmts: &[Stmt], f: &mut F) -> Result<Vec<Stmt>>
where
    F: FnMut(Stmt, &[Stmt]) -> Result<Vec<Stmt>>,
{
    fn go<F>(sts: &[Stmt], scopes: &mut Vec<Stmt>, f: &mut F) -> Result<Vec<Stmt>>
    where
        F: FnMut(Stmt, &[Stmt]) -> Result<Vec<Stmt>>,
    {
        let mut out = Vec::new();
        for s in sts {
            let mut s = s.clone();
            scopes.push(s.clone());
            let body = go(&s.body, scopes, f);
            scopes.pop();
            s.body = body?;
            out.extend(f(s, scopes)?);
        }
        Ok(out)
    }
    go(stmts, &mut Vec::new(), f)
}

/// The unit kind a `par` runs on (`mg`, `vc`, `node`), if it is one.
pub fn unit_of(s: &Stmt) -> Option<String> {
    let pos = s.positional();
    match (pos.len(), pos.get(2)) {
        (3, Some(Term::View(n, _))) => Some(n.clone()),
        _ => None,
    }
}

/// The `VAR in LO..HI` head of a `par` / `for`: `(var, lo, hi)`.
pub fn head(s: &Stmt) -> Option<(String, Term, Term)> {
    match s.positional().first() {
        Some(Term::Member(lhs, rhs)) => match (lhs.as_ref(), rhs.as_ref()) {
            (Term::Name(v), Term::Span(lo, hi)) => {
                Some((v.clone(), (**lo).clone(), (**hi).clone()))
            }
            _ => None,
        },
        _ => None,
    }
}

/// Every name a term (or terms) holds, views' and keywords' included.
pub fn names(ts: &[Term]) -> BTreeSet<String> {
    fn go(t: &Term, out: &mut BTreeSet<String>) {
        match t {
            Term::Name(n) => {
                out.insert(n.clone());
            }
            Term::Int(..)
            | Term::Float(_)
            | Term::Dims(_)
            | Term::Str(_)
            | Term::NewAxis
            | Term::Arrow(_) => {}
            Term::Neg(x) | Term::Not(x) | Term::Pos(x) => go(x, out),
            Term::Offset(a, b)
            | Term::BinOp(_, a, b)
            | Term::Placed(a, b)
            | Term::Compare(_, a, b)
            | Term::AddAssign(a, b)
            | Term::Span(a, b)
            | Term::Assign(a, b)
            | Term::Member(a, b) => {
                go(a, out);
                go(b, out);
            }
            Term::Computed(_, i, sl) => {
                go(i, out);
                for s in sl.iter().flatten() {
                    go(s, out);
                }
            }
            Term::Typed(_, x) | Term::Kw(_, x) => go(x, out),
            Term::Call(_, a) | Term::View(_, a) | Term::Tuple(a) => {
                for x in a {
                    go(x, out);
                }
            }
            Term::Slice(lo, hi) => {
                if let Some(x) = lo {
                    go(x, out);
                }
                if let Some(x) = hi {
                    go(x, out);
                }
            }
        }
    }
    let mut out = BTreeSet::new();
    for t in ts {
        go(t, &mut out);
    }
    out
}

/// `t` with each name in `map` replaced by its term (`Int`s left as they are).
pub fn subst(t: &Term, map: &[(String, Term)]) -> Term {
    let s = |x: &Term| Box::new(subst(x, map));
    let v = |xs: &[Term]| xs.iter().map(|x| subst(x, map)).collect::<Vec<_>>();
    match t {
        Term::Name(n) => map
            .iter()
            .find(|(k, _)| k == n)
            .map(|(_, x)| x.clone())
            .unwrap_or_else(|| t.clone()),
        Term::Int(..)
        | Term::Float(_)
        | Term::Dims(_)
        | Term::Str(_)
        | Term::NewAxis
        | Term::Arrow(_) => t.clone(),
        Term::Neg(x) => Term::Neg(s(x)),
        Term::Not(x) => Term::Not(s(x)),
        Term::Pos(x) => Term::Pos(s(x)),
        Term::Offset(a, b) => Term::Offset(s(a), s(b)),
        Term::BinOp(op, a, b) => Term::BinOp(*op, s(a), s(b)),
        Term::Placed(a, b) => Term::Placed(s(a), s(b)),
        Term::Compare(op, a, b) => Term::Compare(op.clone(), s(a), s(b)),
        Term::Computed(p, i, sl) => Term::Computed(p.clone(), s(i), sl.as_ref().map(|x| v(x))),
        Term::AddAssign(a, b) => Term::AddAssign(s(a), s(b)),
        Term::Span(a, b) => Term::Span(s(a), s(b)),
        Term::Typed(n, x) => Term::Typed(n.clone(), s(x)),
        Term::Kw(n, x) => Term::Kw(n.clone(), s(x)),
        Term::Call(n, a) => Term::Call(n.clone(), v(a)),
        Term::Slice(lo, hi) => Term::Slice(lo.as_ref().map(|x| s(x)), hi.as_ref().map(|x| s(x))),
        Term::View(n, a) => Term::View(n.clone(), v(a)),
        Term::Tuple(a) => Term::Tuple(v(a)),
        Term::Assign(a, b) => Term::Assign(s(a), s(b)),
        Term::Member(a, b) => Term::Member(s(a), s(b)),
    }
}

/// A statement (and its block) with each name in `map` replaced; a keyword's
/// name kept.
pub fn subst_stmt(s: &Stmt, map: &[(String, Term)]) -> Stmt {
    let mut out = s.clone();
    out.args = s
        .args
        .iter()
        .map(|a| match a {
            Term::Assign(l, r) => Term::Assign(l.clone(), Box::new(subst(r, map))),
            other => subst(other, map),
        })
        .collect();
    out.body = s.body.iter().map(|b| subst_stmt(b, map)).collect();
    out
}

/// The buffer a view (or a bare name) reads.
pub fn views(t: &Term) -> Option<&str> {
    match t {
        Term::View(n, _) | Term::Name(n) => Some(n),
        _ => None,
    }
}

/// Python's `str(TextError)` first line: `FILE:L:C: MESSAGE`.
pub fn first_line(e: &ktpu_l1::Error) -> String {
    let full = if e.line > 0 {
        format!("{}:{}:{}: {}", e.file, e.line, e.col, e.message)
    } else if e.file.is_empty() {
        e.message.clone()
    } else {
        format!("{}: {}", e.file, e.message)
    };
    full.lines().next().unwrap_or("").to_string()
}
