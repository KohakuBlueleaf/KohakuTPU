//! Every named mutant of every ported tier-0 vector, run against ktpu-l0
//! (`--features mutants`): the vector (with its ruled input where a ruling
//! supersedes it) must give its expected output unmutated and something else
//! (or a refusal, or a panic) under each of its mutants. A mutant that leaves
//! its vector passing, or has no Rust knob, is a finding.

#[path = "../common/mod.rs"]
mod common;

use std::panic::{catch_unwind, AssertUnwindSafe};

use ktpu_l0::mutant::{self, NAMES};

#[test]
fn every_vector_mutant_breaks_its_vector() {
    let (mut killed, mut survived, mut missing) = (0, Vec::new(), Vec::new());
    let mut baseline = Vec::new();
    for (name, v) in common::ported_vectors() {
        match common::run_vector(&v) {
            Ok(g) if g == v["expect"] => {}
            Ok(_) => {
                baseline.push(name);
                continue;
            }
            Err(_) => continue,
        }
        for m in v["mutants"].as_array().unwrap() {
            let m = m.as_str().unwrap();
            let Some(&known) = NAMES.iter().find(|n| **n == m) else {
                missing.push(format!("{name} ~ {m}"));
                continue;
            };
            let got = catch_unwind(AssertUnwindSafe(|| {
                mutant::with(known, || common::run_vector(&v))
            }));
            match got {
                Ok(Ok(g)) if g == v["expect"] => survived.push(format!("{name} ~ {m}")),
                _ => killed += 1,
            }
        }
    }
    println!("killed: {killed}");
    println!("survived (findings): {survived:#?}");
    println!("no Rust mutant (findings): {missing:#?}");
    assert!(
        baseline.is_empty(),
        "vectors failing unmutated: {baseline:?}"
    );
    assert!(killed > 0);
    assert!(
        survived.is_empty() && missing.is_empty(),
        "{} survived, {} with no Rust mutant",
        survived.len(),
        missing.len()
    );
}
