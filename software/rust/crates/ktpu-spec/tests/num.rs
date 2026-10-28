//! The num.* ports give the same answers as the Python runners did: each
//! frozen vector's answer has the sha256 `golden/num/answers.json` froze
//! from the Python runner before its port (`golden/num_dump.py`, the
//! answer's canonical JSON as Python's `json.dumps(sort_keys=True,
//! separators=(",", ":"))` writes it). A ruling vector's answer is its
//! `expect`, which the Python runner does not give yet.

#![cfg(feature = "stack")]

use ktpu_kernels::pyrepr::float_repr;
use ktpu_machine::Machine;
use ktpu_spec::{runners, vectors};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::PathBuf;

fn answers() -> serde_json::Map<String, Value> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden/num/answers.json");
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str::<Value>(&text)
        .unwrap()
        .as_object()
        .unwrap()
        .clone()
}

/// `v` as Python's `json.dumps(v, sort_keys=True, separators=(",", ":"))`.
fn canonical(v: &Value) -> String {
    match v {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => i.to_string(),
            (_, Some(u)) => u.to_string(),
            _ => float_repr(n.as_f64().unwrap()),
        },
        Value::String(s) => serde_json::to_string(s).unwrap(),
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(canonical).collect::<Vec<_>>().join(",")
        ),
        Value::Object(o) => {
            let mut keys: Vec<&String> = o.keys().collect();
            keys.sort();
            let items: Vec<String> = keys
                .iter()
                .map(|k| {
                    format!(
                        "{}:{}",
                        serde_json::to_string(k).unwrap(),
                        canonical(&o[*k])
                    )
                })
                .collect();
            format!("{{{}}}", items.join(","))
        }
    }
}

fn sha256(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

#[test]
fn canonical_json_is_pythons() {
    let v: Value =
        serde_json::from_str(r#"{"b": [2.0, 1e-05, 16384.0, -0.0, 3], "a": "x"}"#).unwrap();
    assert_eq!(canonical(&v), r#"{"a":"x","b":[2.0,1e-05,16384.0,-0.0,3]}"#);
}

#[test]
fn each_ported_num_vector_answers_as_the_python_runner_did() {
    let frozen = answers();
    let suites = runners::by_runner();
    let all = vectors::load(&vectors::root()).expect("vectors");
    let mut bad = Vec::new();
    let mut checked = 0;
    for v in all
        .iter()
        .filter(|v| v.tier == 0 && v.runner.starts_with("num."))
    {
        let Some(s) = suites.get(v.runner.as_str()) else {
            continue;
        };
        let m = v.machine.as_deref().map(|n| Machine::named(n).unwrap());
        let got = match (s.run)(&v.runner, &v.input, m.as_ref(), None) {
            Ok(g) => g,
            Err(e) => {
                bad.push(format!("{}: {e}", v.id));
                continue;
            }
        };
        checked += 1;
        if v.status == "ruling" {
            if got != v.expect {
                bad.push(format!("{} (ruling): got {got}, want {}", v.id, v.expect));
            }
            continue;
        }
        let Some(want) = frozen.get(&v.id) else {
            bad.push(format!("{}: no frozen answer", v.id));
            continue;
        };
        let text = canonical(&got);
        if sha256(&text) != want["sha256"] {
            bad.push(format!(
                "{}: {text} is not the Python runner's answer",
                v.id
            ));
        }
    }
    println!("{checked} ported num vectors checked");
    assert!(bad.is_empty(), "\n{}", bad.join("\n"));
}
