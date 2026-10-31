//! The L2 module text of a graph, byte for byte what the Python tracer
//! prints (`dsl/trace.py`, DSL.TEXT.001-.004): its term spellings differ
//! from the canonical printer's (`c*32`, `0:32`, `x[r : +4]`), and its
//! annotations align on the padded line, at most column 48.

use ktpu_machine::Refusal;

use crate::graph::*;
use crate::mutant;

/// The annotation column cap (DSL.TEXT.003).
pub const ANNOT_COL: usize = 48;

/// Python's `repr(float)`: shortest round-trip digits, exponent form below
/// 1e-4 and from 1e16 (DSL.TEXT.004).
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
    let n = digits.len() as i32;
    let body = if !(-3..=16).contains(&decpt) {
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
    format!("{}{body}", if v < 0.0 { "-" } else { "" })
}

/// An index: `t*16`, `c*256 + t*16`, positive terms first (DSL.IDX.006, .009).
pub fn index(x: &Index) -> String {
    match x {
        Index::Int(v) => v.to_string(),
        Index::Sym(s) => sym(s),
    }
}

fn sym(s: &Sym) -> String {
    let mut parts: Vec<(bool, String)> = Vec::new();
    for (a, c) in &s.terms {
        let t = atom(a);
        parts.push(match *c {
            1 => (false, t),
            -1 => (true, t),
            c if c > 0 => (false, format!("{t}*{c}")),
            c => (true, format!("{t}*{}", -c)),
        });
    }
    if s.konst > 0 {
        parts.push((false, s.konst.to_string()));
    } else if s.konst < 0 {
        parts.push((true, (-s.konst).to_string()));
    }
    parts.sort_by_key(|p| p.0);
    let mut out = String::new();
    for (i, (neg, t)) in parts.iter().enumerate() {
        match (i, neg) {
            (0, false) => out.push_str(t),
            (0, true) => out = format!("-{t}"),
            (_, false) => out += &format!(" + {t}"),
            (_, true) => out += &format!(" - {t}"),
        }
    }
    out
}

fn atom(a: &Atom) -> String {
    let (op, x, y) = match a {
        Atom::Var(n) => return n.clone(),
        Atom::Floor(x, y) => ("/", x, y),
        Atom::Mod(x, y) => ("%", x, y),
        Atom::Mul(x, y) => ("*", x, y),
    };
    format!("({} {op} {})", operand(x), operand(y))
}

/// An index inside an atom or a condition: a compound sum parenthesised.
fn operand(x: &Index) -> String {
    match x {
        Index::Int(v) => v.to_string(),
        Index::Sym(s) => {
            let t = sym(s);
            if s.terms.len() + usize::from(s.konst != 0) <= 1
                || (mutant::on(mutant::ATOM_SUM_SHORTCUT) && t.starts_with('('))
            {
                t
            } else {
                format!("({t})")
            }
        }
    }
}

pub fn cond(c: &Cond) -> String {
    format!("{} {} {}", operand(&c.a), c.op.text(), operand(&c.b))
}

fn layout_value(k: &str, v: &LayoutValue) -> String {
    match v {
        LayoutValue::Ints(xs) if k == "tile" && xs.len() >= 2 => format!("{}x{}", xs[0], xs[1]),
        LayoutValue::Int(n) => n.to_string(),
        LayoutValue::Str(s) => s.clone(),
        LayoutValue::Bool(b) => if *b { "True" } else { "False" }.into(),
        LayoutValue::Ints(xs) => py_tuple(xs),
    }
}

fn py_tuple(xs: &[i64]) -> String {
    let inner: Vec<String> = xs.iter().map(i64::to_string).collect();
    if xs.len() == 1 {
        format!("({},)", inner[0])
    } else {
        format!("({})", inner.join(", "))
    }
}

fn dims(shape: &[i64]) -> String {
    shape
        .iter()
        .map(i64::to_string)
        .collect::<Vec<_>>()
        .join(", ")
}

