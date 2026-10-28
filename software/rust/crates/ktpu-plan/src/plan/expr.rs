//! The expression a kernel computes (`kernels/expr.py`): `Load(i)`, a
//! number, an op over expressions, a row reduction. Nodes are shared by
//! `Rc`: a node reached twice through one `Rc` is one node (Python's
//! identity), two equal nodes built apart stay two.

use std::collections::HashMap;
use std::rc::Rc;

use serde_json::{json, Value};

use crate::program::{Expr as Table, ExprNode};

pub type E = Rc<Expr>;

#[derive(Debug, PartialEq)]
pub enum Expr {
    Load(usize),
    Const(f64),
    Op(String, Vec<E>),
    Reduce(String, E),
}

pub fn load(i: usize) -> E {
    Rc::new(Expr::Load(i))
}

pub fn constant(v: f64) -> E {
    Rc::new(Expr::Const(v))
}

/// `X.op`: an op node (arity checked by the generators).
pub fn op(name: &str, args: Vec<E>) -> E {
    Rc::new(Expr::Op(name.to_string(), args))
}

pub fn reduce(op: &str, arg: E) -> E {
    Rc::new(Expr::Reduce(op.to_string(), arg))
}

fn key(e: &E) -> usize {
    Rc::as_ptr(e) as usize
}

/// The node table (`emit.expr`): nodes by identity, depth first, each
/// before its users; `ids` maps each node's address to its index.
pub fn table(e: &E, ids: &mut HashMap<usize, usize>) -> Table {
    let mut nodes = Vec::new();
    let root = walk(e, &mut nodes, ids);
    Table { nodes, root }
}

fn walk(e: &E, nodes: &mut Vec<ExprNode>, ids: &mut HashMap<usize, usize>) -> usize {
    if let Some(i) = ids.get(&key(e)) {
        return *i;
    }
    let node = match e.as_ref() {
        Expr::Load(i) => ExprNode::Load { load: *i },
        Expr::Const(v) => ExprNode::Const {
            r#const: number(*v),
        },
        Expr::Op(name, args) => ExprNode::Op {
            op: name.clone(),
            args: args.iter().map(|a| walk(a, nodes, ids)).collect(),
        },
        Expr::Reduce(op, arg) => ExprNode::Reduce {
            reduce: op.clone(),
            arg: walk(arg, nodes, ids),
        },
    };
    let i = nodes.len();
    nodes.push(node);
    ids.insert(key(e), i);
    i
}

/// A float as JSON: a number, or its name.
pub fn number(v: f64) -> Value {
    if v.is_nan() {
        json!("nan")
    } else if v.is_infinite() {
        json!(if v > 0.0 { "inf" } else { "-inf" })
    } else {
        json!(v)
    }
}

/// The outermost reductions (`X.reduces`), a shared node once.
pub fn reduces(e: &E) -> Vec<E> {
    match e.as_ref() {
        Expr::Reduce(..) => vec![e.clone()],
        Expr::Op(_, args) => {
            let found: Vec<E> = args.iter().flat_map(reduces).collect();
            let mut out: Vec<E> = Vec::new();
            for r in found {
                if !out.iter().any(|q| Rc::ptr_eq(q, &r)) {
                    out.push(r);
                }
            }
            out
        }
        _ => Vec::new(),
    }
}

/// Every `Load` index `e` reads (`X.loads`).
pub fn loads(e: &E) -> Vec<usize> {
    let mut out = Vec::new();
    fn go(e: &E, out: &mut Vec<usize>) {
        match e.as_ref() {
            Expr::Load(i) => {
                if !out.contains(i) {
                    out.push(*i)
                }
            }
            Expr::Op(_, a) => a.iter().for_each(|x| go(x, out)),
            Expr::Reduce(_, a) => go(a, out),
            Expr::Const(_) => {}
        }
    }
    go(e, &mut out);
    out
}
