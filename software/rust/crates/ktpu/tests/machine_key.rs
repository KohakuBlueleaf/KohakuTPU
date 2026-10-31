//! A machine differing in every runtime fact compiles to the same bytes and
//! under the same digest: a firmware rebuild keeps each compiled artifact. The
//! compile constants a machine fact bounds hold on every machine.

#![cfg(feature = "stack")]

#[path = "../../ktpu-machine/tests/leaves/mod.rs"]
#[allow(dead_code)]
mod leaves;

use ktpu_machine::{machines_dir, names, Machine};
use serde_json::{json, Value};
use std::path::Path;

/// The fast end of each input kind: DSL and L2 examples, a walk, a band, and a
/// program every machine refuses.
const CASES: [&str; 6] = [
    "examples-dsl-activation",
    "examples-l2-share2",
    "l2-vc-L2.VC.001.pre_then_loop",
    "dsl-mov-DSL.MOV.002.walk_copy",
    "l2-mg-L2.MG.004.whole",
    "l2-pass-L2.PASS.003.absent_kind",
];

fn read(path: &Path) -> Value {
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// The cases a moved runtime fact changes the compiled output of.
fn moved_cases() -> Vec<&'static str> {
    let golden = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/e2e");
    let mut out = Vec::new();
    for name in CASES {
        let case = read(&golden.join(format!("{name}.json")));
        let board = case["machine"].as_str().unwrap();
        let doc = read(&machines_dir().join(format!("{board}.json")));
        let far = leaves::runtime_moved(&doc);
        assert_ne!(doc, far, "{board}");
        let l2 = case["l2"].as_str().unwrap();
        let near = ktpu::json::compile(l2, &doc.to_string(), None).unwrap();
        let moved = ktpu::json::compile(l2, &far.to_string(), None).unwrap();
        if near != moved {
            out.push(name);
        }
    }
    out
}

#[test]
fn a_runtime_fact_moves_no_compiled_byte() {
    assert_eq!(moved_cases(), Vec::<&str>::new());
}

#[cfg(feature = "mutants")]
#[test]
fn a_compile_read_of_a_runtime_fact_is_seen() {
    let moved = ktpu::mutant::with("machine.key/reads-runtime", moved_cases).unwrap();
    assert!(!moved.is_empty());
}

/// The largest cap a mover flags byte encodes: 2^(7-1) beats (ISA.MV.042).
const MOVER_CAP: u32 = 1 << 6;

/// Each compile constant a machine bounds or fixes that does not hold on `m`.
fn disagreements(m: &Machine) -> Vec<&'static str> {
    let mut out = Vec::new();
    if ktpu_l2::target::IMEM_ROOM > i64::from(m.vc.imem_words) {
        out.push("IMEM_ROOM past vc.imem_words");
    }
    if MOVER_CAP > *m.mover.burst_max.runtime() {
        out.push("the mover's burst cap past mover.burst_max");
    }
    if ktpu_isa::unit::vector::VecCfg::of(m).is_err() {
        out.push("XM_NAMES against vc.crossbar_modes");
    }
    out
}

#[test]
fn compile_constants_agree_with_every_machine() {
    let isa = ktpu_isa::Isa::new(&Machine::named("multimesh_v9t6").unwrap()).unwrap();
    assert!(isa.mover.flag_byte(false, 7).is_ok());
    assert!(isa.mover.flag_byte(false, 8).is_err());
    let dir = machines_dir();
    for name in names(&dir) {
        let m = Machine::named(&name).unwrap();
        assert_eq!(disagreements(&m), Vec::<&str>::new(), "{name}");
    }
}

#[test]
fn a_disagreeing_machine_is_seen() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let moved = |path: &str, to: Value| disagreements(&m.with_fact(path, to).unwrap());
    let below = 1i64 << ktpu_l2::target::IMEM_ROOM.ilog2();
    assert_eq!(moved("vc.imem_words", below.into()).len(), 1);
    assert_eq!(moved("mover.burst_max", 32.into()).len(), 1);
    assert_eq!(moved("vc.crossbar_modes", json!(["none"])).len(), 1);
}
