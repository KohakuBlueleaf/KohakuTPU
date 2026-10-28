//! The expression shapes a window contraction or a tabled sum is
//! (`window._factors`, `_weights`, the one sum).

use super::*;

pub(super) fn is_zero(e: &E) -> bool {
    matches!(e.as_ref(), Expr::Const(v) if *v == 0.0)
}

pub(super) fn table_load(e: &E, accesses: &[Access]) -> Option<usize> {
    match e.as_ref() {
        Expr::Load(i) if accesses[*i].slot == Slot::Table => Some(*i),
        _ => None,
    }
}

/// `window._factors`: `(buffer reads, gate tables, scale)` of a product of
/// reads, constants and zero-else selects on tables.
pub(super) fn factors(e: &E, accesses: &[Access]) -> Option<(Vec<usize>, Vec<usize>, f64)> {
    fn walk(x: &E, acc: &[Access], out: &mut (Vec<usize>, Vec<usize>, f64)) -> bool {
        match x.as_ref() {
            Expr::Load(i) => {
                out.0.push(*i);
                acc[*i].slot != Slot::Table
            }
            Expr::Const(v) => {
                out.2 *= v;
                true
            }
            Expr::Op(n, a) if n == "mul" => a.iter().all(|y| walk(y, acc, out)),
            Expr::Op(n, a) if n == "where" && is_zero(&a[2]) => match table_load(&a[0], acc) {
                Some(g) => {
                    out.1.push(g);
                    walk(&a[1], acc, out)
                }
                None => false,
            },
            _ => false,
        }
    }
    let mut out = (Vec::new(), Vec::new(), 1.0);
    walk(e, accesses, &mut out).then_some(out)
}

/// How a table read weighs (`window._weights`).
#[derive(Clone, Copy, PartialEq)]
pub(super) enum How {
    Value,
    On,
    Off,
}

/// `window._weights`: `(buffer reads, [(table read, how)], scale)`.
#[allow(clippy::type_complexity)]
pub(super) fn weights(e: &E, accesses: &[Access]) -> Option<(Vec<usize>, Vec<(usize, How)>, f64)> {
    fn walk(x: &E, acc: &[Access], out: &mut (Vec<usize>, Vec<(usize, How)>, f64)) -> bool {
        match x.as_ref() {
            Expr::Load(i) => {
                if acc[*i].slot == Slot::Table {
                    out.1.push((*i, How::Value));
                } else {
                    out.0.push(*i);
                }
                true
            }
            Expr::Const(v) => {
                out.2 *= v;
                true
            }
            Expr::Op(n, a) if n == "mul" => a.iter().all(|y| walk(y, acc, out)),
            Expr::Op(n, a) if n == "where" => match table_load(&a[0], acc) {
                Some(g) if is_zero(&a[2]) => {
                    out.1.push((g, How::On));
                    walk(&a[1], acc, out)
                }
                Some(g) if is_zero(&a[1]) => {
                    out.1.push((g, How::Off));
                    walk(&a[2], acc, out)
                }
                _ => false,
            },
            _ => false,
        }
    }
    let mut out = (Vec::new(), Vec::new(), 1.0);
    walk(e, accesses, &mut out).then_some(out)
}

/// The one sum of a plan's expression and its argument.
pub(super) fn one_sum(plan: &Plan) -> Option<(E, E)> {
    let reds = all_reduces(plan.expr.as_ref()?);
    match reds.as_slice() {
        [red] => match red.as_ref() {
            Expr::Reduce(op, arg) if op == "sum" => Some((red.clone(), arg.clone())),
            _ => None,
        },
        _ => None,
    }
}
