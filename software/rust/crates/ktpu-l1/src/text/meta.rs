//! Read-time expansion of a body (L1.EXP.001-.010): `for .. unroll`, `when`,
//! `expand MACRO(args)`, computed names and constant arithmetic. The result
//! holds no meta statement and no expression a constant can fold.

use std::collections::HashMap;

use indexmap::IndexMap;

use super::module::Reader;
use super::term::{Stmt, Term};
use crate::error::Result;
use crate::mutant::on;

/// A value the environment binds a name to.
#[derive(Clone, Debug, PartialEq)]
pub enum Val {
    Int(i128),
    Float(f64),
    Term(Term),
}

pub type Env = HashMap<String, Val>;

/// Expands statements against `macros` (`name -> (params, body)`);
/// `unroll_all` makes every `for` unroll (an L1 body has no loop the hardware
/// runs except `loop`).
pub struct Expander<'a> {
    pub macros: &'a IndexMap<String, (Vec<String>, Vec<Stmt>)>,
    pub reader: &'a Reader,
    pub unroll_all: bool,
    depth: usize,
}

fn lit(v: &Val) -> Term {
    match v {
        Val::Int(i) => Term::int(*i),
        Val::Float(f) => Term::Float(*f),
        Val::Term(t) => t.clone(),
    }
}

fn value(t: Term) -> Val {
    match t {
        Term::Int(v, _) => Val::Int(v),
        Term::Float(v) => Val::Float(v),
        t => Val::Term(t),
    }
}

fn num(t: &Term) -> Option<f64> {
    match t {
        Term::Int(v, _) => Some(*v as f64),
        Term::Float(v) => Some(*v),
        _ => None,
    }
}

/// `a + b`, folded: two constants add; a symbol plus constants keeps one
/// constant offset.
pub fn add(a: Term, b: Term) -> Term {
    match (&a, &b) {
        (Term::Int(x, _), Term::Int(y, _)) => return Term::int(x + y),
        (Term::Int(..) | Term::Float(_), Term::Int(..) | Term::Float(_)) => {
            return Term::Float(num(&a).unwrap() + num(&b).unwrap())
        }
        _ => {}
    }
    if let (Term::Offset(base, off), Term::Int(y, _)) = (&a, &b) {
        if let Term::Int(x, _) = off.as_ref() {
            return add((**base).clone(), Term::int(x + y));
        }
    }
    if let Term::Int(0, _) = b {
        return a;
    }
    if matches!(a, Term::Int(..)) && !matches!(b, Term::Int(..)) {
        return add(b, a);
    }
    Term::Offset(Box::new(a), Box::new(b))
}

/// Python's `//` on integers.
pub fn floor_div(p: i128, q: i128) -> i128 {
    let d = p / q;
    if p % q != 0 && ((p < 0) != (q < 0)) {
        d - 1
    } else {
        d
    }
}

fn const_span(t: &Term) -> Option<(i128, i128)> {
    match t {
        Term::Span(lo, hi) => Some((lo.as_int()?, hi.as_int()?)),
        _ => None,
    }
}

fn py_mod_f(x: f64, y: f64) -> f64 {
    let m = x % y;
    if m != 0.0 && (m < 0.0) != (y < 0.0) {
        m + y
    } else {
        m
    }
}

fn compare(op: &str, x: f64, y: f64) -> bool {
    match op {
        "==" => x == y,
        "!=" => x != y,
        "<" if on("l1.expand/lt-inverted") => x >= y,
        "<" => x < y,
        "<=" => x <= y,
        ">" => x > y,
        _ => x >= y,
    }
}

fn compare_int(op: &str, x: i128, y: i128) -> bool {
    match op {
        "==" => x == y,
        "!=" => x != y,
        "<" if on("l1.expand/lt-inverted") => x >= y,
        "<" => x < y,
        "<=" => x <= y,
        ">" => x > y,
        _ => x >= y,
    }
}

