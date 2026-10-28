//! A lazy expression's leaves, product, reductions and lowering to a kernel
//! expression; numpy's broadcast.

use super::*;

/// A leaf's identity (`_key`): a tensor, however many `Ref`s name it, or a
/// product node.
#[derive(Clone, Copy, PartialEq, Debug)]
enum Key {
    Tensor(usize),
    Prod(usize),
}

fn key(x: &KE) -> Key {
    match x.as_ref() {
        K::Ref(t) => Key::Tensor(*t),
        _ => Key::Prod(Rc::as_ptr(x) as usize),
    }
}

/// `_leaves`: the reads, a tensor once, depth first.
pub(super) fn leaves(e: &KE, out: &mut Vec<KE>) {
    match e.as_ref() {
        K::Ref(_) | K::Prod(..) => {
            if !out.iter().any(|x| key(x) == key(e)) {
                out.push(e.clone());
            }
        }
        K::Op(_, a) => a.iter().for_each(|x| leaves(x, out)),
        K::Reduce(_, a) => leaves(a, out),
        K::Const(_) => {}
    }
}

pub(super) fn prod_of(e: &KE) -> Option<KE> {
    let mut out = Vec::new();
    leaves(e, &mut out);
    out.into_iter().find(|x| matches!(x.as_ref(), K::Prod(..)))
}

/// `X.reduces` of a lazy expression: the outermost reductions, a shared
/// node once.
pub(super) fn reduces(e: &KE) -> Vec<KE> {
    match e.as_ref() {
        K::Reduce(..) => vec![e.clone()],
        K::Op(_, a) => {
            let mut out: Vec<KE> = Vec::new();
            for r in a.iter().flat_map(reduces) {
                if !out.iter().any(|q| Rc::ptr_eq(q, &r)) {
                    out.push(r);
                }
            }
            out
        }
        _ => vec![],
    }
}

/// `_lower`: each leaf as `Load(its index in leaves)`, shared nodes shared.
pub(super) fn lower(e: &KE, ls: &[KE], memo: &mut HashMap<usize, E>) -> E {
    let id = Rc::as_ptr(e) as usize;
    if let Some(v) = memo.get(&id) {
        return v.clone();
    }
    let out = match e.as_ref() {
        K::Ref(_) | K::Prod(..) => expr::load(ls.iter().position(|x| key(x) == key(e)).unwrap()),
        K::Op(n, a) => expr::op(n, a.iter().map(|x| lower(x, ls, memo)).collect()),
        K::Reduce(op, a) => expr::reduce(op, lower(a, ls, memo)),
        K::Const(v) => expr::constant(*v),
    };
    memo.insert(id, out.clone());
    out
}

/// `X.count`: ops in the expression, each node once.
pub(super) fn count(e: &E) -> usize {
    fn walk(x: &E, seen: &mut Vec<usize>) {
        let id = Rc::as_ptr(x) as usize;
        if seen.contains(&id) {
            return;
        }
        match x.as_ref() {
            X::Op(_, a) => {
                seen.push(id);
                a.iter().for_each(|y| walk(y, seen));
            }
            X::Reduce(_, a) => {
                seen.push(id);
                walk(a, seen);
            }
            _ => {}
        }
    }
    let mut seen = Vec::new();
    walk(e, &mut seen);
    seen.len()
}

/// numpy's `broadcast_shapes`.
pub(super) fn broadcast(shapes: &[&[i64]]) -> Option<Vec<i64>> {
    let n = shapes.iter().map(|s| s.len()).max().unwrap_or(0);
    let mut out = vec![1; n];
    for s in shapes {
        for (k, d) in s.iter().enumerate() {
            let at = n - s.len() + k;
            if out[at] == 1 {
                out[at] = *d;
            } else if *d != 1 && *d != out[at] {
                return None;
            }
        }
    }
    Some(out)
}