/// `f16[512, 128] @dram(tile=64x64)` (DSL.SPEC.007).
pub fn spec(s: &Spec) -> String {
    let args: Vec<String> = s
        .layout
        .iter()
        .map(|(k, v)| format!("{k}={}", layout_value(k, v)))
        .collect();
    let lay = if args.is_empty() {
        String::new()
    } else {
        format!("({})", args.join(", "))
    };
    format!(
        "{}[{}] @{}{lay}",
        s.dtype.name(),
        dims(&s.shape),
        s.space.name()
    )
}

/// `f32[4, 32] @reg(gt4)` (DSL.VAL.001).
pub fn vtype(t: &ValueType) -> String {
    let mode = match t.mode {
        Some(Mode::Gt4) => "(gt4)",
        None => "",
    };
    format!(
        "{}[{}] @{}{mode}",
        t.dtype.name(),
        dims(&t.shape),
        t.space.name()
    )
}

/// A buffer view (DSL.VIEW.006).
pub fn view(v: &View) -> String {
    if v.axes.iter().all(|a| *a == Axis::All) {
        return v.buffer.clone();
    }
    let mut parts: Vec<String> = v
        .axes
        .iter()
        .map(|a| match a {
            Axis::New => "*".into(),
            Axis::All => ":".into(),
            Axis::Idx(e) => index(e),
            Axis::Run(lo, size) => format!("{} : +{size}", index(lo)),
        })
        .collect();
    while parts.last().is_some_and(|p| p == ":") {
        parts.pop();
    }
    if parts.is_empty() {
        v.buffer.clone()
    } else {
        format!("{}[{}]", v.buffer, parts.join(", "))
    }
}

/// Rows of a value (DSL.VAL.005): a constant slice whose first equal entry
/// is the column axis prints `C0:C1`. A new axis has no text: None.
pub fn value_view(v: &ValueView) -> Option<String> {
    let mut parts = Vec::new();
    for a in &v.axes {
        parts.push(match a {
            Axis::All => ":".to_string(),
            Axis::Idx(e) => index(e),
            Axis::New => return None,
            Axis::Run(Index::Int(lo), size) if v.axes.iter().position(|b| b == a) == Some(1) => {
                format!("{lo}:{}", lo + size)
            }
            Axis::Run(lo, size) => format!("{} : +{size}", index(lo)),
        });
    }
    Some(format!("{}[{}]", v.value, parts.join(", ")))
}

pub fn unit(u: &UnitRef) -> String {
    match &u.index {
        None => format!("{}[any]", u.kind.name()),
        Some(e) => format!("{}[{}]", u.kind.name(), index(e)),
    }
}

/// An operand; None for a value view with a new axis.
pub fn operand_text(o: &Operand) -> Option<String> {
    Some(match o {
        Operand::Value(n) => n.clone(),
        Operand::Bcast(n) => format!("{n}[:, *]"),
        Operand::Float(x) => py_float(x.value()?),
        Operand::Vview(v) => value_view(v)?,
        Operand::View(v) => view(v),
        Operand::Unit(u) => unit(u),
    })
}

/// Items printed as a Python tuple: `(a, b)`, `(a,)`, `()`.
fn items_tuple(inner: &[String]) -> String {
    let comma = if inner.len() == 1 { "," } else { "" };
    format!("({}{comma})", inner.join(", "))
}

fn walk(levels: &[(i64, i64)]) -> String {
    let inner: Vec<String> = levels.iter().map(|(n, s)| format!("({n}, {s})")).collect();
    items_tuple(&inner)
}

pub fn attr(a: &Attr) -> String {
    match a {
        Attr::Int(n) => n.to_string(),
        Attr::Str(s) => s.clone(),
        Attr::Walk(w) => walk(w),
        Attr::Perm(p) => py_tuple(p),
        Attr::Tuple(items) => items_tuple(&items.iter().map(attr).collect::<Vec<_>>()),
        Attr::View(v) => view(v),
        Attr::Index(e) => index(e),
    }
}

