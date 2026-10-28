//! JSON values as the spec vectors and the goldens write them, and a package
//! described as the `l0` runners describe it.

use serde_json::{json, Value};

use crate::engine;
use crate::format::{type_code, type_name, Buffer, Kind, Op, Package, Reloc, Step, Unit};
use crate::word::Word;

/// A JSON int, bool, or `"0x.."` / `"-0x.."` / decimal string; None for
/// anything else.
pub fn try_num(v: &Value) -> Option<i128> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from)),
        Value::String(s) => {
            let (neg, s) = match s.strip_prefix('-') {
                Some(r) => (true, r),
                None => (false, s.as_str()),
            };
            let x = match s.strip_prefix("0x") {
                Some(h) => i128::from_str_radix(h, 16).ok()?,
                None => s.parse().ok()?,
            };
            Some(if neg { -x } else { x })
        }
        Value::Bool(b) => Some(*b as i128),
        _ => None,
    }
}

pub fn num(v: &Value) -> Result<i128, String> {
    try_num(v).ok_or_else(|| format!("not a number: {v}"))
}

/// `num` as the 64-bit two's complement value Python's `& MASK64` takes.
pub fn u(v: &Value) -> Result<u64, String> {
    num(v).map(|x| x as u64)
}

/// `u`, or `d` when `v` is absent or null.
pub fn u_or(v: Option<&Value>, d: u64) -> Result<u64, String> {
    match v {
        None | Some(Value::Null) => Ok(d),
        Some(x) => u(x),
    }
}

pub fn word(v: &Value) -> Result<Word, String> {
    match v {
        Value::String(s) if s.starts_with("0x") => {
            Word::from_hex(s).ok_or_else(|| format!("not a word: {s}"))
        }
        _ => u(v).map(Word::from_u64),
    }
}

pub fn arr(v: &Value) -> Result<&Vec<Value>, String> {
    v.as_array().ok_or_else(|| format!("not a list: {v}"))
}

pub fn words(v: &Value) -> Result<Vec<Word>, String> {
    arr(v)?.iter().map(word).collect()
}

pub fn pairs(v: &Value) -> Result<Vec<(u64, u64)>, String> {
    arr(v)?.iter().map(|p| Ok((u(&p[0])?, u(&p[1])?))).collect()
}

pub fn coord(v: &Value) -> Result<(u32, u32), String> {
    Ok((u(&v[0])? as u32, u(&v[1])? as u32))
}

pub fn flag(v: Option<&Value>, d: bool) -> bool {
    v.and_then(Value::as_bool).unwrap_or(d)
}

pub fn hx(v: i128) -> String {
    if v < 0 {
        format!("-{:#x}", -v)
    } else {
        format!("{v:#x}")
    }
}

pub fn hex_bytes(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

pub fn from_hex_bytes(s: &str) -> Result<Vec<u8>, String> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).map_err(|e| e.to_string()))
        .collect()
}

/// `raw` as 32-byte lines, each the hex of its bytes in memory order.
pub fn lines(raw: &[u8]) -> Vec<String> {
    raw.chunks(32).map(hex_bytes).collect()
}

