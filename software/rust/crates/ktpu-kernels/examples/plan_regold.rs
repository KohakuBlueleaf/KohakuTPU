//! Rewrite the planner goldens' launch graphs (`golden/plan/*.json`) that the
//! current generators trace differently, printing each rewritten launch and
//! its first difference. The goldens were dumped from the Python tracer; a
//! launch is rewritten only after the difference is shown to be a Python
//! defect (record it in `golden/README.md`).
//!
//!     cargo run -p ktpu-kernels --example plan_regold [--write]

use ktpu_graph as g;
use serde_json::Value;
use std::path::PathBuf;

fn clear(body: &mut [g::Stmt]) {
    for s in body {
        s.at.clear();
        match &mut s.node {
            g::Node::Par { body, .. }
            | g::Node::For { body, .. }
            | g::Node::When { body, .. }
            | g::Node::Sticky { body } => clear(body),
            _ => {}
        }
    }
}

fn first_diff(a: &Value, b: &Value, path: String) -> Option<String> {
    match (a, b) {
        (Value::Object(x), Value::Object(y)) => x.iter().find_map(|(k, v)| {
            first_diff(v, y.get(k).unwrap_or(&Value::Null), format!("{path}.{k}"))
        }),
        (Value::Array(x), Value::Array(y)) => x
            .iter()
            .zip(y)
            .enumerate()
            .find_map(|(i, (v, w))| first_diff(v, w, format!("{path}[{i}]")))
            .or_else(|| {
                (x.len() != y.len()).then(|| format!("{path}: {} vs {} items", x.len(), y.len()))
            }),
        _ => (a != b).then(|| format!("{path}: was {a} now {b}")),
    }
}

/// Rewrites every launch under `v` whose traced graph differs; returns the
/// rewritten launches' names and first differences.
fn visit(v: &mut Value, board: &str, out: &mut Vec<String>, quant_schedule: bool) {
    match v {
        Value::Object(m) => {
            if let Some(l) = m.get_mut("launch") {
                if l.get("call").is_some() && l.get("graph").is_some() {
                    let mut want: g::Graph = serde_json::from_value(l["graph"].clone()).unwrap();
                    let machine = ktpu_machine::Machine::named(board)
                        .and_then(|m| m.with_nodes(want.machine.nodes))
                        .expect("a machine");
                    let gen = l["call"]["gen"].as_str().unwrap_or_default().to_string();
                    // plan_calls.rs compares these against their rulings
                    // (RUL.060 truncation, RUL.020 flat tails), not the trace.
                    let ruled = l["call"]["args"].to_string().contains("\"op\":\"trunc\"")
                        || (gen == "vector.flat_kernel"
                            && l["call"]["args"]["n"].as_i64().unwrap_or(0)
                                % machine.vc.vlmax as i64
                                != 0);
                    if quant_schedule && gen != "matmul.quant_kernel" {
                        return;
                    }
                    if ruled {
                        // left as recorded
                    } else if let Ok(k) = ktpu_kernels::call(&gen, &l["call"]["args"], &machine) {
                        let mut got = k.graph;
                        clear(&mut want.body);
                        clear(&mut got.body);
                        if want != got {
                            let (a, b) = (
                                serde_json::to_value(&want).unwrap(),
                                serde_json::to_value(&got).unwrap(),
                            );
                            let d = first_diff(&a, &b, String::new()).unwrap_or_default();
                            out.push(format!("{} {gen}: {d}", want.name));
                            if quant_schedule {
                                // Only schedule attrs and their generated name may change.
                                let mut approved: g::Graph =
                                    serde_json::from_value(l["graph"].clone()).unwrap();
                                approved.name = got.name.clone();
                                for st in &mut approved.body {
                                    if let g::Node::Mover {
                                        op: g::MoverOp::Quant,
                                        kw,
                                        ..
                                    } = &mut st.node
                                    {
                                        assert!(
                                            kw.iter().all(|(k, _)| k == "finite"),
                                            "not a bare quant recording"
                                        );
                                        kw.extend(
                                            ktpu_kernels::gen::matmul::QUANT_SCHEDULE
                                                .iter()
                                                .map(|&(k, v)| (k.into(), g::Attr::Int(v))),
                                        );
                                    }
                                }
                                let mut comparable = approved.clone();
                                clear(&mut comparable.body);
                                assert_eq!(
                                    comparable, got,
                                    "quant regold cannot change tensor/layout/contract operations"
                                );
                                l["name"] = Value::String(approved.name.clone());
                                l["graph"] = serde_json::to_value(approved).unwrap();
                            } else {
                                l["graph"] = serde_json::to_value(&got).unwrap();
                            }
                        }
                    }
                }
            }
            for x in m.values_mut() {
                visit(x, board, out, quant_schedule);
            }
        }
        Value::Array(a) => a
            .iter_mut()
            .for_each(|x| visit(x, board, out, quant_schedule)),
        _ => {}
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let write = args.iter().any(|a| a == "--write");
    let quant_schedule = args.iter().any(|a| a == "--quant-schedule");
    let output = args
        .windows(2)
        .find(|p| p[0] == "--output")
        .map(|p| PathBuf::from(&p[1]));
    if quant_schedule {
        assert!(
            !write,
            "quant schedule exports use --output, never in-place --write"
        );
    }
    if let Some(out) = &output {
        std::fs::create_dir_all(out).unwrap();
    }
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden/plan");
    let mut files: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .collect();
    files.retain(|p| p.extension().is_some_and(|x| x == "json"));
    files.sort();
    for path in files {
        let mut doc: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let Some(board) = doc
            .get("machine")
            .and_then(Value::as_str)
            .map(str::to_string)
        else {
            continue;
        };
        let mut changed = vec![];
        visit(&mut doc, &board, &mut changed, quant_schedule);
        for c in &changed {
            println!("{}: {c}", path.file_name().unwrap().to_string_lossy());
        }
        if let Some(out) = output.as_ref().filter(|_| !changed.is_empty()) {
            let dest = out.join(path.file_name().unwrap());
            use std::io::Write;
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(dest)
                .unwrap();
            file.write_all((serde_json::to_string_pretty(&doc).unwrap() + "\n").as_bytes())
                .unwrap();
        }
        if write && !changed.is_empty() {
            std::fs::write(&path, serde_json::to_string_pretty(&doc).unwrap() + "\n").unwrap();
        }
    }
}