/// One printed statement: `[TARGET = ]OP ARGS`, an annotation, a block.
#[derive(Clone, Debug, PartialEq)]
pub struct Line {
    pub target: Option<String>,
    pub op: String,
    pub args: Vec<String>,
    pub kw: Vec<(String, String)>,
    pub annot: Option<String>,
    pub body: Option<Vec<Line>>,
}

impl Line {
    /// The head (DSL.TEXT.002): operands joined by `, `, by spaces for
    /// `par`, `for`, `when`, `if` and any statement with an arrow; then
    /// `k=v` keywords.
    pub fn head(&self) -> String {
        let words = matches!(self.op.as_str(), "par" | "for" | "when" | "if")
            || self.args.iter().any(|a| a == "->" || a == "<-");
        let mut parts = vec![self.args.join(if words { " " } else { ", " })];
        parts.retain(|p| !p.is_empty());
        parts.extend(self.kw.iter().map(|(k, v)| format!("{k}={v}")));
        let args = parts.join(" ");
        let lhs = self
            .target
            .as_ref()
            .map(|t| format!("{t} = "))
            .unwrap_or_default();
        if args.is_empty() {
            format!("{lhs}{}", self.op)
        } else {
            format!("{lhs}{} {args}", self.op)
        }
    }
}

/// A block's lines at `depth`, annotations aligned (DSL.TEXT.003).
pub fn block(lines: &[Line], depth: usize, out: &mut Vec<String>) {
    let pad = "    ".repeat(depth);
    let heads: Vec<String> = lines.iter().map(|l| format!("{pad}{}", l.head())).collect();
    let width = heads
        .iter()
        .zip(lines)
        .filter(|(_, l)| l.annot.is_some())
        .map(|(h, _)| h.chars().count())
        .max()
        .unwrap_or(0);
    let col = (width + 2).min(ANNOT_COL);
    for (h, l) in heads.iter().zip(lines) {
        match &l.annot {
            Some(a) => {
                let n = h.chars().count();
                if n < col {
                    out.push(format!("{h}{}: {a}", " ".repeat(col - n)));
                } else {
                    out.push(format!("{h}  : {a}"));
                }
            }
            None => out.push(h.clone()),
        }
        if let Some(body) = &l.body {
            block(body, depth + 1, out);
        }
    }
}

/// The module text (DSL.TEXT.001): `target`, a blank line, the `fn` line,
/// the body; a final newline.
pub fn module(target: &str, name: &str, params: &[(String, String)], body: &[Line]) -> String {
    let params: Vec<String> = params.iter().map(|(n, t)| format!("{n}: {t}")).collect();
    let mut out = vec![
        format!("target {target}"),
        String::new(),
        format!("fn {name}.l2({})", params.join(", ")),
    ];
    block(body, 1, &mut out);
    out.join("\n") + "\n"
}

/// A graph's L2 text as lines: the signature (`(name, type text)`) and the
/// body, scratch buffers first (DSL.TEXT.001, DSL.SPEC.008). The text dump
/// of a trace, until ktpu-l2's printer prints the tracer's text.
#[derive(Clone, Debug, PartialEq)]
pub struct L2Module {
    pub target: String,
    pub name: String,
    pub params: Vec<(String, String)>,
    pub body: Vec<Line>,
}

impl L2Module {
    /// The module text, as the Python tracer prints it.
    pub fn text(&self) -> String {
        module(&self.target, &self.name, &self.params, &self.body)
    }
}

