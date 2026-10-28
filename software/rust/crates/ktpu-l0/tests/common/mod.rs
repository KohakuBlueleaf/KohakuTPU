//! Shared by the test targets: machine facts from `machines/*.json`, JSON
//! values as the goldens write them, a package as JSON.

#![allow(dead_code)]

use std::path::PathBuf;

use ktpu_l0::{Buffer, Facts, Kind, Op, Package, Reloc, Step, Unit, Word};
use serde_json::{json, Value};

pub fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..")
}

/// Every tier-0 spec vector whose runner has a port, its input and expect
/// the ruled ones where a ruling supersedes it: `(file stem, vector)`, by
/// path.
pub fn ported_vectors() -> Vec<(String, Value)> {
    fn walk(dir: &std::path::Path, out: &mut Vec<PathBuf>) {
        for e in std::fs::read_dir(dir).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                walk(&p, out);
            } else if p.extension().is_some_and(|x| x == "json") {
                out.push(p);
            }
        }
    }
    let mut paths = Vec::new();
    walk(&root().join("software/tests/spec/vectors"), &mut paths);
    paths.sort();
    let names: Vec<&str> = ktpu_l0::spec::ports().iter().map(|p| p.0).collect();
    paths
        .iter()
        .map(|p| {
            let v: Value = serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap();
            (p.file_stem().unwrap().to_string_lossy().to_string(), v)
        })
        .filter(|(_, v)| v["tier"] == 0 && v["runner"].as_str().is_some_and(|r| names.contains(&r)))
        .map(|(name, mut v)| {
            let (input, expect) = ktpu_l0::spec::ruled(&name, &v["input"], &v["expect"]);
            v["input"] = input;
            v["expect"] = expect;
            (name, v)
        })
        .collect()
}

/// The port of vector `v`'s runner on its input and machine.
pub fn run_vector(v: &Value) -> Result<Value, String> {
    let runner = v["runner"].as_str().unwrap();
    let (_, port) = ktpu_l0::spec::ports()
        .into_iter()
        .find(|p| p.0 == runner)
        .unwrap();
    let m = v["machine"]
        .as_str()
        .map(|n| ktpu_machine::Machine::named(n).unwrap());
    port(&v["input"], m.as_ref())
}

/// The L0 facts of machine file `name`, mesh 0.
pub fn facts(name: &str) -> Facts {
    let m = ktpu_machine::Machine::named(name).unwrap_or_else(|e| panic!("{name}: {e}"));
    Facts::of(&m)
}

pub fn v9t6() -> Facts {
    facts("multimesh_v9t6")
}

/// A JSON int or `"0x.."` / `"-0x.."` string.
pub fn num(v: &Value) -> i128 {
    match v {
        Value::Number(n) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from))
            .unwrap_or_else(|| panic!("number {n}")),
        Value::String(s) => {
            let (neg, s) = match s.strip_prefix('-') {
                Some(r) => (true, r),
                None => (false, s.as_str()),
            };
            let x = i128::from_str_radix(s.trim_start_matches("0x"), 16)
                .unwrap_or_else(|e| panic!("{s}: {e}"));
            if neg {
                -x
            } else {
                x
            }
        }
        Value::Bool(b) => *b as i128,
        _ => panic!("not a number: {v}"),
    }
}

/// `num` as the 64-bit two's complement value Python's `& MASK64` takes.
pub fn u(v: &Value) -> u64 {
    num(v) as u64
}

pub fn word(v: &Value) -> Word {
    match v {
        Value::String(s) => Word::from_hex(s).unwrap_or_else(|| panic!("word {s}")),
        _ => Word::from_u64(u(v)),
    }
}

pub fn words(v: &Value) -> Vec<Word> {
    v.as_array().unwrap().iter().map(word).collect()
}

pub fn hx(v: i128) -> String {
    if v < 0 {
        format!("-{:#x}", -v)
    } else {
        format!("{v:#x}")
    }
}

/// A package from the goldens' normalised JSON.
pub fn package(v: &Value) -> Package {
    let arr = |k: &str| v[k].as_array().cloned().unwrap_or_default();
    Package {
        units: arr("units")
            .iter()
            .map(|e| Unit {
                type_code: u(&e[0]),
                x: u(&e[1]),
                y: u(&e[2]),
                mesh: u(&e[3]),
                credit: u(&e[4]),
                fetch: u(&e[5]),
            })
            .collect(),
        buffers: arr("buffers")
            .iter()
            .map(|b| Buffer {
                name: b["name"].as_str().unwrap().to_string(),
                size: u(&b["size"]),
                default: u(&b["default"]),
                kind: Kind::from_code(u(&b["kind"])).unwrap(),
                mesh: u(&b["mesh"]),
            })
            .collect(),
        steps: arr("steps")
            .iter()
            .map(|s| Step {
                op: Op::from_code(u(&s[0])).unwrap(),
                unit: u(&s[1]),
                count: u(&s[2]),
                arg: u(&s[3]),
                flags: u(&s[4]) as u8,
            })
            .collect(),
        relocs: arr("relocs")
            .iter()
            .map(|r| Reloc {
                payload: u(&r[0]),
                bit: u(&r[1]),
                width: u(&r[2]),
                buffer: u(&r[3]),
                addend: num(&r[4]),
                shift: u(&r[5]),
            })
            .collect(),
        payloads: arr("payloads").iter().map(word).collect(),
        signature: u(&v["signature"]),
        ack_reserve: u(&v["ack_reserve"]),
        checksum: v["checksum"].as_bool().unwrap_or(false),
        buffer_names: arr("names")
            .iter()
            .map(|n| n.as_str().unwrap().to_string())
            .collect(),
    }
}

/// A package as the goldens' normalised JSON.
pub fn package_json(p: &Package) -> Value {
    let h = |v: u64| Value::String(hx(v as i128));
    json!({
        "units": p.units.iter().map(|x| json!([
            h(x.type_code), h(x.x), h(x.y), h(x.mesh), h(x.credit), h(x.fetch)
        ])).collect::<Vec<_>>(),
        "buffers": p.buffers.iter().map(|b| json!({
            "name": b.name, "size": h(b.size), "default": h(b.default),
            "kind": h(b.kind as u64), "mesh": h(b.mesh)
        })).collect::<Vec<_>>(),
        "steps": p.steps.iter().map(|s| json!([
            h(s.op as u64), h(s.unit), h(s.count), h(s.arg), h(s.flags as u64)
        ])).collect::<Vec<_>>(),
        "relocs": p.relocs.iter().map(|r| json!([
            h(r.payload), h(r.bit), h(r.width), h(r.buffer), hx(r.addend), h(r.shift)
        ])).collect::<Vec<_>>(),
        "payloads": p.payloads.iter().map(|w| w.to_hex()).collect::<Vec<_>>(),
        "signature": h(p.signature),
        "ack_reserve": h(p.ack_reserve),
        "checksum": p.checksum,
        "names": p.buffer_names,
    })
}

pub fn hex_bytes(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

pub fn from_hex_bytes(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
