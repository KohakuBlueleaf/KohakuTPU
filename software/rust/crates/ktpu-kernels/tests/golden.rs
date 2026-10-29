//! Every generator call the Python planner made (`golden/kernels/*.json`,
//! written by `golden/kernels_dump.py`) against the Rust generators: the
//! kernel name, the given specs and the graph (each statement's `where`
//! aside) on the call's machine and on two nodes; a call a ruling changes
//! against its ruled kernel, or its ruled refusal's clause (RUL.030); a
//! planning helper's result; a refusal where Python refused.

mod common;

use common::*;
use ktpu_graph as g;
use ktpu_kernels::gen::{convert, lanes, matmul, transpose, vector};
use ktpu_kernels::{Kernel, Side};
use ktpu_machine::{Machine, Refusal};
use serde_json::Value;
use std::collections::BTreeMap;

enum Got {
    Kernel(Kernel, Option<i64>),
    Result(Value),
}

fn s(v: &Value) -> &str {
    v.as_str().expect("a string")
}

/// The Rust generator for one recorded call.
fn run(call: &Value, plans: &[&Value], m: &Machine) -> Result<Got, Refusal> {
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

fn lanes_plan(a: &Value, m: &Machine) -> Result<lanes::Plan, Refusal> {
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

fn graph(v: &Value) -> g::Graph {
    without_where(serde_json::from_value(v.clone()).expect("a golden graph"))
}

/// What differs between a recorded kernel (`want`: name, given, graph,
/// nodes2, nblk) and the Rust one, or None.
fn compare(
    want: &Value,
    got: &Kernel,
    nblk: Option<i64>,
    two: &Result<Got, Refusal>,
) -> Option<String> {
    if want.get("trace_error").is_some() {
        return Some(format!("Python's trace refused: {}", want["trace_error"]));
    }
    if s(&want["name"]) != got.graph.name {
        return Some(format!("name {} vs {}", want["name"], got.graph.name));
    }
    let given: BTreeMap<String, g::Spec> =
        serde_json::from_value(want["given"].clone()).expect("given");
    let have: BTreeMap<String, g::Spec> = got.given.iter().cloned().collect();
    if given != have {
        return Some(format!("given {given:?} vs {have:?}"));
    }
    if let (Some(n), Some(w)) = (nblk, want.get("nblk")) {
        if w.as_i64() != Some(n) {
            return Some(format!("nblk {w} vs {n}"));
        }
    }
    let (wg, gg) = (graph(&want["graph"]), without_where(got.graph.clone()));
    if wg != gg {
        return Some(format!("graph: {}", first_diff(&wg, &gg)));
    }
    match (want.get("nodes2"), two) {
        (Some(Value::String(e)), Ok(_)) => {
            Some(format!("two nodes: Python refused ({e}), Rust did not"))
        }
        (Some(Value::String(_)), Err(_)) => None,
        (Some(w), Ok(Got::Kernel(k2, _))) => {
            let (wg, gg) = (graph(w), without_where(k2.graph.clone()));
            (wg != gg).then(|| format!("two nodes: {}", first_diff(&wg, &gg)))
        }
        (Some(_), Err(e)) => Some(format!("two nodes: Rust refused {e}")),
        _ => None,
    }
}

struct Tally {
    total: usize,
    ruled: usize,
    by_fn: BTreeMap<String, (usize, usize)>,
    fails: Vec<String>,
}

fn check_all() -> Tally {
    let mut files: Vec<_> = std::fs::read_dir(golden_dir())
        .expect("golden/kernels")
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    assert!(files.len() >= 200, "{} golden cases", files.len());
    let (mut total, mut ruled) = (0, 0);
    let mut by_fn: BTreeMap<String, (usize, usize)> = BTreeMap::new();
    let mut fails = vec![];
    for path in &files {
        let doc: Value =
            serde_json::from_str(&std::fs::read_to_string(path).expect("a case")).expect("json");
        let calls = doc["calls"].as_array().expect("calls");
        let plans: Vec<&Value> = calls.iter().filter(|c| c["fn"] == "lanes.plan").collect();
        for call in calls {
            total += 1;
            let f = s(&call["fn"]).to_string();
            let mrec = &call["machine"];
            let m = machine(s(&mrec["board"]), int(&mrec["nodes"]) as u32);
            let facts = ktpu_kernels::trace::Facts::of(&m);
            assert_eq!(
                (facts.mg, facts.vc),
                (int(&mrec["mg"]), int(&mrec["vc"])),
                "{f}: machine"
            );
            let got = run(call, &plans, &m);
            let want = call.get("ruled").unwrap_or(call);
            if call.get("ruled").is_some() {
                ruled += 1;
            }
            let refused = ["error", "trace_error"]
                .iter()
                .find_map(|k| want.get(*k).or(call.get(*k)));
            let bad = match (&got, refused, want.get("refused")) {
                (Err(e), _, Some(r)) => (e.clause != s(&r["clause"]))
                    .then(|| format!("Rust refused {e}, the ruling refuses {}", r["clause"])),
                (Ok(_), _, Some(r)) => Some(format!(
                    "the ruling refuses ({}), Rust did not",
                    r["clause"]
                )),
                (_, _, None) => match (&got, refused) {
                    (Err(_), Some(_)) => None,
                    (Ok(_), Some(e)) => Some(format!("Python refused ({e}), Rust did not")),
                    (Err(e), None) => Some(format!("Rust refused: {e}")),
                    (Ok(Got::Result(v)), None) => {
                        (*v != want["result"]).then(|| format!("result {v} vs {}", want["result"]))
                    }
                    (Ok(Got::Kernel(kern, nblk)), None) => {
                        let two = run(call, &plans, &m.with_nodes(2).expect("two nodes"));
                        if matches!(f.as_str(), "vector.flat_kernel" | "convert.cast_kernel")
                            && int(&call["args"]["n"]) % (m.vc.vlmax as i64) != 0
                        {
                            // RUL.020: port extents are words, not rounded vector rows.
                            let n = int(&call["args"]["n"]);
                            let scalars = call["args"]["scalars"].as_array();
                            let bad = kern.graph.ports.iter().enumerate().find(|(i, p)| {
                                let scalar = scalars.is_some_and(|ss| {
                                    ss.iter().any(|s| s[0].as_u64() == Some(*i as u64))
                                });
                                let item = if p.spec.dtype == g::Dtype::F32 { 4 } else { 2 };
                                let expected = if scalar {
                                    32
                                } else {
                                    (n * item + 31) / 32 * 32
                                };
                                p.spec.shape.iter().product::<i64>() * item != expected
                            });
                            bad.map(|(_, p)| format!("RUL.020: {} is not word-exact", p.name))
                        } else {
                            compare(want, kern, *nblk, &two)
                        }
                    }
                },
            };
            let slot = by_fn.entry(f.clone()).or_default();
            slot.1 += 1;
            match bad {
                None => slot.0 += 1,
                Some(why) => fails.push(format!(
                    "{} {f}: {why}",
                    path.file_name().unwrap().to_string_lossy()
                )),
            }
        }
    }
    Tally {
        total,
        ruled,
        by_fn,
        fails,
    }
}

#[test]
fn every_generator_call_matches_python() {
    let t = check_all();
    let passed = t.total - t.fails.len();
    eprintln!(
        "golden calls: {passed} / {} pass ({} ruled)",
        t.total, t.ruled
    );
    for (f, (p, n)) in &t.by_fn {
        eprintln!("  {f}: {p} / {n}");
    }
    for f in t.fails.iter().take(25) {
        eprintln!("FAIL {f}");
    }
    assert!(
        t.fails.is_empty(),
        "{} of {} golden calls differ",
        t.fails.len(),
        t.total
    );
}

/// Each generator mutant breaks golden calls: the comparison can fail.
#[cfg(feature = "mutants")]
#[test]
fn each_mutant_breaks_golden_calls() {
    for name in ktpu_kernels::mutants::NAMES {
        if *name == "dsl.ops/lane-lost" {
            let m = machine("multimesh_v9t6", 1);
            let args = serde_json::json!({
                "e": {"nodes": [{"load": 0}, {"const": 1.0},
                    {"op": "add", "args": [0, 1]}, {"load": 1},
                    {"op": "mul", "args": [2, 3]}], "root": 4},
                "dtypes": ["f16", "f16"], "n": 10, "scalars": [[0, 0]]
            });
            let make = || ktpu_kernels::call::call("vector.flat_kernel", &args, &m);
            assert!(make().is_ok());
            let broken = ktpu_kernels::mutants::with(name, make);
            assert!(broken.unwrap_err().to_string().contains("store of"));
            continue;
        }
        if *name == "memory/reverse-overrun" {
            let m = machine("multimesh_v9t6", 1);
            let make = || ktpu_kernels::reverse_kernel(288, &[(9, 32)], 0, 144, &m).unwrap();
            let plain = make();
            let broken = ktpu_kernels::mutants::with(name, make);
            assert_ne!(plain.graph.ports, broken.graph.ports);
            continue;
        }
        let t = ktpu_kernels::mutants::with(name, check_all);
        eprintln!(
            "{name}: {} of {} golden calls differ",
            t.fails.len(),
            t.total
        );
        assert!(!t.fails.is_empty(), "mutant {name} breaks no golden call");
    }
}
