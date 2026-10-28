//! Every tier-0 spec vector whose runner `ktpu_isa::spec` ports, run against
//! the Rust stack (`software/tests/spec/vectors`), and under each mutant it
//! names that has a Rust knob: the vector must pass, and fail under each.

use ktpu_isa::spec::{mutants, run, runners, skipped_mutants};
use ktpu_isa::Machine;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

fn files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            files(&p, out);
        } else if p.extension().is_some_and(|x| x == "json") {
            out.push(p);
        }
    }
}

/// `(file name, vector)` of every tier-0 vector of a ported runner.
fn vectors() -> Vec<(String, Value)> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/spec/vectors");
    let mut paths = Vec::new();
    files(&root, &mut paths);
    paths.sort();
    let ported = runners();
    let mut out = Vec::new();
    for path in paths {
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let vs = match &doc {
            Value::Array(v) => v.clone(),
            v => v
                .get("vectors")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_else(|| vec![v.clone()]),
        };
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        for v in vs {
            let runner = v.get("runner").and_then(Value::as_str).unwrap_or("");
            if ported.contains(&runner) && v.get("tier").and_then(Value::as_u64) == Some(0) {
                out.push((name.clone(), v));
            }
        }
    }
    out
}

/// Vectors that read a machine fact the generator does not write yet
/// (RUL.007, RUL.012, RUL.013, RUL.015).
const NEEDS_FACT: &[&str] = &[
    "RUL.007.macs.json",
    "RUL.012.f16_ieee.json",
    "RUL.013.profile.json",
    "RUL.015.simt.json",
];

#[test]
fn tier0_vectors_and_their_mutants() {
    let mut machines: BTreeMap<String, Machine> = BTreeMap::new();
    let knobs = mutants();
    let skipped: BTreeMap<_, _> = skipped_mutants().into_iter().collect();
    let (mut pass, mut elsewhere, mut facts) = (0, 0, 0);
    let (mut killed, mut skip) = (0, 0);
    let mut bad = Vec::new();
    let all = vectors();
    for (name, v) in &all {
        let m = v.get("machine").and_then(Value::as_str).map(|n| {
            machines
                .entry(n.to_string())
                .or_insert_with(|| Machine::named(n).unwrap())
                .clone()
        });
        let runner = v["runner"].as_str().unwrap();
        match run(runner, &v["input"], m.as_ref(), None) {
            Ok(g) if g == v["expect"] => pass += 1,
            Err(e) if e.contains("has no ktpu-isa port") => {
                elsewhere += 1;
                eprintln!("elsewhere {name}: {e}");
                continue;
            }
            Err(e) if NEEDS_FACT.contains(&name.as_str()) => {
                facts += 1;
                eprintln!("needs a fact {name}: {e}");
                continue;
            }
            Ok(g) => {
                bad.push(format!("{name}: got {g}, want {}", v["expect"]));
                continue;
            }
            Err(e) => {
                bad.push(format!("{name}: not run: {e}"));
                continue;
            }
        }
        for mu in v
            .get("mutants")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let mu = mu.as_str().unwrap();
            if !knobs.contains(&mu) {
                assert!(
                    skipped.contains_key(mu),
                    "{name}: mutant {mu} is neither ported nor listed"
                );
                skip += 1;
                continue;
            }
            if cfg!(not(feature = "mutants")) {
                continue;
            }
            match run(runner, &v["input"], m.as_ref(), Some(mu)) {
                Ok(g) if g == v["expect"] => bad.push(format!("{name}: survives {mu}")),
                Ok(_) => killed += 1,
                Err(e) => bad.push(format!("{name}: {mu} not run: {e}")),
            }
        }
    }
    eprintln!(
        "{} vectors: {pass} pass, {elsewhere} read parameters of other crates, {facts} need a \
         machine fact; mutant runs: {killed} killed, {skip} skipped (no Rust knob)",
        all.len()
    );
    assert!(all.len() > 100);
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}