/// A package from the runners' JSON description (`runners/l0.py:package`).
pub fn package(d: &Value) -> Result<Package, String> {
    let list = |k: &str| {
        d.get(k)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
    };
    let kind = |t: &Value| -> Result<u64, String> {
        match t.as_str() {
            Some(s) => type_code(s).map_err(|e| e.to_string()),
            None => u(t),
        }
    };
    let units = list("units")
        .iter()
        .map(|x| {
            Ok(Unit {
                type_code: kind(&x["type"])?,
                x: u(&x["x"])?,
                y: u(&x["y"])?,
                mesh: u_or(x.get("mesh"), 0)?,
                credit: u_or(x.get("credit"), 0)?,
                fetch: u_or(x.get("fetch"), 0)?,
            })
        })
        .collect::<Result<_, String>>()?;
    let buffers = list("buffers")
        .iter()
        .map(|b| {
            let k = b.get("kind").and_then(Value::as_str).unwrap_or("TEMP");
            Ok(Buffer {
                name: b["name"].as_str().unwrap_or_default().to_string(),
                size: u(&b["size"])?,
                default: u_or(b.get("default"), 0)?,
                kind: Kind::from_name(k).ok_or(format!("kind {k}"))?,
                mesh: u_or(b.get("mesh"), 0)?,
            })
        })
        .collect::<Result<_, String>>()?;
    let steps = list("steps")
        .iter()
        .map(|s| {
            let name = s["op"].as_str().unwrap_or_default();
            Ok(Step::new(
                Op::from_name(name).ok_or(format!("op {name}"))?,
                u_or(s.get("unit"), 0)?,
                u_or(s.get("count"), 0)?,
                u_or(s.get("arg"), 0)?,
                u_or(s.get("flags"), 0)? as u8,
            ))
        })
        .collect::<Result<_, String>>()?;
    let relocs = list("relocs")
        .iter()
        .map(|r| {
            Ok(Reloc {
                payload: u(&r["payload"])?,
                bit: u(&r["bit"])?,
                width: u(&r["width"])?,
                buffer: u(&r["buffer"])?,
                addend: r.get("addend").map(num).transpose()?.unwrap_or(0),
                shift: u_or(r.get("shift"), 0)?,
            })
        })
        .collect::<Result<_, String>>()?;
    let payloads = match d.get("payloads") {
        Some(Value::Number(n)) => vec![Word::ZERO; n.as_u64().unwrap_or(0) as usize],
        Some(v) => words(v)?,
        None => Vec::new(),
    };
    Ok(Package {
        units,
        buffers,
        steps,
        relocs,
        payloads,
        signature: u_or(d.get("signature"), 0)?,
        ack_reserve: u_or(d.get("ack_reserve"), 0)?,
        checksum: flag(d.get("checksum"), false),
        buffer_names: Vec::new(),
    })
}

pub fn step_dict(s: &Step) -> Value {
    json!({
        "op": s.op.name(), "unit": s.unit, "count": s.count,
        "arg": hx(s.arg as i128), "flags": s.flags,
    })
}

/// A package as JSON, every field by name (`runners/l0.py:describe`).
pub fn describe(p: &Package) -> Value {
    json!({
        "units": p.units.iter().map(|x| json!({
            "type": type_name(x.type_code), "x": x.x, "y": x.y, "mesh": x.mesh,
            "credit": x.credit, "fetch": hx(x.fetch as i128),
        })).collect::<Vec<_>>(),
        "buffers": p.buffers.iter().map(|b| json!({
            "size": b.size, "default": hx(b.default as i128), "kind": b.kind.name(),
            "mesh": b.mesh,
        })).collect::<Vec<_>>(),
        "steps": p.steps.iter().map(step_dict).collect::<Vec<_>>(),
        "relocs": p.relocs.iter().map(|r| json!({
            "payload": r.payload, "bit": r.bit, "width": r.width, "buffer": r.buffer,
            "addend": r.addend as i64, "shift": r.shift,
        })).collect::<Vec<_>>(),
        "payloads": p.payloads.iter().map(|w| w.to_hex()).collect::<Vec<_>>(),
        "signature": hx(p.signature as i128),
        "ack_reserve": p.ack_reserve,
        "checksum": p.checksum,
    })
}

/// An ENGINE step's entries and the unused slots of its last payload.
pub fn engine_entries(p: &Package, s: &Step) -> Value {
    let out: Vec<Value> = engine::entries(p, s)
        .iter()
        .map(|&(c, v)| json!([c, hx(v as i128)]))
        .collect();
    let mut pad = Vec::new();
    if !s.count.is_multiple_of(3) {
        let w = p.payloads[(s.arg + s.count / 3) as usize];
        for j in (s.count % 3) as u32..3 {
            pad.push(json!([
                w.get(8 * j, 8),
                hx(w.get(64 * (j + 1), 64) as i128)
            ]));
        }
    }
    json!({"first": s.arg, "entries": out, "pad": pad})
}

/// The Python exception class a refusal stands for in the runners.
pub fn refused(r: &crate::Refusal) -> Value {
    let class = if r.clause == "L0.PKG.017" {
        "ValueError"
    } else {
        "PackageError"
    };
    json!({ "refused": class })
}
