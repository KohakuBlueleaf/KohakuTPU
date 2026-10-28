//! The expression rewrites the rows path takes (`_all_reduces`, `_rebuild`,
//! `_prune`, `rows.substitute`, `tree._place`, ...).

use super::*;

/// `_all_reduces`: every reduction, inner ones too, a shared node once.
pub fn all_reduces(e: &E) -> Vec<E> {
    fn go(e: &E, out: &mut Vec<E>) {
        match e.as_ref() {
            Expr::Reduce(_, a) => {
                if out.iter().any(|r| Rc::ptr_eq(r, e)) {
                    return;
                }
                out.push(e.clone());
                go(a, out);
            }
            Expr::Op(_, args) => args.iter().for_each(|a| go(a, out)),
            _ => {}
        }
    }
    let mut out = Vec::new();
    go(e, &mut out);
    out
}

/// `_rebuild`: `e` rebuilt, a node the expression shares rebuilt once.
/// `pre` may replace a node outright (its children then not walked); `post`
/// is given each other node and its rebuilt form, children first.
pub fn rebuild(e: &E, pre: &mut dyn FnMut(&E) -> Option<E>, post: &mut dyn FnMut(&E, E) -> E) -> E {
    fn walk(
        x: &E,
        pre: &mut dyn FnMut(&E) -> Option<E>,
        post: &mut dyn FnMut(&E, E) -> E,
        memo: &mut HashMap<usize, E>,
    ) -> E {
        if let Some(v) = memo.get(&ptr(x)) {
            return v.clone();
        }
        let got = match pre(x) {
            Some(v) => v,
            None => {
                let built = match x.as_ref() {
                    Expr::Op(n, args) => {
                        expr::op(n, args.iter().map(|a| walk(a, pre, post, memo)).collect())
                    }
                    Expr::Reduce(op, a) => expr::reduce(op, walk(a, pre, post, memo)),
                    _ => x.clone(),
                };
                post(x, built)
            }
        };
        memo.insert(ptr(x), got.clone());
        got
    }
    walk(e, pre, post, &mut HashMap::new())
}

/// `_prune`: `e` reading only the accesses it uses, renumbered in order of
/// use; `(e, accesses, [(new reduction, old)])`.
pub fn prune(e: &E, accesses: &[Access]) -> (E, Vec<Access>, Vec<(E, E)>) {
    let mut used: Vec<usize> = Vec::new();
    let mut remap: Vec<(E, E)> = Vec::new();
    let e2 = rebuild(
        e,
        &mut |x| match x.as_ref() {
            Expr::Load(i) => {
                let j = used.iter().position(|u| u == i).unwrap_or_else(|| {
                    used.push(*i);
                    used.len() - 1
                });
                Some(expr::load(j))
            }
            _ => None,
        },
        &mut |x, new| {
            if matches!(x.as_ref(), Expr::Reduce(..)) {
                remap.push((new.clone(), x.clone()));
            }
            new
        },
    );
    let acc = used.iter().map(|i| accesses[*i].clone()).collect();
    (e2, acc, remap)
}

/// `rows.substitute`: `e` with `red` replaced by `fold`, the reads left
/// renumbered from `first` (a fresh node each visit, as Python's walk);
/// `(expression, the reads' old indices)`.
pub(super) fn substitute(
    e: &E,
    red: Option<&E>,
    fold: Option<&E>,
    first: usize,
) -> (E, Vec<usize>) {
    fn go(e: &E, red: Option<&E>, fold: Option<&E>, first: usize, keep: &mut Vec<usize>) -> E {
        if red.is_some_and(|r| Rc::ptr_eq(e, r)) {
            return fold.unwrap().clone();
        }
        match e.as_ref() {
            Expr::Load(i) => {
                let j = keep.iter().position(|k| k == i).unwrap_or_else(|| {
                    keep.push(*i);
                    keep.len() - 1
                });
                expr::load(first + j)
            }
            Expr::Op(n, args) => expr::op(
                n,
                args.iter().map(|a| go(a, red, fold, first, keep)).collect(),
            ),
            Expr::Reduce(op, a) => expr::reduce(op, go(a, red, fold, first, keep)),
            Expr::Const(_) => e.clone(),
        }
    }
    let mut keep = Vec::new();
    let out = go(e, red, fold, first, &mut keep);
    (out, keep)
}

/// `rows._swap`: the node `red` replaced by `new`.
pub(super) fn swap(e: &E, red: &E, new: &E) -> E {
    rebuild(
        e,
        &mut |x| Rc::ptr_eq(x, red).then(|| new.clone()),
        &mut |_, b| b,
    )
}

pub(super) fn renumber(e: &E, by: usize) -> E {
    match e.as_ref() {
        Expr::Load(i) => expr::load(i + by),
        Expr::Op(n, a) => expr::op(n, a.iter().map(|x| renumber(x, by)).collect()),
        Expr::Reduce(op, a) => expr::reduce(op, renumber(a, by)),
        Expr::Const(_) => e.clone(),
    }
}

/// `tree._place`: `post` with its row (`Load(0)`) as `head`, its other
/// reads after the `base` head ones.
pub(super) fn place_head(post: &E, head: &E, base: usize) -> E {
    match post.as_ref() {
        Expr::Load(0) => head.clone(),
        Expr::Load(i) => expr::load(base + i - 1),
        Expr::Op(n, a) => expr::op(n, a.iter().map(|x| place_head(x, head, base)).collect()),
        _ => post.clone(),
    }
}

/// `tree._shift`: `o` read `rows` rows further down.
pub(super) fn shift(o: &Operand, rows: i64, w: i64) -> Operand {
    match o.kind.as_str() {
        "full" | "tile" => Operand {
            offset: o.offset + rows * if o.pitch != 0 { o.pitch } else { w },
            ..o.clone()
        },
        "row" => Operand {
            offset: o.offset + rows,
            ..o.clone()
        },
        _ => o.clone(),
    }
}

/// The ranges the reduction `red` reduces (by identity).
pub fn reductions_of(plan: &Plan, red: &E) -> Vec<Id> {
    plan.reductions
        .iter()
        .find(|(r, _)| Rc::ptr_eq(r, red))
        .map(|(_, rs)| rs.clone())
        .expect("a reduction of the plan")
}

/// `_leaf_reduce`: the first reduction with none inside it.
pub(super) fn leaf_reduce(e: &E) -> E {
    all_reduces(e)
        .into_iter()
        .find(|r| match r.as_ref() {
            Expr::Reduce(_, a) => all_reduces(a).is_empty(),
            _ => false,
        })
        .expect("a reduction")
}

/// `itertools.permutations` order.
pub(super) fn permutations(xs: &[Id]) -> Vec<Vec<Id>> {
    if xs.is_empty() {
        return vec![vec![]];
    }
    let mut out = Vec::new();
    for i in 0..xs.len() {
        let mut rest = xs.to_vec();
        let x = rest.remove(i);
        for mut p in permutations(&rest) {
            p.insert(0, x);
            out.push(p);
        }
    }
    out
}

/// Python's repr of an `Operand`.
pub(super) fn py_operand(o: &Operand) -> String {
    format!(
        "Operand(kind='{}', dtype='{}', offset={}, pitch={}, rows={})",
        o.kind, o.dtype, o.offset, o.pitch, o.rows
    )
}

/// `coef[r] = s`, a new range appended.
pub(super) fn put(coef: &mut Vec<(Id, i64)>, r: Id, s: i64) {
    match coef.iter_mut().find(|(x, _)| *x == r) {
        Some(slot) => slot.1 = s,
        None => coef.push((r, s)),
    }
}
