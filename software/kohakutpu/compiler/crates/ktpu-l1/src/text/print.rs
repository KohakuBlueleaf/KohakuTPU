//! `Stmt` trees to canonical text (L1.TXT.013), byte for byte the Python
//! printer's (`syntax.fmt`, `syntax.emit`).

use super::term::{IntStyle, Stmt, Term};

/// Python's `repr(float)`: the shortest round-trip digits, exponent form
/// below 1e-4 and from 1e16.
pub fn py_float(v: f64) -> String {
    if v.is_nan() {
        return "nan".into();
    }
    if v.is_infinite() {
        return if v > 0.0 { "inf" } else { "-inf" }.into();
    }
    if v == 0.0 {
        return if v.is_sign_negative() { "-0.0" } else { "0.0" }.into();
    }
    let e = format!("{:e}", v.abs());
    let (mant, exp) = e.split_once('e').unwrap();
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp.parse::<i32>().unwrap() + 1;
    let sign = if v < 0.0 { "-" } else { "" };
    let n = digits.len() as i32;
    let body = if decpt <= -4 || decpt > 16 {
        let mut m = digits[..1].to_string();
        if n > 1 {
            m.push('.');
            m.push_str(&digits[1..]);
        }
        let x = decpt - 1;
        format!("{m}e{}{:02}", if x < 0 { '-' } else { '+' }, x.abs())
    } else if decpt <= 0 {
        format!("0.{}{digits}", "0".repeat((-decpt) as usize))
    } else if decpt >= n {
        format!("{digits}{}.0", "0".repeat((decpt - n) as usize))
    } else {
        format!(
            "{}.{}",
            &digits[..decpt as usize],
            &digits[decpt as usize..]
        )
    };
    format!("{sign}{body}")
}

fn hex(v: i128) -> String {
    let neg = v < 0;
    let mut digits = format!("{:x}", v.unsigned_abs());
    if neg {
        digits.insert(0, '-');
    }
    let mut groups = Vec::new();
    while !digits.is_empty() {
        let cut = digits.len().saturating_sub(4);
        groups.insert(0, digits[cut..].to_string());
        digits.truncate(cut);
    }
    if groups.is_empty() {
        groups.push("0".into());
    }
    format!("0x{}", groups.join("_"))
}

fn size(v: i128) -> String {
    for (unit, shift) in [("G", 30), ("M", 20), ("K", 10)] {
        if v != 0 && v.rem_euclid(1 << shift) == 0 {
            return format!("{}{unit}", v >> shift);
        }
    }
    v.to_string()
}

fn join(ts: &[Term]) -> String {
    ts.iter().map(fmt).collect::<Vec<_>>().join(", ")
}

/// One term, canonically.
pub fn fmt(t: &Term) -> String {
    match t {
        Term::Name(s) => s.clone(),
        Term::Int(v, IntStyle::Hex) => hex(*v),
        Term::Int(v, IntStyle::Size) => size(*v),
        Term::Int(v, IntStyle::Dec) => v.to_string(),
        Term::Float(v) => py_float(*v),
        Term::Dims(vs) => vs
            .iter()
            .map(|v| v.to_string())
            .collect::<Vec<_>>()
            .join("x"),
        Term::Str(s) => format!("\"{s}\""),
        Term::Neg(x) => format!("-{}", fmt(x)),
        Term::Not(x) => format!("!{}", fmt(x)),
        Term::Offset(b, o) => {
            if simple(b) && simple(o) {
                format!("{}+{}", fmt(b), fmt(o))
            } else {
                format!("{} + {}", fmt(b), paren(o, '+', false))
            }
        }
        Term::BinOp(op, a, b) => format!("{} {op} {}", paren(a, *op, true), paren(b, *op, false)),
        Term::Pos(x) => format!("+{}", fmt(x)),
        Term::Placed(x, s) => format!("{} @{}", fmt(x), fmt(s)),
        Term::Compare(op, a, b) => format!("{} {op} {}", fmt(a), fmt(b)),
        Term::Computed(p, i, None) => format!("{p}{{{}}}", fmt(i)),
        Term::Computed(p, i, Some(sl)) => format!("{p}{{{}}}[{}]", fmt(i), join(sl)),
        Term::AddAssign(a, b) => format!("{} += {}", fmt(a), fmt(b)),
        Term::Span(lo, hi) => format!("{}..{}", fmt(lo), fmt(hi)),
        Term::Typed(n, ty) => format!("{n}: {}", fmt(ty)),
        Term::Kw(n, v) => format!("{n}={}", fmt(v)),
        Term::Call(n, args) => format!("{n}({})", join(args)),
        Term::Slice(lo, hi) => format!(
            "{}:{}",
            lo.as_ref()
                .map(|x| format!("{} ", fmt(x)))
                .unwrap_or_default(),
            hi.as_ref()
                .map(|x| format!(" {}", fmt(x)))
                .unwrap_or_default()
        ),
        Term::NewAxis => "*".into(),
        Term::View(n, sl) => format!("{n}[{}]", join(sl)),
        Term::Tuple(items) if items.len() == 1 => format!("({},)", fmt(&items[0])),
        Term::Tuple(items) => format!("({})", join(items)),
        Term::Assign(a, b) => match a.as_ref() {
            Term::Name(_) => format!("{}={}", fmt(a), fmt(b)),
            _ => format!("{} = {}", fmt(a), fmt(b)),
        },
        Term::Member(a, b) => format!("{} in {}", fmt(a), fmt(b)),
        Term::Arrow(s) => s.clone(),
    }
}

