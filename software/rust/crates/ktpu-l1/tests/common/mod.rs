//! Shared helpers: the spec runners' JSON forms (`ktpu_l1::spec::json`) and
//! the vectors on disk.

#![allow(dead_code)]

use std::path::PathBuf;

#[allow(unused_imports)]
pub use ktpu_l1::spec::json::*;
use serde_json::Value;

pub fn machine() -> ktpu_machine::Machine {
    ktpu_machine::Machine::named("multimesh_v9t6").expect("machine file")
}

pub fn repo() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..")
}

/// Every vector under `software/tests/spec/vectors/<dir>` run by `runner`.
pub fn vectors(dir: &str, runner: &str) -> Vec<(String, Value)> {
    let root = repo().join("software/tests/spec/vectors").join(dir);
    let mut out = Vec::new();
    let mut todo = vec![root];
    while let Some(d) = todo.pop() {
        for e in std::fs::read_dir(&d).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                todo.push(p);
            } else if p.extension().is_some_and(|x| x == "json") {
                let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
                if runner.is_empty() || v["runner"] == runner {
                    out.push((p.file_name().unwrap().to_string_lossy().into_owned(), v));
                }
            }
        }
    }
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

/// The vector's machine, or none for the default.
pub fn machine_of(v: &Value) -> Option<ktpu_machine::Machine> {
    v["machine"]
        .as_str()
        .map(|n| ktpu_machine::Machine::named(n).expect("vector machine"))
}

/// Every `runner` vector through `ktpu_l1::spec::run`: each must give its
/// `expect`. Returns how many ran.
pub fn run_all(runner: &str) -> usize {
    let vs = vectors("l1", runner);
    assert!(!vs.is_empty(), "no {runner} vectors");
    let mut bad = Vec::new();
    let mut ran = 0;
    for (name, v) in &vs {
        if runner == "l1.package" && v["input"]["all"] == true {
            continue;
        }
        ran += 1;
        let m = machine_of(v);
        match ktpu_l1::spec::run(runner, &v["input"], m.as_ref(), None) {
            Ok(got) if got == v["expect"] => {}
            Ok(got) => bad.push(format!("{name}\n  got  {got}\n  want {}", v["expect"])),
            Err(e) => bad.push(format!("{name}\n  failed {e}")),
        }
    }
    assert!(
        bad.is_empty(),
        "{} of {}:\n{}",
        bad.len(),
        ran,
        bad.join("\n")
    );
    eprintln!("{runner}: {ran} of {} vectors run and match", vs.len());
    ran
}
