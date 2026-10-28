//! The elementwise expression a generated kernel computes
//! (`kernels/expr.py`): `Load(i)` reads operand i, `Const` is a number,
//! `Op(name, args)` one of [`OpName`], `Reduce(op, x)` folds a row. A node
//! reached twice through one `Rc` is one node: emitted once ([`emit`]) and
//! one reduction ([`reduces`]).

pub mod emit;
pub mod eval;

pub use emit::emit;
pub use eval::{evaluate, fold};

use crate::pyrepr::Py;
use ktpu_machine::Refusal;
use std::collections::HashSet;
use std::rc::Rc;

pub type E = Rc<Expr>;

#[derive(Debug, PartialEq)]
pub enum Expr {
    Load(usize),
    Const(f64),
    Op(OpName, Vec<E>),
    Reduce(RedOp, E),
}

macro_rules! ops {
    ($($v:ident $s:literal $n:literal),* $(,)?) => {
        /// The expression ops and their arity (`OPS`).
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        pub enum OpName { $($v),* }

        impl OpName {
            pub const ALL: &'static [OpName] = &[$(OpName::$v),*];

            pub fn name(self) -> &'static str {
                match self { $(OpName::$v => $s),* }
            }

            pub fn arity(self) -> usize {
                match self { $(OpName::$v => $n),* }
            }
        }
    };
}

ops! {
    Add "add" 2, Sub "sub" 2, Mul "mul" 2, Div "div" 2, Max "max" 2, Min "min" 2,
    Pow "pow" 2, Neg "neg" 1, Abs "abs" 1, Recip "recip" 1, Exp2 "exp2" 1,
    Log2 "log2" 1, Sqrt "sqrt" 1, Rsqrt "rsqrt" 1, Sin "sin" 1, Trunc "trunc" 1,
    Cmplt "cmplt" 2, Cmpgt "cmpgt" 2, Cmpeq "cmpeq" 2, Cmpne "cmpne" 2,
    Where "where" 3, Fma "fma" 3, And "and" 2, Or "or" 2, Xor "xor" 2, Not "not" 1,
    Id "id" 1,
}

impl OpName {
    pub fn parse(name: &str) -> Option<OpName> {
        OpName::ALL.iter().copied().find(|o| o.name() == name)
    }
}

/// Row reductions.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RedOp {
    Sum,
    Max,
    Prod,
}

impl RedOp {
    pub fn name(self) -> &'static str {
        match self {
            RedOp::Sum => "sum",
            RedOp::Max => "max",
            RedOp::Prod => "prod",
        }
    }

    pub fn parse(name: &str) -> Option<RedOp> {
        [RedOp::Sum, RedOp::Max, RedOp::Prod]
            .into_iter()
            .find(|r| r.name() == name)
    }
}

/// An operand of [`op`]: an expression or a number (made a `Const`).
pub enum Operand {
    E(E),
    Num(f64),
}

impl From<E> for Operand {
    fn from(e: E) -> Operand {
        Operand::E(e)
    }
}

impl From<&E> for Operand {
    fn from(e: &E) -> Operand {
        Operand::E(e.clone())
    }
}

impl From<f64> for Operand {
    fn from(x: f64) -> Operand {
        Operand::Num(x)
    }
}

pub fn load(i: usize) -> E {
    Rc::new(Expr::Load(i))
}

pub fn konst(x: f64) -> E {
    Rc::new(Expr::Const(x))
}

pub fn reduce(op: RedOp, x: impl Into<Operand>) -> E {
    Rc::new(Expr::Reduce(op, operand(x.into())))
}

fn operand(o: Operand) -> E {
    match o {
        Operand::E(e) => e,
        Operand::Num(x) => konst(x),
    }
}

/// `Op(name, args)`, numbers made constants; an unknown op or another
/// operand count is refused.
pub fn op(name: &str, args: Vec<Operand>) -> Result<E, Refusal> {
    let Some(o) = OpName::parse(name) else {
        return Err(Refusal::new(
            "TG.PLAN.020",
            format!("no expression op {name:?}"),
        ));
    };
    if args.len() != o.arity() {
        return Err(Refusal::new(
            "TG.PLAN.020",
            format!("{name} takes {} operands, got {}", o.arity(), args.len()),
        ));
    }
    Ok(Rc::new(Expr::Op(
        o,
        args.into_iter().map(operand).collect(),
    )))
}

