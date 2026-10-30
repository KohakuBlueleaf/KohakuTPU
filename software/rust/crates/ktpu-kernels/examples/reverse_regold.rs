//! Rewrite the kernel and planner goldens (`golden/kernels`, `golden/plan`)
//! a generator change moves, each proven to be moved by that change alone: a
//! call traces differently with and without the change's reverse mutant, and
//! under the mutant it traces to the golden as held (each statement's `where`
//! aside), on the call's machine and, for a kernel golden, on two nodes; the
//! name and the given specs do not move. Audit by default; `--write`
//! rewrites the moved graphs in place.
//!
//!     cargo run -p ktpu-kernels --features mutants --example reverse_regold -- MUTANT [--write]

#[path = "../tests/common/mod.rs"]
mod common;

use common::*;
use ktpu_graph as g;
use ktpu_kernels::mutants::{with, LOCAL, NAMES};
use ktpu_machine::{Machine, Refusal};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

type Moved = Result<Option<g::Graph>, String>;

/// `now` against `old` (the mutant's) and the held graph: None where the
/// change does not move it, else the new graph, `where` cleared.
fn regolded(now: g::Graph, old: g::Graph, held: &Value) -> Moved {
    let (now, old) = (without_where(now), without_where(old));
    if now == old {
        return Ok(None);
    }
    let held = without_where(serde_json::from_value(held.clone()).map_err(|e| e.to_string())?);
    if old != held {
        return Err(format!("not the mutant's: {}", first_diff(&held, &old)));
    }
    if now.name != held.name || now.ports != held.ports {
        return Err("the change moves the name or the ports".into());
    }
    Ok(Some(now))
}

/// A call's kernel, or its planning result, or its refusal.
fn traced(got: Result<Got, Refusal>) -> Result<Result<ktpu_kernels::Kernel, Value>, String> {
    match got {
        Ok(Got::Kernel(k, _)) => Ok(Ok(k)),
        Ok(Got::Result(v)) => Ok(Err(v)),
        Err(e) => Err(e.to_string()),
    }
}

/// One kernel golden call: its moved graph and two-node graph.
fn kernel_call(
    call: &Value,
    plans: &[&Value],
    mutant: &'static str,
) -> Result<Vec<(&'static str, g::Graph)>, String> {
    let mrec = &call["machine"];
    let m = machine(s(&mrec["board"]), int(&mrec["nodes"]) as u32);
    let mut out = vec![];
    for (field, m) in [
        ("graph", m.clone()),
        ("nodes2", m.with_nodes(2).expect("two nodes")),
    ] {
        let (now, old) = (
            traced(run(call, plans, &m)),
            traced(with(mutant, || run(call, plans, &m))),
        );
        let (now, old) = match (now, old) {
            (Ok(Ok(a)), Ok(Ok(b))) => (a, b),
            (Ok(Err(a)), Ok(Err(b))) if a == b => continue,
            (Err(a), Err(b)) if a == b => continue,
            (a, b) => {
                return Err(format!(
                    "{field}: the change moves the outcome: {:?} -> {:?}",
                    b.err(),
                    a.err()
                ))
            }
        };
        if call.get("ruled").is_some() || !call[field].is_object() {
            if without_where(now.graph) != without_where(old.graph) {
                return Err(format!("{field}: a ruled or refused call moves"));
            }
            continue;
        }
        if matches!(s(&call["fn"]), "vector.flat_kernel" | "convert.cast_kernel")
            && int(&call["args"]["n"]) % (m.vc.vlmax as i64) != 0
        {
            // RUL.020: golden.rs holds such a call to its ports only.
            if now.graph.ports != old.graph.ports {
                return Err(format!(
                    "{field}: the change moves a word-extent call's ports"
                ));
            }
            continue;
        }
        let given = now.given.iter().cloned().collect::<BTreeMap<_, _>>();
        if let Some(gr) =
            regolded(now.graph, old.graph, &call[field]).map_err(|e| format!("{field}: {e}"))?
        {
            let held: BTreeMap<String, g::Spec> =
                serde_json::from_value(call["given"].clone()).map_err(|e| e.to_string())?;
            if given != held {
                return Err(format!("{field}: the change moves the given specs"));
            }
            out.push((field, gr));
        }
    }
    Ok(out)
}

