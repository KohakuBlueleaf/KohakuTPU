//! A checked graph as `.ktpu` statements and terms (`ktpu_l1::text`), the
//! trees the reader makes of the tracer's text (DSL.TEXT): ktpu-l2's
//! `Program::build` takes them.
//!
//! An index is built as the tracer spells it, then folded by the text
//! grammar's precedence (`ktpu.pest` expr / product / unary), so the tree
//! is the one that spelling reads to.

use ktpu_l1::text::{IntStyle, Stmt as KStmt, Term};

use crate::graph::*;
use crate::mutant;
use crate::text;

#[derive(Clone, Debug, PartialEq)]
enum Tok {
    Int(i64),
    Name(String),
    Op(char),
    Open,
    Close,
}

fn int_toks(v: i64) -> Vec<Tok> {
    if v < 0 {
        vec![Tok::Op('-'), Tok::Int(-v)]
    } else {
        vec![Tok::Int(v)]
    }
}

/// `const + sum(coef * atom)`, positive terms first (DSL.IDX.006).
fn sym_toks(s: &Sym) -> Vec<Tok> {
    let mut parts: Vec<(bool, Vec<Tok>)> = Vec::new();
    for (a, c) in &s.terms {
        let mut t = atom_toks(a);
        if c.abs() != 1 {
            t.push(Tok::Op('*'));
            t.push(Tok::Int(c.abs()));
        }
        parts.push((*c < 0, t));
    }
    if s.konst != 0 {
        parts.push((s.konst < 0, vec![Tok::Int(s.konst.abs())]));
    }
    parts.sort_by_key(|p| p.0);
    let mut out = Vec::new();
    for (i, (neg, t)) in parts.into_iter().enumerate() {
        if i > 0 || neg {
            out.push(Tok::Op(if neg { '-' } else { '+' }));
        }
        out.extend(t);
    }
    out
}

fn atom_toks(a: &Atom) -> Vec<Tok> {
    let (op, x, y) = match a {
        Atom::Var(n) => return vec![Tok::Name(n.clone())],
        Atom::Floor(x, y) => ('/', x, y),
        Atom::Mod(x, y) => ('%', x, y),
        Atom::Mul(x, y) => ('*', x, y),
    };
    let mut out = vec![Tok::Open];
    out.extend(operand_toks(x));
    out.push(Tok::Op(op));
    out.extend(operand_toks(y));
    out.push(Tok::Close);
    out
}

/// An index inside an atom or a condition: a sum of several parts is
/// bracketed (DSL.IDX.009).
fn operand_toks(x: &Index) -> Vec<Tok> {
    match x {
        Index::Int(v) => int_toks(*v),
        Index::Sym(s) => {
            let t = sym_toks(s);
            if s.terms.len() + usize::from(s.konst != 0) <= 1
                || (mutant::on(mutant::ATOM_SUM_SHORTCUT) && t.first() == Some(&Tok::Open))
            {
                t
            } else {
                let mut out = vec![Tok::Open];
                out.extend(t);
                out.push(Tok::Close);
                out
            }
        }
    }
}

struct Fold<'a> {
    toks: &'a [Tok],
    at: usize,
}

impl Fold<'_> {
    fn peek_op(&self, ops: &str) -> Option<char> {
        match self.toks.get(self.at) {
            Some(Tok::Op(c)) if ops.contains(*c) => Some(*c),
            _ => None,
        }
    }

    fn expr(&mut self) -> Term {
        let mut acc = self.product();
        while let Some(c) = self.peek_op("+-") {
            self.at += 1;
            let rhs = Box::new(self.product());
            acc = if c == '+' {
                Term::Offset(Box::new(acc), rhs)
            } else {
                Term::BinOp('-', Box::new(acc), rhs)
            };
        }
        acc
    }

    fn product(&mut self) -> Term {
        let mut acc = self.unary();
        while let Some(c) = self.peek_op("*/%") {
            self.at += 1;
            acc = Term::BinOp(c, Box::new(acc), Box::new(self.unary()));
        }
        acc
    }

    fn unary(&mut self) -> Term {
        if self.peek_op("-").is_some() {
            self.at += 1;
            return Term::Neg(Box::new(self.unary()));
        }
        let t = self.toks[self.at].clone();
        self.at += 1;
        match t {
            Tok::Int(v) => Term::Int(v as i128, IntStyle::Dec),
            Tok::Name(n) => Term::Name(n),
            Tok::Open => {
                let e = self.expr();
                self.at += 1;
                e
            }
            Tok::Op(_) | Tok::Close => unreachable!("tokens of a printed index"),
        }
    }
}

fn fold(toks: &[Tok]) -> Term {
    Fold { toks, at: 0 }.expr()
}

/// An index term (DSL.IDX).
pub fn index(x: &Index) -> Term {
    match x {
        Index::Int(v) => fold(&int_toks(*v)),
        Index::Sym(s) => fold(&sym_toks(s)),
    }
}

