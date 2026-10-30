//! Reviewed line-layout overlay: immutable input, corrected graph compilation.
use ktpu::{compile, Cache, Input, Stack};
use ktpu_machine::Machine;
use serde_json::{json, Value};
use std::path::Path;

pub const CASE: &str = "kernels-lanes-TG.RUN.012.relayout";

pub fn check(case: &Value, m: &Machine) -> Result<(), String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let graph: ktpu_graph::Graph =
        serde_json::from_value(case["graph"].clone()).map_err(|e| e.to_string())?;
    let slots = ktpu::stack::lines::isolate(graph.clone(), m);
    if slots.ports != graph.ports || slots == graph {
        return Err("line-slot eligibility/ports changed".into());
    }
    let old = compile(
        &Stack,
        &Input::L2(case["l2"].as_str().ok_or("old L2")?.into()),
        Some(graph.name.as_str()),
        m,
        &Cache::off(),
    )
    .map_err(|e| e.to_string())?;
    if !old
        .verdict
        .findings
        .iter()
        .any(|f| f.clause == "L1.VER.017")
    {
        return Err("unsafe frozen L2 did not trigger L1.VER.017".into());
    }
    let new = compile(
        &Stack,
        &Input::Graph(case["graph"].to_string()),
        Some(graph.name.as_str()),
        m,
        &Cache::off(),
    )
    .map_err(|e| e.to_string())?;
    if new.verdict.refused || new.bound.is_some() {
        return Err(format!("corrected graph: {:?}", new.verdict));
    }
    let output = json!({"case":CASE, "clause":"L1.VER.017", "compiled":ktpu::json::compiled(&new)});
    let path = root
        .join("software/rust/golden/lines")
        .join(format!("{CASE}.json"));
    let mode = std::env::var("KTPU_LINE_OUTPUT_REGOLD").unwrap_or_default();
    if !["", "audit", "write"].contains(&mode.as_str()) {
        return Err("line output mode".into());
    }
    if mode == "write" {
        std::fs::create_dir_all(path.parent().unwrap()).map_err(|e| e.to_string())?;
        std::fs::write(&path, serde_json::to_string_pretty(&output).unwrap() + "\n")
            .map_err(|e| e.to_string())?;
    }
    if mode != "audit" {
        let want: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).map_err(|e| e.to_string())?)
                .map_err(|e| e.to_string())?;
        if want != output {
            return Err("corrected line-layout output differs; audit before regeneration".into());
        }
    }
    println!("LINE overlay: frozen input rejected; replicated line slots accepted; {} scratch arenas, {} node packages",
        new.scratch.len(), new.packages.len());
    Ok(())
}
