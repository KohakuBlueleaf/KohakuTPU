//! The expression in float64 (`evaluate`, numpy's arithmetic): the folding
//! of all-constant ops and the host reference of a scalar expression.

use super::{Expr, OpName, RedOp, E};

fn bit(b: bool) -> f64 {
    if b {
        1.0
    } else {
        0.0
    }
}

/// numpy's maximum / minimum: a NaN operand gives NaN.
fn npmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else {
        a.max(b)
    }
}

fn npmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else {
        a.min(b)
    }
}

/// One op over float64 values.
pub fn fold(o: OpName, a: &[f64]) -> f64 {
    match o {
        OpName::Add => a[0] + a[1],
        OpName::Sub => a[0] - a[1],
        OpName::Mul => a[0] * a[1],
        OpName::Div => a[0] / a[1],
        OpName::Max => npmax(a[0], a[1]),
        OpName::Min => npmin(a[0], a[1]),
        OpName::Pow => a[0].powf(a[1]),
        OpName::Neg => -a[0],
        OpName::Abs => a[0].abs(),
        OpName::Recip => 1.0 / a[0],
        OpName::Exp2 => a[0].exp2(),
        OpName::Log2 => a[0].log2(),
        OpName::Sqrt => a[0].sqrt(),
        OpName::Rsqrt => 1.0 / a[0].sqrt(),
        OpName::Sin => a[0].sin(),
        OpName::Trunc => a[0].trunc(),
        OpName::Cmplt => bit(a[0] < a[1]),
        OpName::Cmpgt => bit(a[0] > a[1]),
        OpName::Cmpeq => bit(a[0] == a[1]),
        OpName::Cmpne => bit(a[0] != a[1]),
        OpName::Where => {
            if a[0] != 0.0 {
                a[1]
            } else {
                a[2]
            }
        }
        OpName::Fma => a[0] * a[1] + a[2],
        OpName::And => bit(a[0] != 0.0 && a[1] != 0.0),
        OpName::Or => bit(a[0] != 0.0 || a[1] != 0.0),
        OpName::Xor => bit((a[0] != 0.0) ^ (a[1] != 0.0)),
        OpName::Not => bit(a[0] == 0.0),
        OpName::Id => a[0],
    }
}

/// `e` over rows of operand values (`arrays[i]` is operand i's row, a
/// one-value row a scalar); a reduction folds the row.
pub fn evaluate(e: &E, arrays: &[Vec<f64>]) -> Vec<f64> {
    match &**e {
        Expr::Load(i) => arrays[*i].clone(),
        Expr::Const(x) => vec![*x],
        Expr::Reduce(r, x) => {
            let v = evaluate(x, arrays);
            let out = match r {
                RedOp::Sum => v.iter().sum(),
                RedOp::Max => v.iter().copied().fold(f64::NEG_INFINITY, npmax),
                RedOp::Prod => v.iter().product(),
            };
            vec![out]
        }
        Expr::Op(o, args) => {
            let vals: Vec<Vec<f64>> = args.iter().map(|a| evaluate(a, arrays)).collect();
            let n = vals.iter().map(Vec::len).max().unwrap_or(1);
            (0..n)
                .map(|j| {
                    let xs: Vec<f64> = vals
                        .iter()
                        .map(|v| if v.len() == 1 { v[0] } else { v[j] })
                        .collect();
                    fold(*o, &xs)
                })
                .collect()
        }
    }
}
