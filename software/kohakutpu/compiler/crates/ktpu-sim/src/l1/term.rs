//! L1 terms as numbers: a name is the program's constant.

use std::collections::HashMap;

use ktpu_l1::text::{fmt, Term};

use crate::R;

pub type Env = HashMap<String, i64>;

pub fn int(t: &Term, env: &Env) -> R<i64> {
    Ok(match t {
        Term::Int(v, _) => *v as i64,
        Term::Name(n) => *env.get(n).ok_or(format!("unbound {n}"))?,
        Term::Neg(a) => -int(a, env)?,
        Term::Pos(a) => int(a, env)?,
        Term::Offset(a, b) => int(a, env)? + int(b, env)?,
        Term::BinOp(op, a, b) => {
            let (a, b) = (int(a, env)?, int(b, env)?);
            match op {
                '-' => a - b,
                '*' => a * b,
                '/' => a.div_euclid(b),
                '%' => a.rem_euclid(b),
                _ => return Err(format!("operator {op}")),
            }
        }
        Term::Call(n, args) if n == "parameter" && args.len() == 2 => int(&args[1], env)?,
        _ => return Err(format!("wanted a constant, got {}", fmt(t))),
    })
}

/// A `seti` value: an integer's low 24 bits, a float's E8M15 (ISA.VC.160).
pub fn immediate(t: &Term, env: &Env) -> R<u32> {
    match t {
        Term::Float(f) => Ok(crate::num::lane(*f)),
        Term::Neg(a) if matches!(**a, Term::Float(_)) => {
            let Term::Float(f) = **a else { unreachable!() };
            Ok(crate::num::lane(-f))
        }
        _ => Ok((int(t, env)? & 0xFF_FFFF) as u32),
    }
}

/// The number after `prefix` in a name like `v12`.
pub fn numbered(t: &Term, prefix: &str) -> R<u32> {
    let bad = || format!("wanted {prefix}N, got {}", fmt(t));
    let text = t.as_name().ok_or_else(bad)?;
    text.strip_prefix(prefix)
        .and_then(|r| r.parse().ok())
        .ok_or_else(bad)
}

/// `(a, b)`.
pub fn pair(t: &Term, env: &Env) -> R<(i64, i64)> {
    match t {
        Term::Tuple(items) if items.len() == 2 => Ok((int(&items[0], env)?, int(&items[1], env)?)),
        _ => Err(format!("wanted (A, B), got {}", fmt(t))),
    }
}