fn kernels(dir: &Path, mutant: &'static str, write: bool, refused: &mut Vec<String>) -> usize {
    let mut moved = 0;
    for path in jsons(dir) {
        let mut doc: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let calls = doc["calls"].as_array().unwrap().clone();
        let plans: Vec<&Value> = calls.iter().filter(|c| c["fn"] == "lanes.plan").collect();
        let mut changed = false;
        for (i, call) in calls.iter().enumerate() {
            let name = format!(
                "{} #{i} {}",
                path.file_name().unwrap().to_string_lossy(),
                s(&call["fn"])
            );
            match kernel_call(call, &plans, mutant) {
                Ok(fields) => {
                    for (field, gr) in fields {
                        println!("{name} {field}");
                        doc["calls"][i][field] = serde_json::to_value(&gr).unwrap();
                        moved += 1;
                        changed = true;
                    }
                }
                Err(e) => refused.push(format!("{name}: {e}")),
            }
        }
        if write && changed {
            std::fs::write(&path, serde_json::to_string_pretty(&doc).unwrap() + "\n").unwrap();
        }
    }
    moved
}

/// Every planner golden launch under `v`, rewritten where the change moves it.
fn launches(
    v: &mut Value,
    board: &str,
    mutant: &'static str,
    at: &str,
    moved: &mut usize,
    refused: &mut Vec<String>,
) {
    match v {
        Value::Object(o) => {
            if let Some(l) = o
                .get_mut("launch")
                .filter(|l| l.get("call").is_some() && l.get("graph").is_some())
            {
                let gen = l["call"]["gen"].as_str().unwrap_or_default().to_string();
                let nodes = l["graph"]["machine"]["nodes"].as_u64().unwrap() as u32;
                let m: Machine = Machine::named(board)
                    .and_then(|m| m.with_nodes(nodes))
                    .expect("a machine");
                let args = l["call"]["args"].clone();
                let now = ktpu_kernels::call(&gen, &args, &m).map_err(|e| e.to_string());
                let old =
                    with(mutant, || ktpu_kernels::call(&gen, &args, &m)).map_err(|e| e.to_string());
                let name = format!("{at} {gen}");
                match (now, old) {
                    (Ok(now), Ok(old)) => match regolded(now.graph, old.graph, &l["graph"]) {
                        Ok(Some(gr)) => {
                            println!("{name}");
                            l["graph"] = serde_json::to_value(&gr).unwrap();
                            *moved += 1;
                        }
                        Ok(None) => {}
                        Err(e) => refused.push(format!("{name}: {e}")),
                    },
                    (Err(a), Err(b)) if a == b => {}
                    (a, b) => refused.push(format!(
                        "{name}: the change moves the outcome: {:?} -> {:?}",
                        b.err(),
                        a.err()
                    )),
                }
            }
            for x in o.values_mut() {
                launches(x, board, mutant, at, moved, refused);
            }
        }
        Value::Array(a) => a
            .iter_mut()
            .for_each(|x| launches(x, board, mutant, at, moved, refused)),
        _ => {}
    }
}

fn plans(dir: &Path, mutant: &'static str, write: bool, refused: &mut Vec<String>) -> usize {
    let mut moved = 0;
    for path in jsons(dir) {
        let mut doc: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let Some(board) = doc
            .get("machine")
            .and_then(Value::as_str)
            .map(str::to_string)
        else {
            continue;
        };
        let before = moved;
        let at = path.file_name().unwrap().to_string_lossy().to_string();
        launches(&mut doc, &board, mutant, &at, &mut moved, refused);
        if write && moved > before {
            std::fs::write(&path, serde_json::to_string_pretty(&doc).unwrap() + "\n").unwrap();
        }
    }
    moved
}

fn jsons(dir: &Path) -> Vec<PathBuf> {
    let mut files: Vec<_> = std::fs::read_dir(dir)
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    files
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mutant: &'static str = NAMES
        .iter()
        .chain(LOCAL)
        .find(|n| **n == args[0])
        .unwrap_or_else(|| panic!("no mutant {}", args[0]));
    let write = args.iter().any(|a| a == "--write");
    let golden = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden");
    let mut refused = vec![];
    let k = kernels(&golden.join("kernels"), mutant, write, &mut refused);
    let p = plans(&golden.join("plan"), mutant, write, &mut refused);
    println!(
        "{k} kernel golden graphs and {p} planner golden launches moved by the change, {} refused",
        refused.len()
    );
    for r in &refused {
        println!("REFUSED {r}");
    }
    assert!(refused.is_empty(), "{} refused", refused.len());
}
