//! A build at fixed binds (`CardStack::binds`, `node_fed`): the lanes
//! relayout's 2177 buffers are not relocatable (STACK.RELOC), so the card
//! builds it at each binding; built at the Python reference's binds, its
//! packages are the reference's bytes. Under `ktpu.stack/binds_ignored` the
//! binds are dropped and the build is refused or differs.

#![cfg(feature = "stack")]

use ktpu::{Backend, CardStack, Stack};
use ktpu_machine::Machine;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;

const CASE: &str = "kernels-lanes-TG.RUN.012.relayout";

fn case() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden/e2e")
        .join(format!("{CASE}.json"));
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// The reference's relocatable bytes of each mesh, or why the build is not
/// them. The header's signature word (hex 16..32) is the machine's in Rust
/// and 0 in Python (RUL.041): left out.
fn built_at_its_binds(c: &Value, m: &Machine) -> Result<(), String> {
    let binds: BTreeMap<String, i128> = c["binds"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_i64().unwrap() as i128))
        .collect();
    let b = CardStack {
        binds: Some(binds),
        node_fed: true,
        ..CardStack::default()
    };
    let l1 = Stack
        .parse_l1(c["l1"].as_str().unwrap())
        .map_err(|e| e.to_string())?;
    let name = c["name"].as_str().unwrap();
    let progs = b.programs(&l1, name, None, m).map_err(|e| e.to_string())?;
    let got = b.order(&progs, m).map_err(|e| e.to_string())?;
    let want = c["ruled"]["packages"]
        .as_object()
        .or(c["packages"].as_object())
        .unwrap();
    for (mesh, p) in want {
        let g = ktpu::pipeline::cache::hex(&got[&mesh.parse::<u32>().unwrap()].bytes);
        let w = p["relocatable"].as_str().unwrap();
        if g.len() != w.len() || g[..16] != w[..16] || g[32..] != w[32..] {
            return Err(format!("mesh {mesh}: bytes differ"));
        }
    }
    Ok(())
}

fn machine(c: &Value) -> Machine {
    let m = Machine::named(c["machine"].as_str().unwrap()).unwrap();
    let n = c["nodes"].as_u64().unwrap() as u32;
    match m.nodes() == n {
        true => m,
        false => m.with_nodes(n).unwrap(),
    }
}

#[test]
fn a_build_at_the_references_binds_is_the_references_bytes() {
    let c = case();
    // The case's scheduled L1 is the RUL.063 one where it differs; the
    // reference built from it is `ruled`.
    assert!(
        !c["ruled"].is_null(),
        "{CASE} carries its RUL.063 reference"
    );
    built_at_its_binds(&ruled_l1(&c), &machine(&c)).unwrap();
}

#[cfg(feature = "mutants")]
#[test]
fn dropping_the_binds_is_caught() {
    let c = ruled_l1(&case());
    let m = machine(&c);
    let got =
        ktpu::mutant::with(ktpu::stack::BINDS_IGNORED, || built_at_its_binds(&c, &m)).unwrap();
    assert!(got.is_err(), "the mutant gave the reference's bytes");
}

/// The case with `l1` the RUL.063 schedule (golden/sched/e2e) the ruled
/// reference was built from.
fn ruled_l1(c: &Value) -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden/sched/e2e")
        .join(format!("{CASE}.json"));
    let s: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut c = c.clone();
    c["l1"] = s["scheduled_ruled"].clone();
    c
}