/// A `kt.when` condition.
pub fn cond(c: &Cond) -> Term {
    Term::Compare(
        c.op.text().into(),
        Box::new(fold(&operand_toks(&c.a))),
        Box::new(fold(&operand_toks(&c.b))),
    )
}

fn int(v: i64) -> Term {
    Term::dec(v as i128)
}

/// A float operand as Python's repr reads (DSL.TEXT.004).
pub fn float(v: f64) -> Term {
    let t = text::py_float(v);
    let body = t.trim_start_matches('-');
    let mag = Term::Float(match body {
        "inf" => f64::INFINITY,
        "nan" => f64::NAN,
        _ => body.parse().expect("a printed float"),
    });
    if t.starts_with('-') {
        Term::Neg(Box::new(mag))
    } else {
        mag
    }
}

fn name(s: &str) -> Term {
    Term::Name(s.to_string())
}

fn tuple(xs: &[i64]) -> Term {
    Term::Tuple(xs.iter().map(|x| int(*x)).collect())
}

fn kw(k: &str, v: Term) -> Term {
    Term::Assign(Box::new(name(k)), Box::new(v))
}

fn layout_value(k: &str, v: &LayoutValue) -> Term {
    match v {
        LayoutValue::Ints(xs) if k == "tile" && xs.len() >= 2 => {
            Term::Dims(vec![xs[0] as i128, xs[1] as i128])
        }
        LayoutValue::Int(n) => int(*n),
        LayoutValue::Str(s) => name(s),
        LayoutValue::Bool(b) => name(if *b { "True" } else { "False" }),
        LayoutValue::Ints(xs) => tuple(xs),
    }
}

fn typed(dtype: Dtype, shape: &[i64], space: Term) -> Term {
    let dims = shape.iter().map(|d| int(*d)).collect();
    Term::Placed(
        Box::new(Term::View(dtype.name().into(), dims)),
        Box::new(space),
    )
}

/// `f16[512, 128] @dram(tile=64x64)` (DSL.SPEC.007).
pub fn spec(s: &Spec) -> Term {
    let space = if s.layout.is_empty() {
        name(s.space.name())
    } else {
        let args = s
            .layout
            .iter()
            .map(|(k, v)| Term::Kw(k.clone(), Box::new(layout_value(k, v))))
            .collect();
        Term::Call(s.space.name().into(), args)
    };
    typed(s.dtype, &s.shape, space)
}

/// `f32[4, 32] @reg(gt4)` (DSL.VAL.001).
pub fn vtype(t: &ValueType) -> Term {
    let space = match t.mode {
        Some(Mode::Gt4) => Term::Call(t.space.name().into(), vec![name("gt4")]),
        None => name(t.space.name()),
    };
    typed(t.dtype, &t.shape, space)
}

fn all() -> Term {
    Term::Slice(None, None)
}

fn run(lo: &Index, size: i64) -> Term {
    Term::Slice(
        Some(Box::new(index(lo))),
        Some(Box::new(Term::Pos(Box::new(int(size))))),
    )
}

/// A buffer view (DSL.VIEW.006): the name when every entry left is `:`.
pub fn view(v: &View) -> Term {
    let mut axes = v.axes.as_slice();
    while let [rest @ .., Axis::All] = axes {
        axes = rest;
    }
    if axes.is_empty() {
        return name(&v.buffer);
    }
    let entries = axes
        .iter()
        .map(|a| match a {
            Axis::New => Term::NewAxis,
            Axis::All => all(),
            Axis::Idx(e) => index(e),
            Axis::Run(lo, size) => run(lo, *size),
        })
        .collect();
    Term::View(v.buffer.clone(), entries)
}

/// Rows of a value (DSL.VAL.005); the check refuses a new axis.
pub fn value_view(v: &ValueView) -> Term {
    let entries = v
        .axes
        .iter()
        .map(|a| match a {
            Axis::Run(Index::Int(lo), size) if v.axes.iter().position(|b| b == a) == Some(1) => {
                Term::Slice(Some(Box::new(int(*lo))), Some(Box::new(int(lo + size))))
            }
            Axis::Run(lo, size) => run(lo, *size),
            Axis::Idx(e) => index(e),
            Axis::All | Axis::New => all(),
        })
        .collect();
    Term::View(v.value.clone(), entries)
}

/// `KIND[index]`, `KIND[any]`.
pub fn unit(u: &UnitRef) -> Term {
    let at = u.index.as_ref().map(index).unwrap_or_else(|| name("any"));
    Term::View(u.kind.name().into(), vec![at])
}

