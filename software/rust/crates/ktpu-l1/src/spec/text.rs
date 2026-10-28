//! `l1.parse`, `l1.module`, `l1.expand`.

use std::collections::BTreeMap;

use ktpu_machine::Machine;
use serde_json::{json, Map, Value};

use super::json::{stmt, term};
use super::Port;
use crate::lower::node::bodies;
use crate::text::{self, emit, fmt, read};

fn text_of(inp: &Value) -> Result<&str, String> {
    inp["text"]
        .as_str()
        .ok_or_else(|| "input has no text".to_string())
}

/// `{name: value}` of the input's `binds`.
pub fn binds(inp: &Value) -> Result<BTreeMap<String, i128>, String> {
    let mut out = BTreeMap::new();
    if let Some(b) = inp["binds"].as_object() {
        for (k, v) in b {
            let v = v
                .as_i64()
                .ok_or_else(|| format!("bind {k}: not an integer"))?;
            out.insert(k.clone(), v as i128);
        }
    }
    Ok(out)
}

/// `-` for no node, else the node's number.
pub fn key(n: Option<i128>) -> String {
    n.map_or_else(|| "-".into(), |v| v.to_string())
}

fn lines(text: &str) -> Value {
    json!(text.lines().collect::<Vec<_>>())
}

/// Statements of `text`; with `split`, each top statement's positional and
/// keyword arguments as a reader sees them; with `print`, its canonical text.
pub fn parse(inp: &Value, _: &Machine) -> Port {
    let got = text::parse(text_of(inp)?, "<text>").map_err(|e| e.to_string())?;
    if inp["print"].as_bool() == Some(true) {
        return Ok(json!({
            "stmts": got.iter().map(stmt).collect::<Vec<_>>(),
            "text": lines(&emit(&got)),
        }));
    }
    if inp["split"].as_bool() == Some(true) {
        let split = got.iter().map(|s| {
            let kw: Map<String, Value> = s
                .kwargs()
                .into_iter()
                .map(|(k, v)| (k.to_string(), term(v)))
                .collect();
            json!({
                "op": s.op,
                "positional": s.positional().into_iter().map(term).collect::<Vec<_>>(),
                "kwargs": kw,
            })
        });
        return Ok(Value::Array(split.collect()));
    }
    Ok(Value::Array(got.iter().map(stmt).collect()))
}

/// The module's target, fns, images and macros; with `regs`, its core build.
pub fn module(inp: &Value, _: &Machine) -> Port {
    let m = read(text_of(inp)?, "<ktpu>").map_err(|e| e.to_string())?;
    let mut out = Map::new();
    out.insert("target".into(), json!(m.target));
    if inp["regs"].as_bool() == Some(true) {
        out.insert("vregs".into(), json!(m.vregs));
        out.insert("vcopies".into(), json!(m.vcopies));
    }
    let fns = m.fns.iter().map(|((n, lv), b)| {
        let ps: Vec<Value> = b.params.iter().map(|(p, t)| json!([p, fmt(t)])).collect();
        json!([n, lv, ps, b.stmts.len()])
    });
    out.insert("fns".into(), fns.collect());
    let defs = |d: &crate::text::Definition| json!([d.name, d.params, d.stmts.len()]);
    out.insert("images".into(), m.images.values().map(defs).collect());
    out.insert("macros".into(), m.macros.values().map(defs).collect());
    Ok(Value::Object(out))
}

/// The body after read-time expansion, as canonical text, per node.
pub fn expand(inp: &Value, _: &Machine) -> Port {
    let m = read(text_of(inp)?, "<ktpu>").map_err(|e| e.to_string())?;
    let name = inp["fn"].as_str().ok_or("input has no fn")?;
    let got = bodies(&m, name, &binds(inp)?).map_err(|e| e.to_string())?;
    Ok(Value::Object(
        got.iter()
            .map(|(n, st)| (key(*n), lines(&emit(st))))
            .collect(),
    ))
}
