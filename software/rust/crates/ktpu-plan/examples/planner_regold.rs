//! The plan-golden changes a change of the planner makes. For every golden
//! (`golden/plan/*.json`) holding a product, each run (`run::execute`) and
//! `kp` log (`tensor::Session::replay`) is planned now: each step must equal
//! the golden's or be a launch that differs from it only in the keys KEYS
//! of its `MOVED` fields (`call`, `info`); an end record or refusal other
//! than the golden's aborts. With `--mutant`, each is also planned under
//! that ktpu-kernels mutant, the reverse of the change, and must reproduce
//! the golden exactly. The moved fields are written as JSON-pointer patches
//! to `OUT_DIR/patches.json` (`scripts/py/measure/mmeff/apply_patches.py`
//! applies them), the moved launches a file to `OUT_DIR/manifest.json`.
//!
//!     cargo run -p ktpu-plan --example planner_regold -- \
//!         OUT_DIR KEY[,KEY..] [--mutant MUTANT]

#[path = "../tests/common/mod.rs"]
mod common;

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use ktpu_plan::{class, lower, run, tensor::Session, Case, Step};
use serde_json::{json, Value};

/// The launch fields a change may move.
const MOVED: [&str; 2] = ["call", "info"];

/// `v` without the keys `keys` in any object below it.
fn without(v: &Value, keys: &[String]) -> Value {
    match v {
        Value::Object(o) => Value::Object(
            o.iter()
                .filter(|(k, _)| !keys.contains(k))
                .map(|(k, x)| (k.clone(), without(x, keys)))
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.iter().map(|x| without(x, keys)).collect()),
        _ => v.clone(),
    }
}

/// One recording: its steps, end records (runs) and refusal's class text.
struct Rec {
    steps: Vec<Step>,
    end: Option<BTreeMap<String, ktpu_plan::Buffer>>,
    error: Option<String>,
}

/// Every run then every `kp` entry of `case`, planned now.
fn record(case: &Case) -> Vec<Rec> {
    let m = case.machine();
    let mut out = Vec::new();
    for r in &case.runs {
        let l = lower::plan(&case.plans[r.plan].uops).expect("a run's ast plans");
        let (x, error) = match run::execute(l, &r.state, &m) {
            Ok(x) => (x, None),
            Err((e, x)) => (x, Some(class::text(&e))),
        };
        let bases: Vec<String> = r.end.keys().cloned().collect();
        out.push(Rec {
            end: Some(x.records(&bases)),
            steps: x.steps,
            error,
        });
    }
    let mut s = Session::new(&m);
    let mut names: HashMap<String, usize> = HashMap::new();
    for k in &case.kp {
        let (steps, made, error) = match s.replay(&k.op, &k.args, &names) {
            Ok(r) => (r.steps, r.out, None),
            Err((e, steps)) => (steps, None, Some(class::text(&e))),
        };
        if let (Some(name), Some(t)) = (&k.out, made) {
            names.entry(name.clone()).or_insert(t);
        }
        out.push(Rec {
            steps,
            end: None,
            error,
        });
    }
    out
}

/// The golden's own recordings, in [`record`]'s order, with their JSON
/// pointers.
fn golden(case: &Case) -> Vec<(String, Rec)> {
    let runs = case.runs.iter().enumerate().map(|(i, r)| {
        (
            format!("/runs/{i}"),
            Rec {
                steps: r.steps.clone(),
                end: Some(r.end.clone()),
                error: r.error.clone(),
            },
        )
    });
    let kp = case.kp.iter().enumerate().map(|(i, k)| {
        (
            format!("/kp/{i}"),
            Rec {
                steps: k.steps.clone(),
                end: None,
                error: k.error.clone(),
            },
        )
    });
    runs.chain(kp).collect()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (out, keys, mutant) = match &args[..] {
        [out, keys] => (out, keys, None),
        [out, keys, flag, name] if flag == "--mutant" => (out, keys, Some(name)),
        _ => panic!("usage: planner_regold OUT_DIR KEY[,KEY..] [--mutant MUTANT]"),
    };
    let mutant: Option<&'static str> = mutant.map(|m| {
        *ktpu_kernels::mutants::NAMES
            .iter()
            .chain(ktpu_kernels::mutants::LOCAL)
            .find(|n| *n == m)
            .unwrap_or_else(|| panic!("no ktpu-kernels mutant {m}"))
    });
    let keys: Vec<String> = keys.split(',').map(str::to_string).collect();
    let out = PathBuf::from(out);
    std::fs::create_dir_all(&out).unwrap();
    let (mut patches, mut manifest, mut bad) = (Vec::new(), BTreeMap::new(), Vec::new());
    for f in common::goldens() {
        let raw = std::fs::read_to_string(&f).unwrap();
        if !raw.contains("matmul.") {
            continue;
        }
        let file = f.file_name().unwrap().to_str().unwrap().to_string();
        let case: Case = serde_json::from_str(&raw).unwrap();
        let want = golden(&case);
        let before = mutant.map(|m| ktpu_kernels::mutants::with(m, || record(&case)));
        let now = record(&case);
        let mut moved = 0;
        for (i, ((at, w), n)) in want.iter().zip(&now).enumerate() {
            let renamed = at.starts_with("/runs");
            let ws = common::want(&w.steps, renamed);
            let mut fail = |why: String| bad.push(format!("{file}{at}: {why}"));
            if let (Some(m), Some(before)) = (mutant, &before) {
                let b = &before[i];
                if common::normal(&b.steps, renamed) != ws || b.end != w.end || b.error != w.error {
                    fail(format!("{m} does not reproduce the golden"));
                    continue;
                }
            }
            if n.end != w.end || n.error != w.error {
                fail(format!("refusal {:?}, recorded {:?}", n.error, w.error));
                continue;
            }
            let ns = common::normal(&n.steps, renamed);
            if ns.len() != ws.len() {
                fail(format!("{} steps, recorded {}", ns.len(), ws.len()));
                continue;
            }
            for (j, (have, was)) in ns.iter().zip(&ws).enumerate() {
                if have == was {
                    continue;
                }
                if without(have, &keys) != *was {
                    fail(format!(
                        "step {j}: {}",
                        common::first_diff(&without(have, &keys), was)
                    ));
                    continue;
                }
                let Step::Launch(l) = &n.steps[j] else {
                    fail(format!("step {j} moved and is no launch"));
                    continue;
                };
                let fields = serde_json::to_value(l.as_ref()).unwrap();
                let (mut rest, mut old) = (have["launch"].clone(), was["launch"].clone());
                for k in MOVED {
                    rest.as_object_mut().unwrap().remove(k);
                    old.as_object_mut().unwrap().remove(k);
                }
                if rest != old {
                    fail(format!("step {j} moves outside {MOVED:?}"));
                    continue;
                }
                for k in MOVED {
                    patches.push(json!({
                        "file": file,
                        "pointer": format!("{at}/steps/{j}/launch/{k}"),
                        "value": fields[k],
                    }));
                }
                moved += 1;
            }
        }
        if moved > 0 {
            manifest.insert(file, moved);
        }
    }
    assert!(bad.is_empty(), "{} refused:\n{}", bad.len(), bad.join("\n"));
    let write = |name: &str, v: &Value| {
        std::fs::write(out.join(name), serde_json::to_string_pretty(v).unwrap()).unwrap()
    };
    write("patches.json", &Value::Array(patches));
    write("manifest.json", &json!(manifest));
    println!(
        "{} plan goldens move, {} launches",
        manifest.len(),
        manifest.values().sum::<i32>()
    );
}
