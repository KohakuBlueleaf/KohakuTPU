//! Offline L1.VER.017 audit of capture graph sources, without a device.
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use ktpu::Backend;
use ktpu::Stack;
use ktpu_l1::build::{programs_in, Builder, Opts};
use ktpu_l1::layout::{buffer::Buffer, scratch::arenas};
use ktpu_machine::Machine;
use serde_json::{json, Value};

fn audit(source: &str) -> Result<Vec<String>, Box<dyn std::error::Error>> {
    let graph: Value = serde_json::from_str(source)?;
    let m = Machine::named("multimesh_v9t6")?
        .with_nodes(graph["machine"]["nodes"].as_u64().unwrap_or(1) as u32)?;
    let name = graph["name"].as_str().ok_or("graph name")?;
    let parsed = if std::env::var_os("KTPU_LINE_AUDIT_ORIGINAL_GRAPH").is_some() {
        let graph: ktpu_graph::Graph = serde_json::from_str(source)?;
        Stack.parse_l2(&ktpu_graph::lower(&graph)?.text())?
    } else {
        Stack.graph_to_l2(source, &m)?
    };
    let caller: BTreeSet<_> = Stack.caller(&parsed, name)?.into_iter().collect();
    let passed = Stack.passes(&parsed, name, &m)?;
    let l1 = Stack.lower(&passed, name, &m)?;
    let l1 = Stack.schedule(&l1, &m)?;
    let ar = arenas(&l1, name, &caller, &m)?;
    let mut binds = BTreeMap::new();
    let mut next = 0x1000000u64;
    for (n, t) in &l1.body(name, "l1")?.params {
        if caller.contains(n) {
            binds.insert(n.clone(), next as i128);
            next += Buffer::of(t)?.bytes(&m)?.div_ceil(4096) * 4096;
        }
    }
    let mut bases = Vec::new();
    for a in ar {
        let size = a.bytes.div_ceil(4096) * 4096;
        bases.push((a, next));
        next += size;
    }
    let programs = programs_in(&l1, name, &binds, &bases, &m, Some(m.nodes()), None)?;
    let builder = Builder::new(&m)?;
    let target = ktpu_verify::Target::from_machine(&m, None)?;
    let mut findings = Vec::new();
    for p in programs {
        let package = builder
            .build(&p, Opts::default())?
            .build(true, false)
            .bind(&[])?;
        findings.extend(
            ktpu_verify::lines::check(&package, p.mesh, &target)
                .into_iter()
                .map(|f| f.to_string()),
        );
    }
    Ok(findings)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let family = args
        .first()
        .ok_or("usage: library_lines FAMILY [OFFSET] [COUNT]")?;
    let offset = args
        .get(1)
        .map(|v| v.parse())
        .transpose()?
        .unwrap_or(0usize);
    let count = args
        .get(2)
        .map(|v| v.parse())
        .transpose()?
        .unwrap_or(usize::MAX);
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let capture: Value = serde_json::from_str(&std::fs::read_to_string(
        root.join("build/librun").join(family).join("capture.json"),
    )?)?;
    let sources = capture["sources"].as_object().ok_or("sources object")?;
    let mut results = Vec::new();
    for (i, (key, v)) in sources.iter().enumerate().skip(offset).take(count) {
        let source = v
            .as_str()
            .or_else(|| v["source"].as_str())
            .ok_or("source string")?;
        let g: Value = serde_json::from_str(source)?;
        let name = g["name"].as_str().unwrap_or("?");
        eprintln!("{family} {i}/{} {name}", sources.len());
        let checked = if let Ok(mutant) = std::env::var("KTPU_LINE_AUDIT_MUTANT") {
            ktpu::mutant::with(&mutant, || audit(source).map_err(|e| e.to_string()))?
        } else {
            audit(source).map_err(|e| e.to_string())
        };
        let result = match checked {
            Ok(findings) => json!({"index": i, "key": key, "name": name,
                "flagged": !findings.is_empty(), "findings": findings}),
            Err(e) => json!({"index": i, "key": key, "name": name, "error": e.to_string()}),
        };
        println!("{}", serde_json::to_string(&result)?);
        results.push(result);
    }
    let phase = std::env::var("KTPU_LINE_AUDIT_PHASE").unwrap_or_else(|_| "after".into());
    let dir = root.join("build/line/library").join(phase);
    std::fs::create_dir_all(&dir)?;
    let output =
        json!({"family":family, "total":sources.len(), "offset":offset, "results":results});
    std::fs::write(
        dir.join(format!("{family}-{offset}.json")),
        serde_json::to_string_pretty(&output)? + "\n",
    )?;
    Ok(())
}
