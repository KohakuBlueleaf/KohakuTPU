//! Shared by the tests: the goldens' argument decoding, machines, graph
//! comparison.

#![allow(dead_code)]

use ktpu_graph as g;
use ktpu_kernels::expr::{self, Expr, OpName, RedOp, E};
use ktpu_kernels::gen::lanes;
use ktpu_kernels::{Kind, Mm, Operand};
use ktpu_machine::Machine;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::PathBuf;
use std::rc::Rc;

pub fn golden_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden/kernels")
}

/// Machine file `board` as a card of `nodes` nodes.
pub fn machine(board: &str, nodes: u32) -> Machine {
    Machine::named(board)
        .expect("a machine file")
        .with_nodes(nodes)
        .expect("a node count")
}

pub fn dtype(s: &str) -> g::Dtype {
    serde_json::from_value(Value::String(s.to_string())).expect("a dtype")
}

pub fn int(v: &Value) -> i64 {
    v.as_i64().unwrap_or_else(|| panic!("an int, got {v}"))
}

pub fn num(v: &Value) -> f64 {
    match v {
        Value::String(s) => match s.as_str() {
            "inf" => f64::INFINITY,
            "-inf" => f64::NEG_INFINITY,
            _ => f64::NAN,
        },
        _ => v.as_f64().expect("a number"),
    }
}

pub fn expr_of(v: &Value) -> E {
    let d = &v["expr"];
    let mut nodes: Vec<E> = vec![];
    for n in d["nodes"].as_array().expect("nodes") {
        let e = if let Some(i) = n.get("load") {
            expr::load(int(i) as usize)
        } else if let Some(c) = n.get("const") {
            expr::konst(num(c))
        } else if let Some(r) = n.get("reduce") {
            let op = RedOp::parse(r.as_str().expect("a name")).expect("a reduction");
            Rc::new(Expr::Reduce(op, nodes[int(&n["arg"]) as usize].clone()))
        } else {
            let op = OpName::parse(n["op"].as_str().expect("a name")).expect("an op");
            let args = n["args"]
                .as_array()
                .expect("args")
                .iter()
                .map(|a| nodes[int(a) as usize].clone())
                .collect();
            Rc::new(Expr::Op(op, args))
        };
        nodes.push(e);
    }
    nodes[int(&d["root"]) as usize].clone()
}

pub fn operand_of(v: &Value) -> Operand {
    let o = &v["operand"];
    Operand {
        kind: Kind::parse(o["kind"].as_str().expect("a kind")).expect("a kind"),
        dtype: dtype(o["dtype"].as_str().expect("a dtype")),
        offset: int(&o["offset"]),
        pitch: int(&o["pitch"]),
        rows: int(&o["rows"]),
    }
}

pub fn operands_of(v: &Value) -> Vec<Operand> {
    v.as_array()
        .expect("operands")
        .iter()
        .map(operand_of)
        .collect()
}

pub fn mm_of(v: &Value) -> Mm {
    let m = &v["mm"];
    Mm {
        bt: int(&m["bt"]),
        m: int(&m["m"]),
        n: int(&m["n"]),
        k: int(&m["k"]),
        mp: int(&m["mp"]),
        kp: int(&m["kp"]),
        tm: int(&m["tm"]),
        tn: int(&m["tn"]),
        nk: int(&m["nk"]),
        shared: m["shared"].as_bool().expect("a bool"),
        quantum: int(&m["quantum"]),
    }
}

pub fn walk_of(v: &Value) -> Vec<(i64, i64)> {
    v.as_array()
        .expect("a walk")
        .iter()
        .map(|l| (int(&l[0]), int(&l[1])))
        .collect()
}

pub fn sha(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// A lane relayout plan as the goldens summarise it.
pub fn lanes_summary(pl: &lanes::Plan) -> Value {
    let index: Vec<u8> = pl.index.iter().flat_map(|x| x.to_le_bytes()).collect();
    let masks: Vec<u8> = pl.masks.iter().flat_map(|x| x.to_le_bytes()).collect();
    serde_json::json!({
        "classes": pl.classes.iter().map(|c| serde_json::json!([
            c.taps.iter().map(|(r, m)| [*r, *m]).collect::<Vec<_>>(), c.words, c.keep
        ])).collect::<Vec<_>>(),
        "src_words": pl.src_words,
        "dst_words": pl.dst_words,
        "fill": pl.fill,
        "index_len": pl.index.len(),
        "index_sha256": sha(&index),
        "masks_shape": [pl.mask_rows, pl.masks.len() as i64 / pl.mask_rows],
        "masks_sha256": sha(&masks),
    })
}

/// A graph with every statement's `where` cleared: the Python goldens
/// name Python lines, the Rust graphs Rust lines.
pub fn without_where(mut gr: g::Graph) -> g::Graph {
    fn clear(body: &mut [g::Stmt]) {
        for s in body {
            s.at.clear();
            match &mut s.node {
                g::Node::Par { body, .. }
                | g::Node::For { body, .. }
                | g::Node::When { body, .. }
                | g::Node::Sticky { body } => clear(body),
                _ => {}
            }
        }
    }
    clear(&mut gr.body);
    gr
}

/// The first place two graphs differ, for a failure message.
pub fn first_diff(want: &g::Graph, got: &g::Graph) -> String {
    let (a, b) = (
        serde_json::to_value(want).expect("json"),
        serde_json::to_value(got).expect("json"),
    );
    fn walk(a: &Value, b: &Value, path: String) -> Option<String> {
        match (a, b) {
            (Value::Object(x), Value::Object(y)) => {
                for (k, v) in x {
                    let w = y.get(k).unwrap_or(&Value::Null);
                    if let Some(d) = walk(v, w, format!("{path}.{k}")) {
                        return Some(d);
                    }
                }
                None
            }
            (Value::Array(x), Value::Array(y)) => {
                for (i, (v, w)) in x.iter().zip(y).enumerate() {
                    if let Some(d) = walk(v, w, format!("{path}[{i}]")) {
                        return Some(d);
                    }
                }
                (x.len() != y.len()).then(|| format!("{path}: {} vs {} items", x.len(), y.len()))
            }
            _ => (a != b).then(|| format!("{path}: want {a} got {b}")),
        }
    }
    walk(&a, &b, String::new()).unwrap_or_else(|| "no difference".into())
}
