//! Shared by the tests: the goldens' argument decoding, machines, graph
//! comparison.

#![allow(dead_code)]

use ktpu_graph as g;
use ktpu_kernels::expr::{self, Expr, OpName, RedOp, E};
use ktpu_kernels::gen::{convert, lanes, matmul, transpose, vector};
use ktpu_kernels::{Kernel, Kind, Mm, Operand, Side};
use ktpu_machine::{Machine, Refusal};
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

pub enum Got {
    Kernel(Kernel, Option<i64>),
    Result(Value),
}

pub fn s(v: &Value) -> &str {
    v.as_str().expect("a string")
}

/// The Rust generator for one recorded call.
pub fn run(call: &Value, plans: &[&Value], m: &Machine) -> Result<Got, Refusal> {
    let a = &call["args"];
    let k = |k: Kernel| Got::Kernel(k, None);
    Ok(match s(&call["fn"]) {
        "vector.rows_kernel" => {
            let bands = a["bands"].as_i64();
            let (kern, nblk) = vector::rows_kernel(
                &expr_of(&a["e"]),
                &operands_of(&a["operands"]),
                int(&a["R"]),
                int(&a["W"]),
                &operand_of(&a["out"]),
                bands,
                m,
            )?;
            Got::Kernel(kern, Some(nblk))
        }
        "vector.flat_kernel" => {
            let dtypes: Vec<g::Dtype> = a["dtypes"]
                .as_array()
                .expect("dtypes")
                .iter()
                .map(|d| dtype(s(d)))
                .collect();
            let scalars: Vec<(usize, i64)> = a["scalars"]
                .as_array()
                .expect("scalars")
                .iter()
                .map(|p| (int(&p[0]) as usize, int(&p[1])))
                .collect();
            k(vector::flat_kernel(
                &expr_of(&a["e"]),
                &dtypes,
                int(&a["n"]),
                dtype(s(&a["out_dtype"])),
                &scalars,
                m,
            )?)
        }
        "matmul.quant_kernel" => {
            let side = if s(&a["side"]) == "a" {
                Side::A
            } else {
                Side::B
            };
            k(matmul::quant_kernel(
                &operand_of(&a["src"]),
                int(&a["rows"]),
                int(&a["cols"]),
                int(&a["rp"]),
                int(&a["cp"]),
                side,
                int(&a["g"]),
                int(&a["nk"]),
                int(&a["batches"]),
                m,
            )?)
        }
        "matmul.mm_kernel" => k(matmul::mm_kernel(
            &mm_of(&a["mm"]),
            &expr_of(&a["e"]),
            &operands_of(&a["extras"]),
            &operand_of(&a["out"]),
            &matmul::unrecorded().read(a, matmul::Group::Kernel)?,
            m,
        )?),
        "convert.copy_kernel" => k(convert::copy_kernel(int(&a["nbytes"]), m)?),
        "convert.walk_kernel" => k(convert::walk_kernel(
            int(&a["src_bytes"]),
            &walk_of(&a["levels"]),
            int(&a["at"]),
            int(&a["dst_bytes"]),
            &walk_of(&a["dlevels"]),
            int(&a["dat"]),
            m,
        )?),
        "convert.fill_kernel" => k(convert::fill_kernel(
            int(&a["dst_bytes"]),
            &walk_of(&a["dlevels"]),
            int(&a["dat"]),
            int(&a["value"]),
            int(&a["ew"]),
            m,
        )?),
        "convert.cast_kernel" => k(convert::cast_kernel(
            int(&a["n"]),
            dtype(s(&a["src"])),
            dtype(s(&a["dst"])),
            m,
        )?),
        "transpose.transpose_kernel" => k(transpose::transpose_kernel(
            int(&a["nb"]),
            int(&a["A"]),
            int(&a["B"]),
            m,
        )?),
        "transpose.reverse_kernel" => k(transpose::reverse_kernel(
            int(&a["src_bytes"]),
            &walk_of(&a["levels"]),
            int(&a["at"]),
            int(&a["n"]),
            m,
        )?),
        "lanes.relayout_kernel" => {
            let want = &a["pl"]["lanes_plan"];
            let pl = plans
                .iter()
                .find_map(|p| {
                    let pl = lanes_plan(&p["args"], m).ok()?;
                    (lanes_summary(&pl) == *want).then_some(pl)
                })
                .ok_or_else(|| Refusal::new("golden", "no lanes.plan call gives this plan"))?;
            k(lanes::relayout_kernel(&pl, m)?)
        }
        "matmul.plan" => {
            let mm = matmul::plan(
                int(&a["bt"]),
                int(&a["m"]),
                int(&a["n"]),
                int(&a["k"]),
                a["shared"].as_bool().expect("a bool"),
                int(&a["quantum"]),
                ktpu_machine::PAIRING,
                m,
                &matmul::unrecorded().read(a, matmul::Group::Plan)?,
            )?;
            Got::Result(serde_json::json!({"mm": {
                "bt": mm.bt, "m": mm.m, "n": mm.n, "k": mm.k, "mp": mm.mp, "kp": mm.kp,
                "tm": mm.tm, "tn": mm.tn, "nk": mm.nk, "shared": mm.shared, "quantum": mm.quantum,
            }}))
        }
        "lanes.plan" => {
            Got::Result(serde_json::json!({"lanes_plan": lanes_summary(&lanes_plan(a, m)?)}))
        }
        "transpose.masks" => {
            let masks = transpose::masks(m)?;
            let bits: Vec<u8> = masks.iter().flat_map(|x| x.to_le_bytes()).collect();
            let vlmax = ktpu_kernels::gen::Hw::of(m).vlmax;
            Got::Result(serde_json::json!({"array": {
                "shape": [masks.len() as i64 / vlmax, vlmax], "dtype": "float16", "sha256": sha(&bits),
            }}))
        }
        other => panic!("no Rust generator for {other}"),
    })
}

pub fn lanes_plan(a: &Value, m: &Machine) -> Result<lanes::Plan, Refusal> {
    lanes::plan(
        int(&a["src_n"]),
        &walk_of(&a["walk"]),
        int(&a["at"]),
        &walk_of(&a["dwalk"]),
        int(&a["dat"]),
        int(&a["m"]),
        a["keep"].as_bool().expect("a bool"),
        m,
    )
}
