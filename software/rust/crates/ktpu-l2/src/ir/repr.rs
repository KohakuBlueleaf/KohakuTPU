//! Python's `repr` of a term (`syntax.py`'s dataclasses), as refusal messages
//! quote it (`f"got {t!r}"`).

use ktpu_l1::text::{py_float, IntStyle, Term};

/// Python's `repr(str)`: single quotes unless the text holds one.
pub fn py_str(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(q);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            c if c == q => {
                out.push('\\');
                out.push(c);
            }
            c => out.push(c),
        }
    }
    out.push(q);
    out
}

fn tuple(items: &[Term]) -> String {
    match items.len() {
        0 => "()".into(),
        1 => format!("({},)", repr(&items[0])),
        _ => format!(
            "({})",
            items.iter().map(repr).collect::<Vec<_>>().join(", ")
        ),
    }
}

fn opt(t: &Option<Box<Term>>) -> String {
    t.as_ref().map(|x| repr(x)).unwrap_or_else(|| "None".into())
}

/// `repr(t)` as Python prints the dataclass.
pub fn repr(t: &Term) -> String {
    match t {
        Term::Name(s) => format!("Name(text={})", py_str(s)),
        Term::Int(v, st) => {
            let style = match st {
                IntStyle::Dec => "dec",
                IntStyle::Hex => "hex",
                IntStyle::Size => "size",
            };
            format!("Int(value={v}, style='{style}')")
        }
        Term::Float(v) => format!("Float(value={})", py_float(*v)),
        Term::Dims(vs) => {
            let items: Vec<String> = vs.iter().map(|v| v.to_string()).collect();
            let inner = if items.len() == 1 {
                format!("({},)", items[0])
            } else {
                format!("({})", items.join(", "))
            };
            format!("Dims(values={inner})")
        }
        Term::Str(s) => format!("Str(text={})", py_str(s)),
        Term::Neg(x) => format!("Neg(x={})", repr(x)),
        Term::Not(x) => format!("Not(x={})", repr(x)),
        Term::Offset(b, o) => format!("Offset(base={}, off={})", repr(b), repr(o)),
        Term::BinOp(op, a, b) => {
            format!("BinOp(op='{op}', a={}, b={})", repr(a), repr(b))
        }
        Term::Pos(x) => format!("Pos(x={})", repr(x)),
        Term::Placed(x, s) => format!("Placed(x={}, space={})", repr(x), repr(s)),
        Term::Compare(op, a, b) => {
            format!("Compare(op={}, a={}, b={})", py_str(op), repr(a), repr(b))
        }
        Term::Computed(p, i, sl) => format!(
            "Computed(prefix={}, index={}, slices={})",
            py_str(p),
            repr(i),
            sl.as_ref()
                .map(|s| tuple(s))
                .unwrap_or_else(|| "None".into())
        ),
        Term::AddAssign(a, b) => format!("AddAssign(lhs={}, rhs={})", repr(a), repr(b)),
        Term::Span(lo, hi) => format!("Span(lo={}, hi={})", repr(lo), repr(hi)),
        Term::Typed(n, ty) => format!("Typed(name={}, type={})", py_str(n), repr(ty)),
        Term::Kw(n, v) => format!("Kw(name={}, value={})", py_str(n), repr(v)),
        Term::Call(n, a) => format!("Call(name={}, args={})", py_str(n), tuple(a)),
        Term::Slice(lo, hi) => format!("Slice(lo={}, hi={})", opt(lo), opt(hi)),
        Term::NewAxis => "NewAxis()".into(),
        Term::View(n, s) => format!("View(name={}, slices={})", py_str(n), tuple(s)),
        Term::Tuple(items) => format!("Tuple(items={})", tuple(items)),
        Term::Assign(a, b) => format!("Assign(lhs={}, rhs={})", repr(a), repr(b)),
        Term::Member(a, b) => format!("Member(lhs={}, rhs={})", repr(a), repr(b)),
        Term::Arrow(s) => format!("Arrow(text={})", py_str(s)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dataclass_reprs() {
        let v = Term::View("x".into(), vec![Term::int(4)]);
        assert_eq!(
            repr(&v),
            "View(name='x', slices=(Int(value=4, style='dec'),))"
        );
        assert_eq!(repr(&Term::Slice(None, None)), "Slice(lo=None, hi=None)");
        assert_eq!(repr(&Term::Float(0.5)), "Float(value=0.5)");
    }
}
