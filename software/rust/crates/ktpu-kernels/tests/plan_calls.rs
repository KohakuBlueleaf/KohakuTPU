//! The planner goldens' launches (`golden/plan/*.json`, `plan_dump.py`):
//! each recorded generator call made again through [`ktpu_kernels::call`]
//! gives the kernel the Python launch traced (name and graph, `where`
//! aside).

mod common;

use common::{first_diff, machine, without_where};
use ktpu_graph as g;
use serde_json::Value;
use std::path::PathBuf;

fn launches<'a>(v: &'a Value, out: &mut Vec<&'a Value>) {
    match v {
        Value::Object(m) => {
            if let Some(l) = m.get("launch") {
                if l.get("call").is_some() && l.get("graph").is_some() {
                    out.push(l);
                }
            }
            m.values().for_each(|x| launches(x, out));
        }
        Value::Array(a) => a.iter().for_each(|x| launches(x, out)),
        _ => {}
    }
}

/// Kernel name -> its ruled graph in `golden/kernels` (RUL.060 changes a
/// truncation, which the planner goldens record as Python traced it).
fn ruled() -> std::collections::HashMap<String, Value> {
    let mut out = std::collections::HashMap::new();
    for e in std::fs::read_dir(common::golden_dir()).unwrap().flatten() {
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(e.path()).unwrap()).unwrap();
        for c in doc["calls"].as_array().into_iter().flatten() {
            if let Some(r) = c.get("ruled").filter(|r| r.get("graph").is_some()) {
                out.insert(r["name"].as_str().unwrap().to_string(), r["graph"].clone());
            }
        }
    }
    out
}

fn has_trunc(args: &Value) -> bool {
    args.to_string().contains("\"op\":\"trunc\"")
}

#[test]
fn every_planner_launch_matches_python() {
    let ruled = ruled();
    let mut n_ruled = 0;
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden/plan");
    let Ok(read) = std::fs::read_dir(&dir) else {
        panic!("no planner goldens at {}", dir.display());
    };
    let mut files: Vec<_> = read.flatten().map(|e| e.path()).collect();
    files.retain(|p| p.extension().is_some_and(|x| x == "json"));
    files.sort();
    let (mut total, mut fails) = (0, vec![]);
    for path in &files {
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let Some(board) = doc.get("machine").and_then(Value::as_str) else {
            continue;
        };
        let mut ls = vec![];
        launches(&doc, &mut ls);
        for l in ls {
            total += 1;
            let mut want: g::Graph = serde_json::from_value(l["graph"].clone()).unwrap();
            if has_trunc(&l["call"]["args"]) {
                n_ruled += 1;
                match ruled.get(&want.name) {
                    Some(r) => {
                        // The reference may be another board's trace: the
                        // launch's machine stays the planner golden's.
                        let machine = want.machine;
                        want = serde_json::from_value(r.clone()).unwrap();
                        want.machine = machine;
                    }
                    None => {
                        fails.push(format!("{}: no RUL.060 reference", want.name));
                        continue;
                    }
                }
            }
            let m = machine(board, want.machine.nodes);
            let gen = l["call"]["gen"].as_str().unwrap_or_default();
            let why = match ktpu_kernels::call(gen, &l["call"]["args"], &m) {
                Err(e) => Some(format!("refused: {e}")),
                Ok(k) => {
                    if gen == "vector.flat_kernel" {
                        let n = l["call"]["args"]["n"].as_i64().unwrap();
                        if n % m.vc.vlmax as i64 != 0 {
                            for port in &k.graph.ports {
                                let item = if port.spec.dtype == g::Dtype::F32 {
                                    4
                                } else {
                                    2
                                };
                                assert_eq!(
                                    port.spec.shape.iter().product::<i64>() * item,
                                    (n * item + 31) / 32 * 32,
                                    "RUL.020"
                                );
                            }
                            continue;
                        }
                    }
                    let (w, got) = (without_where(want), without_where(k.graph));
                    (w != got).then(|| first_diff(&w, &got))
                }
            };
            if let Some(why) = why {
                fails.push(format!(
                    "{} {gen}: {why}",
                    path.file_name().unwrap().to_string_lossy()
                ));
            }
        }
    }
    eprintln!(
        "planner launches: {} / {total} match ({n_ruled} against RUL.060's graph)",
        total - fails.len()
    );
    for f in fails.iter().take(20) {
        eprintln!("FAIL {f}");
    }
    assert!(total > 0, "no launch in the planner goldens");
    assert!(
        fails.is_empty(),
        "{} of {total} launches differ",
        fails.len()
    );
}
