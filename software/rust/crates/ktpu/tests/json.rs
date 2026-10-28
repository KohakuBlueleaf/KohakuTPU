//! `ktpu::json::compile` (the `wasm` exports' body): its JSON against the
//! native compile of the same input, field by field.

#![cfg(feature = "stack")]

use ktpu::{Cache, Compiled, Input, Stack};
use ktpu_machine::Machine;
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::Path;

const BOARD: &str = "multimesh_v9t6";

fn machine_text() -> String {
    let path = ktpu_machine::machines_dir().join(format!("{BOARD}.json"));
    std::fs::read_to_string(path).unwrap()
}

/// A golden kernel's tracer graph (JSON text) and its L2 text.
fn golden() -> (String, String) {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/e2e/examples-dsl-activation.json");
    let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    (
        case["graph"].to_string(),
        case["l2"].as_str().unwrap().to_string(),
    )
}

fn native(source: &str) -> Result<Compiled, ktpu::Refused> {
    let m = Machine::from_json(&machine_text()).unwrap();
    ktpu::compile(&Stack, &Input::detect(source), None, &m, &Cache::off())
}

fn unhex(s: &str) -> Vec<u8> {
    assert_eq!(s.len() % 2, 0, "even-length hex");
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn keys(v: &Value) -> BTreeSet<&str> {
    v.as_object().unwrap().keys().map(String::as_str).collect()
}

/// `got` is `want` in `_rs.compile`'s dict shape, bytes as hex.
fn assert_shape(got: &Value, want: &Compiled) {
    assert_eq!(
        keys(got),
        BTreeSet::from(["name", "l2", "l1", "packages", "scratch", "verdict", "bound"])
    );
    assert_eq!(got["name"], want.name.as_str());
    assert_eq!(got["l2"], want.l2.as_str());
    assert_eq!(got["l1"], want.l1.as_str());
    let pkgs = got["packages"].as_object().unwrap();
    assert!(!want.packages.is_empty(), "a kernel with packages");
    assert_eq!(
        pkgs.keys().cloned().collect::<Vec<_>>(),
        want.packages.keys().map(u32::to_string).collect::<Vec<_>>()
    );
    for (mesh, p) in &want.packages {
        let one = &pkgs[&mesh.to_string()];
        assert_eq!(keys(one), BTreeSet::from(["bytes", "params", "images"]));
        assert_eq!(one["images"], p.images);
        assert_eq!(one["images"]["version"], 1);
        assert_eq!(
            unhex(one["bytes"].as_str().unwrap()),
            p.bytes,
            "mesh {mesh}"
        );
        assert_eq!(one["params"], serde_json::json!(p.params));
    }
    let scratch = got["scratch"].as_array().unwrap();
    assert_eq!(scratch.len(), want.scratch.len());
    for (one, a) in scratch.iter().zip(&want.scratch) {
        assert_eq!(
            keys(one),
            BTreeSet::from(["space", "node", "bytes", "members"])
        );
        assert_eq!(one["space"], a.space.as_str());
        assert_eq!(one["node"].as_i64(), a.node);
        assert_eq!(one["bytes"].as_u64(), Some(a.bytes));
        let members: Vec<(String, u64)> = one["members"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| (m[0].as_str().unwrap().to_string(), m[1].as_u64().unwrap()))
            .collect();
        assert_eq!(members, a.members);
    }
    let v = &got["verdict"];
    assert_eq!(keys(v), BTreeSet::from(["refused", "findings"]));
    assert_eq!(v["refused"], want.verdict.refused);
    let found = v["findings"].as_array().unwrap();
    assert_eq!(found.len(), want.verdict.findings.len());
    for (one, f) in found.iter().zip(&want.verdict.findings) {
        assert_eq!(keys(one), BTreeSet::from(["kind", "clause", "at", "text"]));
        assert_eq!(
            (&one["kind"], &one["clause"], &one["at"], &one["text"]),
            (
                &Value::from(f.kind.as_str()),
                &Value::from(f.clause.as_str()),
                &Value::from(f.at.as_str()),
                &Value::from(f.text.as_str())
            )
        );
    }
    assert_eq!(got["bound"].as_str(), want.bound.as_deref());
}

#[test]
fn a_graph_compiles_to_the_native_kernel() {
    let (graph, _) = golden();
    let got: Value =
        serde_json::from_str(&ktpu::json::compile(&graph, &machine_text(), None).unwrap()).unwrap();
    assert_shape(&got, &native(&graph).unwrap());
}

#[test]
fn l2_text_compiles_to_the_native_kernel() {
    let (_, l2) = golden();
    let got: Value =
        serde_json::from_str(&ktpu::json::compile(&l2, &machine_text(), None).unwrap()).unwrap();
    assert_eq!(got["l2"], l2.as_str());
    assert_shape(&got, &native(&l2).unwrap());
}

#[test]
fn a_refusal_is_a_value_naming_its_stage_and_clause() {
    let (_, l2) = golden();
    let named = ktpu::json::compile(&l2, &machine_text(), Some("no_such_fn")).unwrap();
    let got: Value = serde_json::from_str(&named).unwrap();
    let m = Machine::from_json(&machine_text()).unwrap();
    let want = ktpu::compile(
        &Stack,
        &Input::detect(&l2),
        Some("no_such_fn"),
        &m,
        &Cache::off(),
    )
    .unwrap_err();
    assert_eq!(keys(&got), BTreeSet::from(["refused"]));
    assert_eq!(
        got["refused"],
        serde_json::json!({
            "stage": want.stage.name(),
            "clause": want.refusal.clause,
            "message": want.refusal.message,
        })
    );
    assert!(!want.refusal.clause.is_empty());
}

#[test]
fn an_unreadable_machine_file_is_an_error() {
    let (_, l2) = golden();
    assert!(ktpu::json::compile(&l2, "{}", None).is_err());
    assert!(ktpu::json::compile(&l2, "not json", None).is_err());
}