impl<'a> Expander<'a> {
    pub fn new(
        macros: &'a IndexMap<String, (Vec<String>, Vec<Stmt>)>,
        reader: &'a Reader,
        unroll_all: bool,
    ) -> Expander<'a> {
        Expander {
            macros,
            reader,
            unroll_all,
            depth: 0,
        }
    }

    pub fn stmts(&mut self, stmts: &[Stmt], env: &Env) -> Result<Vec<Stmt>> {
        let mut out = Vec::new();
        for st in stmts {
            out.extend(self.stmt(st, env)?);
        }
        Ok(out)
    }

    pub fn stmt(&mut self, st: &Stmt, env: &Env) -> Result<Vec<Stmt>> {
        if st.op == "for" && st.target.is_none() && self.unrolls(st) {
            return self.for_(st, env);
        }
        if st.op == "when" && st.target.is_none() {
            let pos = st.positional();
            let Some(cond) = pos.first() else {
                return Err(self
                    .reader
                    .fail(st, "L1.EXP.003", "wanted `when CONDITION`"));
            };
            let v = self.term(cond, env, st)?;
            let Term::Int(c, _) = v else {
                if !self.unroll_all {
                    let mut kept = st.clone();
                    kept.args = vec![v];
                    kept.body = self.stmts(&st.body, env)?;
                    return Ok(vec![kept]);
                }
                return Err(self.reader.fail(
                    st,
                    "L1.EXP.009",
                    format!("`when` wants a constant condition, got {v:?}"),
                ));
            };
            return if c != 0 {
                self.stmts(&st.body, env)
            } else {
                Ok(Vec::new())
            };
        }
        if st.op == "expand" && st.target.is_none() {
            return self.expand(st, env);
        }
        let mut out = st.clone();
        out.args = st
            .args
            .iter()
            .map(|a| self.term(a, env, st))
            .collect::<Result<_>>()?;
        out.annot = match &st.annot {
            None => None,
            Some(a) => Some(self.term(a, env, st)?),
        };
        if let Some(Val::Term(Term::Name(n))) = env.get(&st.op) {
            out.op = n.clone();
        }
        out.target = match &st.target {
            Some(t @ Term::Computed(..)) => Some(self.term(t, env, st)?),
            Some(Term::Name(n)) => match env.get(n) {
                Some(Val::Term(Term::Name(m))) => Some(Term::Name(m.clone())),
                _ => Some(Term::Name(n.clone())),
            },
            other => other.clone(),
        };
        out.body = self.stmts(&st.body, env)?;
        Ok(vec![out])
    }

    fn unrolls(&self, st: &Stmt) -> bool {
        if on("l1.expand/no-unroll") {
            return false;
        }
        self.unroll_all
            || st
                .positional()
                .iter()
                .skip(1)
                .any(|p| p.as_name() == Some("unroll"))
    }

    fn for_(&mut self, st: &Stmt, env: &Env) -> Result<Vec<Stmt>> {
        let pos = st.positional();
        let (var, dom) = match pos.first() {
            Some(Term::Member(lhs, rhs)) => match lhs.as_ref() {
                Term::Name(v) => (v.clone(), rhs.as_ref()),
                _ => {
                    return Err(self.reader.fail(
                        st,
                        "L1.EXP.002",
                        "wanted `for VAR in LO..HI [unroll]`",
                    ))
                }
            },
            _ => {
                return Err(self.reader.fail(
                    st,
                    "L1.EXP.002",
                    "wanted `for VAR in LO..HI [unroll]`",
                ))
            }
        };
        let dom = self.term(dom, env, st)?;
        let Some((lo, hi)) = const_span(&dom) else {
            return Err(self.reader.fail(
                st,
                "L1.EXP.008",
                format!("an unrolled `for` needs constant bounds, got {dom:?}"),
            ));
        };
        let mut out = Vec::new();
        for v in lo..hi {
            let mut inner = env.clone();
            inner.insert(var.clone(), Val::Int(v));
            out.extend(self.stmts(&st.body, &inner)?);
        }
        Ok(out)
    }

    fn expand(&mut self, st: &Stmt, env: &Env) -> Result<Vec<Stmt>> {
        if on("l1.expand/drop-expand") {
            return Ok(Vec::new());
        }
        let pos = st.positional();
        let call = match pos.first() {
            Some(Term::Call(n, args)) if pos.len() == 1 && self.macros.contains_key(n) => {
                (n.clone(), args.clone())
            }
            other => {
                return Err(self.reader.fail(
                    st,
                    "L1.EXP.004",
                    format!("wanted `expand MACRO(args)` naming a macro, got {other:?}"),
                ))
            }
        };
        let macros = self.macros;
        let (params, body) = &macros[&call.0];
        if call.1.len() != params.len() {
            return Err(self.reader.fail(
                st,
                "L1.EXP.010",
                format!("{} takes {} arguments", call.0, params.len()),
            ));
        }
        self.depth += 1;
        if self.depth > 32 {
            return Err(self.reader.fail(
                st,
                "L1.EXP.010",
                format!("macro {} expands itself", call.0),
            ));
        }
        let mut inner = env.clone();
        for (p, a) in params.iter().zip(&call.1) {
            inner.insert(p.clone(), value(self.term(a, env, st)?));
        }
        let out = self.stmts(body, &inner)?;
        self.depth -= 1;
        Ok(out)
    }

    fn terms(&self, ts: &[Term], env: &Env, st: &Stmt) -> Result<Vec<Term>> {
        ts.iter().map(|t| self.term(t, env, st)).collect()
    }

    fn bx(&self, t: &Term, env: &Env, st: &Stmt) -> Result<Box<Term>> {
        Ok(Box::new(self.term(t, env, st)?))
    }

    /// `t` against `env`, folded.
    pub fn term(&self, t: &Term, env: &Env, st: &Stmt) -> Result<Term> {
        Ok(match t {
            Term::Name(n) => match env.get(n) {
                Some(v) => lit(v),
                None => t.clone(),
            },
            Term::Int(..)
            | Term::Float(_)
            | Term::Str(_)
            | Term::Dims(_)
            | Term::Arrow(_)
            | Term::NewAxis => t.clone(),
            Term::Neg(x) => match self.term(x, env, st)? {
                Term::Int(v, _) => Term::int(-v),
                Term::Float(v) => Term::Float(-v),
                v => Term::Neg(Box::new(v)),
            },
            Term::Not(x) => match self.term(x, env, st)? {
                Term::Int(v, _) => Term::int((v == 0) as i128),
                v => Term::Not(Box::new(v)),
            },
            Term::Pos(x) => Term::Pos(self.bx(x, env, st)?),
            Term::Offset(a, b) => add(self.term(a, env, st)?, self.term(b, env, st)?),
            Term::BinOp(op, a, b) => {
                let (a, b) = (self.term(a, env, st)?, self.term(b, env, st)?);
                self.bin(*op, a, b, st)?
            }
            Term::Compare(op, a, b) => {
                let (x, y) = (self.term(a, env, st)?, self.term(b, env, st)?);
                match (&x, &y) {
                    (Term::Int(p, _), Term::Int(q, _)) => {
                        Term::int(compare_int(op, *p, *q) as i128)
                    }
                    _ => match (num(&x), num(&y)) {
                        (Some(p), Some(q)) => Term::int(compare(op, p, q) as i128),
                        _ => Term::Compare(op.clone(), Box::new(x), Box::new(y)),
                    },
                }
            }
            Term::Computed(prefix, index, slices) => {
                let i = self.term(index, env, st)?;
                let Term::Int(iv, _) = i else {
                    if !self.unroll_all {
                        let kept = match slices {
                            None => None,
                            Some(s) => Some(self.terms(s, env, st)?),
                        };
                        return Ok(Term::Computed(prefix.clone(), Box::new(i), kept));
                    }
                    return Err(self.reader.fail(
                        st,
                        "L1.EXP.006",
                        format!("{prefix}{{...}} wants a constant index, got {i:?}"),
                    ));
                };
                let name = format!("{prefix}{iv}");
                match slices {
                    None => Term::Name(name),
                    Some(s) => Term::View(name, self.terms(s, env, st)?),
                }
            }
            Term::Span(lo, hi) => Term::Span(self.bx(lo, env, st)?, self.bx(hi, env, st)?),
            Term::Slice(lo, hi) => Term::Slice(
                match lo {
                    None => None,
                    Some(x) => Some(self.bx(x, env, st)?),
                },
                match hi {
                    None => None,
                    Some(x) => Some(self.bx(x, env, st)?),
                },
            ),
            Term::View(n, s) => Term::View(n.clone(), self.terms(s, env, st)?),
            Term::Call(n, a) => Term::Call(n.clone(), self.terms(a, env, st)?),
            Term::Tuple(items) => Term::Tuple(self.terms(items, env, st)?),
            Term::Typed(n, ty) => Term::Typed(n.clone(), self.bx(ty, env, st)?),
            Term::Kw(n, v) => Term::Kw(n.clone(), self.bx(v, env, st)?),
            Term::Placed(x, s) => Term::Placed(self.bx(x, env, st)?, self.bx(s, env, st)?),
            // A keyword or a register named on the left is a name, never a value.
            Term::Assign(a, b) => {
                let lhs = match a.as_ref() {
                    Term::Name(_) => a.clone(),
                    _ => self.bx(a, env, st)?,
                };
                Term::Assign(lhs, self.bx(b, env, st)?)
            }
            Term::AddAssign(a, b) => {
                let lhs = match a.as_ref() {
                    Term::Name(_) => a.clone(),
                    _ => self.bx(a, env, st)?,
                };
                Term::AddAssign(lhs, self.bx(b, env, st)?)
            }
            Term::Member(a, b) => Term::Member(a.clone(), self.bx(b, env, st)?),
        })
    }

    fn bin(&self, op: char, a: Term, b: Term, st: &Stmt) -> Result<Term> {
        if let (Some(x), Some(y)) = (num(&a), num(&b)) {
            if "/%".contains(op) && y == 0.0 {
                return Err(self.reader.fail(st, "L1.EXP.005", "division by zero"));
            }
            if let (Term::Int(p, _), Term::Int(q, _)) = (&a, &b) {
                let (p, q) = (*p, *q);
                return Ok(Term::int(match op {
                    '-' => p - q,
                    '*' => p * q,
                    '/' => floor_div(p, q),
                    _ => p - q * floor_div(p, q),
                }));
            }
            return Ok(Term::Float(match op {
                '-' => x - y,
                '*' => x * y,
                '/' => x / y,
                _ => py_mod_f(x, y),
            }));
        }
        if op == '-' {
            if let Term::Int(v, _) = b {
                return Ok(add(a, Term::int(-v)));
            }
        }
        Ok(Term::BinOp(op, Box::new(a), Box::new(b)))
    }
}