pub fn operand(o: &Operand) -> Term {
    match o {
        Operand::Value(n) => name(n),
        Operand::Bcast(n) => Term::View(n.clone(), vec![all(), Term::NewAxis]),
        Operand::Float(x) => float(x.value().expect("a checked float")),
        Operand::Vview(v) => value_view(v),
        Operand::View(v) => view(v),
        Operand::Unit(u) => unit(u),
    }
}

pub fn attr(a: &Attr) -> Term {
    match a {
        Attr::Int(n) => int(*n),
        Attr::Str(s) => name(s),
        Attr::Walk(w) => Term::Tuple(w.iter().map(|(n, s)| tuple(&[*n, *s])).collect()),
        Attr::Perm(p) => tuple(p),
        Attr::Tuple(items) => Term::Tuple(items.iter().map(attr).collect()),
        Attr::View(v) => view(v),
        Attr::Index(e) => index(e),
    }
}

fn kws(pairs: &[(String, Attr)]) -> impl Iterator<Item = Term> + '_ {
    pairs.iter().map(|(k, a)| kw(k, attr(a)))
}

fn span(var: &str, lo: &Index, hi: &Index) -> Term {
    Term::Member(
        Box::new(name(var)),
        Box::new(Term::Span(Box::new(index(lo)), Box::new(index(hi)))),
    )
}

/// A statement; `words` when its operands are apart by spaces (DSL.TEXT.002).
fn st(op: &str, args: Vec<Term>, words: bool) -> KStmt {
    let mut s = KStmt::new(op, args);
    s.commas = !words;
    s
}

fn arrow(a: &str) -> Term {
    Term::Arrow(a.into())
}

fn stmt(g: &Stmt) -> KStmt {
    let kids = |b: &[Stmt]| b.iter().map(stmt).collect::<Vec<_>>();
    match &g.node {
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
        } => {
            let mut args = vec![span(var, lo, hi), name("on"), unit(u)];
            if *classed {
                args.push(kw("classed", int(1)));
            }
            if let Some(band) = class_band {
                args.push(kw("class_band", int(*band)));
            }
            if let Some(count) = repeats {
                args.push(kw("repeats", int(*count)));
                args.push(kw(
                    "advances",
                    Term::Tuple(advances.iter().map(|v| int(*v)).collect()),
                ));
            }
            KStmt {
                body: kids(body),
                ..st("par", args, true)
            }
        }
        Node::For {
            var,
            lo,
            hi,
            pipe,
            body,
        } => {
            let mut args = vec![span(var, lo, hi)];
            args.extend(pipe.map(|k| kw("pipe", int(k))));
            KStmt {
                body: kids(body),
                ..st("for", args, true)
            }
        }
        Node::When { cond: c, body } => KStmt {
            body: kids(body),
            ..st("when", vec![cond(c)], true)
        },
        Node::Sticky { body } => KStmt {
            body: kids(body),
            ..st("if", vec![name("sticky")], true)
        },
        Node::Value {
            op,
            target,
            ty,
            args,
            kw,
        } => {
            let mut terms: Vec<Term> = args.iter().map(operand).collect();
            terms.extend(kws(kw));
            KStmt {
                target: Some(name(target)),
                annot: Some(vtype(ty)),
                ..st(op.name(), terms, false)
            }
        }
        Node::Store { dst, src } => st("store", vec![view(dst), arrow("<-"), name(src)], true),
        Node::Drain {
            src,
            dst,
            add,
            node,
        } => {
            let mut args = vec![name(src), arrow("->"), operand(dst)];
            if *add {
                args.push(name("add"));
            }
            args.extend(node.iter().map(|n| kw("node", index(n))));
            st("drain", args, true)
        }
        Node::Send { src, dst, side } => st(
            "send",
            vec![name(src), arrow("->"), unit(dst), kw("side", name(side))],
            true,
        ),
        Node::Mover {
            op,
            dst,
            src,
            flags,
            kw,
        } => {
            let mut args = vec![view(dst)];
            if let Some(s) = src {
                args.push(arrow("<-"));
                args.push(view(s));
            }
            args.extend(flags.iter().map(|f| name(f)));
            args.extend(kws(kw));
            st(op.name(), args, src.is_some())
        }
    }
}

/// The body: the scratch buffers (`hN = buffer : SPEC`), then the
/// statements (DSL.TEXT.001, DSL.SPEC.008).
pub fn stmts(g: &Graph) -> Vec<KStmt> {
    let mut out: Vec<KStmt> = g
        .scratch
        .iter()
        .map(|s| KStmt {
            target: Some(name(&s.name)),
            annot: Some(spec(&s.spec)),
            ..st("buffer", Vec::new(), false)
        })
        .collect();
    out.extend(g.body.iter().map(stmt));
    out
}

/// The signature: each port and its type, in port order.
pub fn params(g: &Graph) -> Vec<(String, Term)> {
    g.ports
        .iter()
        .map(|p| (p.name.clone(), spec(&p.spec)))
        .collect()
}