/// The operands `e` reads.
pub fn loads(e: &E) -> HashSet<usize> {
    match &**e {
        Expr::Load(i) => HashSet::from([*i]),
        Expr::Op(_, args) => args.iter().flat_map(loads).collect(),
        Expr::Reduce(_, x) => loads(x),
        Expr::Const(_) => HashSet::new(),
    }
}

/// The outermost reductions in `e`, one a node.
pub fn reduces(e: &E) -> Vec<E> {
    match &**e {
        Expr::Reduce(..) => vec![e.clone()],
        Expr::Op(_, args) => {
            let mut out: Vec<E> = vec![];
            for r in args.iter().flat_map(reduces) {
                if crate::mutants::on("kernels.expr/reduces_dup")
                    || !out.iter().any(|q| Rc::ptr_eq(q, &r))
                {
                    out.push(r);
                }
            }
            out
        }
        _ => vec![],
    }
}

/// Ops in the expression, each node once.
pub fn count(e: &E) -> usize {
    fn walk(x: &E, seen: &mut HashSet<*const Expr>) {
        if !matches!(&**x, Expr::Op(..) | Expr::Reduce(..)) || !seen.insert(Rc::as_ptr(x)) {
            return;
        }
        match &**x {
            Expr::Op(_, args) => args.iter().for_each(|a| walk(a, seen)),
            Expr::Reduce(_, a) => walk(a, seen),
            _ => {}
        }
    }
    let mut seen = HashSet::new();
    walk(e, &mut seen);
    seen.len()
}

/// An expression from its node table `{"nodes": [..], "root": i}` (or that
/// table under `"expr"`): `{"load": i}`, `{"const": v}` (v a number or
/// `"inf"` / `"-inf"` / `"nan"`), `{"op": name, "args": [j..]}`,
/// `{"reduce": op, "arg": j}`, each node before its users; one `Rc` a
/// node, so a node read twice stays one.
pub fn from_table(v: &serde_json::Value) -> Result<E, Refusal> {
    let bad = |what: &str| Refusal::new("API.OP.001", format!("an expression table: {what}"));
    let t = v.get("expr").unwrap_or(v);
    let nodes_in = t
        .get("nodes")
        .and_then(|n| n.as_array())
        .ok_or_else(|| bad("no nodes"))?;
    let mut nodes: Vec<E> = Vec::with_capacity(nodes_in.len());
    let at = |nodes: &[E], j: &serde_json::Value| -> Result<E, Refusal> {
        j.as_u64()
            .and_then(|j| nodes.get(j as usize).cloned())
            .ok_or_else(|| bad(&format!("node {j} before its user")))
    };
    for n in nodes_in {
        let e = if let Some(i) = n.get("load") {
            load(i.as_u64().ok_or_else(|| bad("a load index"))? as usize)
        } else if let Some(c) = n.get("const") {
            konst(match c {
                serde_json::Value::String(s) => match s.as_str() {
                    "inf" => f64::INFINITY,
                    "-inf" => f64::NEG_INFINITY,
                    "nan" => f64::NAN,
                    _ => return Err(bad(&format!("a constant {s:?}"))),
                },
                _ => c.as_f64().ok_or_else(|| bad("a constant"))?,
            })
        } else if let Some(r) = n.get("reduce") {
            let op = r
                .as_str()
                .and_then(RedOp::parse)
                .ok_or_else(|| bad("a reduction"))?;
            Rc::new(Expr::Reduce(op, at(&nodes, &n["arg"])?))
        } else if let Some(o) = n.get("op") {
            let name = o.as_str().ok_or_else(|| bad("an op name"))?;
            let args = n
                .get("args")
                .and_then(|a| a.as_array())
                .ok_or_else(|| bad("op args"))?
                .iter()
                .map(|j| at(&nodes, j).map(Operand::E))
                .collect::<Result<Vec<_>, _>>()?;
            op(name, args)?
        } else {
            return Err(bad(&format!("a node {n}")));
        };
        nodes.push(e);
    }
    at(&nodes, &t["root"])
}

/// The expression as Python prints it (`Op(name='add', args=(..))`).
pub fn py(e: &E) -> Py {
    match &**e {
        Expr::Load(i) => Py::Obj("Load", vec![("i", Py::Int(*i as i64))]),
        Expr::Const(x) => Py::Obj("Const", vec![("value", Py::Float(*x))]),
        Expr::Op(o, args) => Py::Obj(
            "Op",
            vec![
                ("name", Py::str(o.name())),
                ("args", Py::Tuple(args.iter().map(py).collect())),
            ],
        ),
        Expr::Reduce(r, x) => Py::Obj("Reduce", vec![("op", Py::str(r.name())), ("arg", py(x))]),
    }
}
