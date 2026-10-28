//! Shared by the golden tests: the goldens, steps as comparable JSON.

#![allow(dead_code)]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use ktpu_plan::Step;
use serde_json::Value;

pub fn goldens() -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/plan");
    let mut out: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    out.sort();
    out
}

/// Kernel name -> its graph as RUL.060 rules a truncation (`golden/kernels`,
/// each call's `ruled`): the plan goldens record Python's trace.
fn ruled_graphs() -> &'static HashMap<String, Value> {
    static GRAPHS: OnceLock<HashMap<String, Value>> = OnceLock::new();
    GRAPHS.get_or_init(|| {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/kernels");
        let mut out = HashMap::new();
        for e in std::fs::read_dir(&dir).unwrap().flatten() {
            let doc: Value =
                serde_json::from_str(&std::fs::read_to_string(e.path()).unwrap()).unwrap();
            for c in doc["calls"].as_array().into_iter().flatten() {
                if let Some(r) = c.get("ruled").filter(|r| r.get("graph").is_some()) {
                    out.insert(r["name"].as_str().unwrap().to_string(), r["graph"].clone());
                }
            }
        }
        out
    })
}

/// The golden's steps as the Rust stack must take them: a launch whose
/// expression truncates carries its RUL.060 graph (the launch's machine
/// kept); a truncating launch with no ruled reference panics.
pub fn ruled(steps: &[Step]) -> Vec<Step> {
    let mut out = steps.to_vec();
    for s in &mut out {
        let Step::Launch(l) = s else { continue };
        let truncates = l
            .call
            .as_ref()
            .is_some_and(|c| c.args.to_string().contains("\"op\":\"trunc\""));
        let Some(g) = l.graph.as_mut().filter(|_| truncates) else {
            continue;
        };
        let r = ruled_graphs()
            .get(&g.name)
            .unwrap_or_else(|| panic!("{}: no RUL.060 reference in golden/kernels", g.name));
        let machine = g.machine;
        *g = serde_json::from_value(r.clone()).unwrap();
        g.machine = machine;
    }
    out
}

/// [`normal`] of the golden's steps [`ruled`].
pub fn want(steps: &[Step], renamed: bool) -> Vec<Value> {
    normal(&ruled(steps), renamed)
}

/// The first place two JSON values differ, with both sides there.
pub fn first_diff(a: &Value, b: &Value) -> String {
    fn walk(a: &Value, b: &Value, path: String) -> Option<String> {
        match (a, b) {
            (Value::Object(x), Value::Object(y)) => {
                let keys: std::collections::BTreeSet<&String> = x.keys().chain(y.keys()).collect();
                keys.into_iter().find_map(|k| {
                    let (v, w) = (
                        x.get(k).unwrap_or(&Value::Null),
                        y.get(k).unwrap_or(&Value::Null),
                    );
                    walk(v, w, format!("{path}.{k}"))
                })
            }
            (Value::Array(x), Value::Array(y)) => x
                .iter()
                .zip(y)
                .enumerate()
                .find_map(|(i, (v, w))| walk(v, w, format!("{path}[{i}]")))
                .or_else(|| {
                    (x.len() != y.len())
                        .then(|| format!("{path}: {} vs {} items", x.len(), y.len()))
                }),
            _ => (a != b).then(|| {
                let cut = |v: &Value| v.to_string().chars().take(300).collect::<String>();
                format!("{path}: have {} want {}", cut(a), cut(b))
            }),
        }
    }
    walk(a, b, String::new()).unwrap_or_else(|| "no difference".into())
}

/// Every statement's `where` dropped: Python's graphs name Python lines,
/// Rust's Rust lines.
fn clear_where(v: &mut Value) {
    match v {
        Value::Object(o) => {
            o.remove("where");
            o.values_mut().for_each(clear_where);
        }
        Value::Array(a) => a.iter_mut().for_each(clear_where),
        _ => {}
    }
}

fn rename(v: &mut Value, names: &mut HashMap<String, String>) {
    match v {
        Value::String(s) if s.starts_with("tmp") || s.starts_with("raw") => {
            let n = names.len();
            let kind = s[..3].to_string();
            *s = names
                .entry(s.clone())
                .or_insert_with(|| format!("{kind}#{n}"))
                .clone();
        }
        Value::Array(a) => a.iter_mut().for_each(|x| rename(x, names)),
        Value::Object(o) => o.values_mut().for_each(|x| rename(x, names)),
        _ => {}
    }
}

/// The steps as comparable JSON: each launch's graph without `where`;
/// with `renamed`, temporaries named by first use (a run's own `tmpN` count
/// starts where the Python process's stood).
pub fn normal(steps: &[Step], renamed: bool) -> Vec<Value> {
    let mut names: HashMap<String, String> = HashMap::new();
    let mut out = Vec::new();
    for s in steps {
        let mut v = serde_json::to_value(s).unwrap();
        let graph = v
            .get_mut("launch")
            .and_then(Value::as_object_mut)
            .and_then(|l| l.remove("graph"));
        if renamed {
            rename(&mut v, &mut names);
        }
        if let Some(mut g) = graph {
            clear_where(&mut g);
            v["launch"]["graph"] = g;
        }
        out.push(v);
    }
    out
}
