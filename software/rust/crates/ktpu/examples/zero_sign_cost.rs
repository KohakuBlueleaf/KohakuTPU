//! The compile-side cost of `zero_sign` (L2.VC.026): every golden kernel
//! (`golden/e2e`, `golden/l2` and each distinct launch graph of `golden/plan`)
//! lowered under both values, a JSON line a kernel and value.
//!
//! ```text
//! zero_sign_cost --out FILE [--shard i/n]
//! ```
//!
//! A line holds the case's machine, kernel name and L2 text, and the
//! refusal (stage text) or: the seconds the passes, the
//! lowering and the image scheduler take; the images the search chose and
//! the core's timing of each (summed: `cycles`); the scheduled L1's ops by
//! mnemonic, its `seti` count, line count and text.

use ktpu::{Backend, Stack};
use ktpu_l2::{compile_with, Program, Search, Strategy, Target, ZeroSign, PASSES};
use ktpu_machine::Machine;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Instant;

/// One kernel: where it came from, its machine, its input and fn name.
struct Case {
    name: String,
    machine: String,
    nodes: Option<u32>,
    graph: Option<String>,
    l2: Option<String>,
    kernel: Option<String>,
}

fn golden(sub: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden")
        .join(sub)
}

fn json_files(dir: &Path) -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    paths
}

/// Each `graph` object under a plan's launches.
fn graphs<'a>(v: &'a Value, out: &mut Vec<&'a Value>) {
    match v {
        Value::Object(o) => {
            if let Some(g) = o.get("graph").filter(|g| g.is_object()) {
                out.push(g);
            }
            o.values().for_each(|x| graphs(x, out));
        }
        Value::Array(a) => a.iter().for_each(|x| graphs(x, out)),
        _ => {}
    }
}

fn cases() -> Vec<Case> {
    let mut out = Vec::new();
    for (sub, prefix) in [("e2e", "e2e"), ("l2", "l2")] {
        for p in json_files(&golden(sub)) {
            let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
            let graph = v
                .get("graph")
                .filter(|g| !g.is_null())
                .map(Value::to_string);
            let l2 = v["l2"].as_str().map(str::to_string);
            if graph.is_none() && l2.is_none() {
                continue;
            }
            out.push(Case {
                name: format!("{prefix}/{}", p.file_stem().unwrap().to_string_lossy()),
                machine: v["machine"].as_str().unwrap().to_string(),
                nodes: v["nodes"].as_u64().map(|n| n as u32),
                graph,
                l2,
                kernel: v["name"].as_str().map(str::to_string),
            });
        }
    }
    let mut seen = BTreeSet::new();
    for p in json_files(&golden("plan")) {
        let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        let mut gs = Vec::new();
        graphs(&v, &mut gs);
        for g in gs {
            let text = g.to_string();
            if !seen.insert(text.clone()) {
                continue;
            }
            out.push(Case {
                name: format!("plan/{}", g["name"].as_str().unwrap_or("?")),
                machine: v["machine"].as_str().unwrap().to_string(),
                nodes: v["nodes"].as_u64().map(|n| n as u32),
                graph: Some(text),
                l2: None,
                kernel: None,
            });
        }
    }
    out
}

fn machine(file: &str, nodes: Option<u32>) -> Machine {
    let m = Machine::named(file).expect("machine file");
    match nodes {
        Some(n) if n != m.nodes() => m.with_nodes(n).expect("a card of that many nodes"),
        _ => m,
    }
}

fn program(c: &Case, m: &Machine) -> Result<Program, String> {
    let p = match (&c.graph, &c.l2) {
        (Some(g), _) => Stack.graph_to_l2(g, m),
        (None, Some(t)) => Stack.parse_l2(t),
        (None, None) => unreachable!(),
    }
    .map_err(|r| format!("{}: {}", r.clause, r.message))?;
    let name = c.kernel.clone().unwrap_or_else(|| p.name.clone());
    match p.name == name {
        true => Ok(p),
        false => Program::parse(p.text(), "<l2>", Some(&name)).map_err(|e| e.to_string()),
    }
}

/// Mnemonic -> count over the L1 text's instruction lines.
fn histogram(l1: &str) -> BTreeMap<String, u64> {
    let mut h = BTreeMap::new();
    for line in l1.lines() {
        let Some(word) = line.split_whitespace().next() else {
            continue;
        };
        if word
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        {
            *h.entry(word.to_string()).or_default() += 1;
        }
    }
    h
}

fn measure(c: &Case, z: ZeroSign) -> Value {
    let m = machine(&c.machine, c.nodes);
    let p = match program(c, &m) {
        Ok(p) => p,
        Err(e) => return json!({"case": c.name, "zero_sign": z.name(), "input": e}),
    };
    let t = Target::of(&m);
    let search = Search::new(Strategy::Auto).signed(z);
    let t0 = Instant::now();
    let got = compile_with(&p, &t, &PASSES, true, &search);
    let seconds = t0.elapsed().as_secs_f64();
    let chosen = search.chosen();
    let mut line = json!({
        "case": c.name, "zero_sign": z.name(), "seconds": seconds,
        "machine": c.machine, "kernel": p.name, "l2": p.text(),
        "images": chosen.len(),
        "cycles": chosen.iter().map(|(_, n)| n).sum::<i64>(),
        "image_cycles": chosen.iter().map(|(_, n)| n).collect::<Vec<_>>(),
        "timed": search.timed(),
    });
    match got {
        Ok(out) => {
            let text = out.l1.source.clone();
            let ops = histogram(&text);
            line["seti"] = json!(ops.get("seti").copied().unwrap_or(0));
            line["ops"] = json!(ops);
            line["lines"] = json!(text.lines().count());
            line["l1"] = json!(text);
        }
        Err(e) => line["refused"] = json!(format!("{}: {}", e.clause, e.message)),
    }
    line
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args: BTreeMap<String, String> = argv
        .chunks(2)
        .map(|kv| (kv[0].trim_start_matches("--").to_string(), kv[1].clone()))
        .collect();
    let (i, n) = args
        .get("shard")
        .map_or("0/1", String::as_str)
        .split_once('/')
        .unwrap();
    let (i, n): (usize, usize) = (i.parse().unwrap(), n.parse().unwrap());
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&args["out"])
        .expect("--out");
    for c in cases().into_iter().skip(i).step_by(n) {
        for z in [ZeroSign::Native, ZeroSign::Preserve] {
            writeln!(f, "{}", measure(&c, z)).unwrap();
        }
    }
}