/// The text dump of a checked graph ([`crate::check`]).
pub fn lower(g: &Graph) -> Result<L2Module, Refusal> {
    crate::check(g)?;
    let mut body: Vec<Line> = g
        .scratch
        .iter()
        .map(|s| Line {
            target: Some(s.name.clone()),
            annot: Some(spec(&s.spec)),
            ..line("buffer", Vec::new())
        })
        .collect();
    body.extend(g.body.iter().map(stmt));
    Ok(L2Module {
        target: g.target.clone(),
        name: g.name.clone(),
        params: g
            .ports
            .iter()
            .map(|p| (p.name.clone(), spec(&p.spec)))
            .collect(),
        body,
    })
}

fn line(op: &str, args: Vec<String>) -> Line {
    Line {
        target: None,
        op: op.into(),
        args,
        kw: Vec::new(),
        annot: None,
        body: None,
    }
}

fn kws(kw: &[(String, Attr)]) -> Vec<(String, String)> {
    kw.iter().map(|(k, a)| (k.clone(), attr(a))).collect()
}

fn span(var: &str, lo: &Index, hi: &Index) -> String {
    format!("{var} in {}..{}", index(lo), index(hi))
}

fn spelt(o: &Operand) -> String {
    operand_text(o).expect("a checked operand has a spelling")
}

fn stmt(st: &Stmt) -> Line {
    let kids = |b: &[Stmt]| Some(b.iter().map(stmt).collect());
    match &st.node {
        Node::Par {
            var,
            lo,
            hi,
            unit: u,
            classed,
            class_band,
            repeats,
            advances,
            body,
        } => Line {
            kw: {
                let mut kw = if *classed {
                    vec![("classed".into(), "1".into())]
                } else {
                    vec![]
                };
                if let Some(band) = class_band {
                    kw.push(("class_band".into(), band.to_string()));
                }
                if let Some(count) = repeats {
                    kw.push(("repeats".into(), count.to_string()));
                    kw.push((
                        "advances".into(),
                        format!(
                            "({},)",
                            advances
                                .iter()
                                .map(ToString::to_string)
                                .collect::<Vec<_>>()
                                .join(", ")
                        ),
                    ));
                }
                kw
            },
            body: kids(body),
            ..line("par", vec![span(var, lo, hi), "on".into(), unit(u)])
        },
        Node::For {
            var,
            lo,
            hi,
            pipe,
            body,
        } => Line {
            kw: pipe
                .iter()
                .map(|k| ("pipe".into(), k.to_string()))
                .collect(),
            body: kids(body),
            ..line("for", vec![span(var, lo, hi)])
        },
        Node::When { cond: c, body } => Line {
            body: kids(body),
            ..line("when", vec![cond(c)])
        },
        Node::Sticky { body } => Line {
            body: kids(body),
            ..line("if", vec!["sticky".into()])
        },
        Node::Value {
            op,
            target,
            ty,
            args,
            kw,
        } => Line {
            target: Some(target.clone()),
            kw: kws(kw),
            annot: Some(vtype(ty)),
            ..line(op.name(), args.iter().map(spelt).collect())
        },
        Node::Store { dst, src } => line("store", vec![view(dst), "<-".into(), src.clone()]),
        Node::Drain {
            src,
            dst,
            add,
            node,
        } => {
            let mut args = vec![src.clone(), "->".into(), spelt(dst)];
            if *add {
                args.push("add".into());
            }
            Line {
                kw: node.iter().map(|n| ("node".into(), index(n))).collect(),
                ..line("drain", args)
            }
        }
        Node::Send { src, dst, side } => Line {
            kw: vec![("side".into(), side.clone())],
            ..line("send", vec![src.clone(), "->".into(), unit(dst)])
        },
        Node::Mover {
            op,
            dst,
            src,
            flags,
            kw,
        } => {
            let mut args = vec![view(dst)];
            if let Some(s) = src {
                args.push("<-".into());
                args.push(view(s));
            }
            args.extend(flags.iter().cloned());
            Line {
                kw: kws(kw),
                ..line(op.name(), args)
            }
        }
    }
}