fn simple(t: &Term) -> bool {
    matches!(
        t,
        Term::Name(_)
            | Term::Int(..)
            | Term::Float(_)
            | Term::View(..)
            | Term::Computed(..)
            | Term::Call(..)
    )
}

fn op_rank(op: char) -> u8 {
    match op {
        '+' | '-' => 1,
        _ => 2,
    }
}

fn rank(t: &Term) -> u8 {
    match t {
        Term::Offset(..) => 1,
        Term::BinOp(op, ..) => op_rank(*op),
        _ => 3,
    }
}

/// `t` as an operand of `op`: bracketed when it binds less tightly, or
/// equally on the right of a non-associative operator.
fn paren(t: &Term, op: char, left: bool) -> String {
    let (r, mine) = (rank(t), op_rank(op));
    let inner = match t {
        Term::BinOp(o, ..) => *o,
        _ => '+',
    };
    let right = !left && r == mine && ("-/%".contains(op) || inner != op);
    if r < mine || (right && !crate::mutant::on("l1.text/no-right-parens")) {
        format!("({})", fmt(t))
    } else {
        fmt(t)
    }
}

fn op_text(st: &Stmt) -> String {
    match &st.target {
        None => st.op.clone(),
        Some(t) => format!("{} = {}", fmt(t), st.op),
    }
}

/// A statement's line without its annotation.
pub fn head(st: &Stmt) -> String {
    if st.args.len() == 1 {
        if let Term::Assign(a, b) = &st.args[0] {
            return format!("{} {} = {}", op_text(st), fmt(a), fmt(b));
        }
    }
    let pos: Vec<&Term> = st.positional();
    let kws: Vec<&Term> = st.args.iter().filter(|a| a.as_kwarg().is_some()).collect();
    let mut parts = vec![op_text(st)];
    if !pos.is_empty() {
        if st.commas {
            let mut out = String::new();
            let mut prev: Option<&Term> = None;
            for a in &pos {
                if let Some(p) = prev {
                    let arrow = matches!(a, Term::Arrow(_)) || matches!(p, Term::Arrow(_));
                    out.push_str(if arrow { " " } else { ", " });
                }
                out.push_str(&fmt(a));
                prev = Some(a);
            }
            parts.push(out);
        } else {
            parts.push(pos.iter().map(|a| fmt(a)).collect::<Vec<_>>().join(" "));
        }
    }
    parts.extend(kws.iter().map(|a| fmt(a)));
    parts.join(" ")
}

/// Canonical text for `stmts` at `indent` levels, annotations aligned no
/// further than column `align` (48 by default; see `emit`).
pub fn emit_at(stmts: &[Stmt], indent: usize, align: usize) -> String {
    let pad = " ".repeat(4 * indent);
    let heads: Vec<String> = stmts.iter().map(head).collect();
    let longest = heads
        .iter()
        .zip(stmts)
        .filter(|(_, s)| s.annot.is_some())
        .map(|(h, _)| h.chars().count())
        .max()
        .unwrap_or(0);
    let col = (longest + 2).min(align);
    let mut lines: Vec<String> = Vec::new();
    let mut prev: Option<&Stmt> = None;
    for (h, s) in heads.iter().zip(stmts) {
        if indent == 0 {
            if let Some(p) = prev {
                if !s.body.is_empty() || !p.body.is_empty() {
                    lines.push(String::new());
                }
            }
        }
        prev = Some(s);
        let mut line = format!("{pad}{h}");
        if let Some(a) = &s.annot {
            let n = h.chars().count();
            if n < col {
                let width = pad.len() + col;
                let have = line.chars().count();
                line.push_str(&" ".repeat(width.saturating_sub(have)));
                line = format!("{line}: {}", fmt(a));
            } else {
                line = format!("{line}  : {}", fmt(a));
            }
        }
        lines.push(line);
        if !s.body.is_empty() {
            let inner = emit_at(&s.body, indent + 1, align);
            lines.push(inner.trim_end_matches('\n').to_string());
        }
    }
    lines.join("\n") + "\n"
}

/// Canonical text for `stmts` (L1.TXT.013).
pub fn emit(stmts: &[Stmt]) -> String {
    emit_at(stmts, 0, 48)
}
