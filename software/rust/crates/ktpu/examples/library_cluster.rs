//! Offline MG consistency audit of every graph in a library capture.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use ktpu::{Backend, Stack};
use ktpu_l1::build::{programs_in, Builder, Opts};
use ktpu_l1::layout::{buffer::Buffer, scratch::arenas};
use ktpu_machine::Machine;
use ktpu_verify::{Kind, Nodes, Target};
use serde_json::{json, Value};

fn audit(source: &str, board: &str) -> Result<Value, Box<dyn std::error::Error>> {
    let graph: Value = serde_json::from_str(source)?;
    let m =
        Machine::named(board)?.with_nodes(graph["machine"]["nodes"].as_u64().unwrap_or(1) as u32)?;
    let name = graph["name"].as_str().ok_or("graph name")?;
    let parsed = Stack.graph_to_l2(source, &m)?;
    let caller: BTreeSet<_> = Stack.caller(&parsed, name)?.into_iter().collect();
    let passed = Stack.passes(&parsed, name, &m)?;
    let l1 = Stack.schedule(&Stack.lower(&passed, name, &m)?, &m)?;
    let mut binds = BTreeMap::new();
    let mut next = 0x1000000u64;
    for (n, t) in &l1.body(name, "l1")?.params {
        if caller.contains(n) {
            binds.insert(n.clone(), next as i128);
            next += Buffer::of(t)?.bytes(&m)?.div_ceil(4096) * 4096;
        }
    }
    let mut bases = Vec::new();
    for a in arenas(&l1, name, &caller, &m)? {
        let size = a.bytes.div_ceil(4096) * 4096;
        bases.push((a, next));
        next += size;
    }
    let programs = programs_in(&l1, name, &binds, &bases, &m, Some(m.nodes()), None)?;
    let builder = Builder::new(&m)?;
    let target = Target::from_machine(&m, None)?;
    let mut nodes = Nodes::new();
    for p in programs {
        nodes.insert(
            p.mesh,
            builder
                .build(&p, Opts::default())?
                .build(true, false)
                .bind(&[])?,
        );
    }
    let found = ktpu_verify::check(&[nodes], &target);
    let cluster: Vec<_> = found
        .iter()
        .filter(|f| f.kind == Kind::Cluster)
        .map(ToString::to_string)
        .collect();
    let other: Vec<_> = found
        .iter()
        .filter(|f| f.kind != Kind::Cluster && f.kind.refuses())
        .map(ToString::to_string)
        .collect();
    Ok(json!({"name": name, "cluster": cluster, "other_refusals": other}))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let family = std::env::args()
        .nth(1)
        .ok_or("usage: library_cluster FAMILY")?;
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let capture: Value = serde_json::from_str(&std::fs::read_to_string(
        root.join("build/librun").join(&family).join("capture.json"),
    )?)?;
    let board = capture["board"].as_str().ok_or("capture board")?;
    let sources = capture["sources"].as_object().ok_or("sources object")?;
    let mut results = Vec::new();
    let mut failed = 0;
    for (i, (key, v)) in sources.iter().enumerate() {
        let source = v
            .as_str()
            .or_else(|| v["source"].as_str())
            .ok_or("source string")?;
        let mut result = match audit(source, board) {
            Ok(v) => v,
            Err(e) => json!({"error": e.to_string()}),
        };
        result["key"] = json!(key);
        if result.get("error").is_some()
            || result["cluster"].as_array().is_some_and(|v| !v.is_empty())
            || result["other_refusals"]
                .as_array()
                .is_some_and(|v| !v.is_empty())
        {
            failed += 1;
            eprintln!("{result}");
        }
        results.push(result);
        if (i + 1) % 50 == 0 {
            eprintln!("{family}: {}/{}", i + 1, sources.len());
        }
    }
    let dir = root.join("build/mgchk");
    std::fs::create_dir_all(&dir)?;
    std::fs::write(
        dir.join(format!("library-{family}.json")),
        serde_json::to_string_pretty(&json!({
            "family":family, "sources":sources.len(), "failed": failed, "results":results
        }))?,
    )?;
    println!(
        "{family}: {} sources checked, {failed} compile errors or refusing findings",
        sources.len()
    );
    if failed != 0 {
        return Err("library cluster audit failed".into());
    }
    Ok(())
}
