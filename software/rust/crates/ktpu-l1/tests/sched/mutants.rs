//! Every mutant a scheduler vector names (`--features mutants`), run on the
//! RUL.063 scheduler: the vector's ruled output must change under it.

use std::panic::{catch_unwind, AssertUnwindSafe};

use ktpu_l1::mutant;
use ktpu_machine::Machine;
use serde_json::Value;

const KNOBS: &[&str] = &[
    "l1.sched/sync-movable",
    "l1.sched/l1-words",
    "l1.sched/latency",
    "l1.sched/cfg-cost",
    "l1.sched/min-gain",
    "l1.sched/timing-flat",
    "l1.sched/unstable",
];

#[test]
fn rul_063_config_witness_fails_under_cfg_cost() {
    let (s, m) = mutant::with("l1.sched/cfg-cost", super::clauses::config_cost_cycles);
    assert!(
        m - s <= 4,
        "cfg-cost mutant still charges dispatch: {s} -> {m}"
    );
}

#[test]
fn every_scheduler_mutant_breaks_its_vector() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let golden = super::root().join("software/rust/golden/sched/vectors");
    let vectors = super::root().join("software/tests/spec/vectors/l1/sched");
    let (mut killed, mut survived, mut missing) = (0, Vec::new(), Vec::new());
    for e in std::fs::read_dir(&golden).unwrap() {
        let p = e.unwrap().path();
        let case: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        let v: Value = serde_json::from_str(
            &std::fs::read_to_string(vectors.join(p.file_name().unwrap())).unwrap(),
        )
        .unwrap();
        let runner = case["runner"].as_str().unwrap();
        for name in v["mutants"].as_array().unwrap() {
            let name = name.as_str().unwrap();
            let Some(&knob) = KNOBS.iter().find(|k| **k == name) else {
                missing.push(format!("{} ~ {name}", case["vector"]));
                continue;
            };
            let got = catch_unwind(AssertUnwindSafe(|| {
                mutant::with(knob, || super::vectors::run(runner, &case["input"], &m))
            }));
            match got {
                Ok(g) if g == case["ruled"] => {
                    survived.push(format!("{} ~ {name}", case["vector"]))
                }
                _ => killed += 1,
            }
        }
    }
    println!("killed {killed}; survived {survived:?}; no knob {missing:?}");
    assert!(survived.is_empty() && missing.is_empty());
}
