//! Deletion witnesses for Python bank lint: all expanded images and placement.
use ktpu_machine::Machine;
use ktpu_verify::{lint, Kind};
use serde_json::json;
use std::path::PathBuf;

fn machine(copies: u32) -> Machine {
    Machine::named("multimesh_v9t6")
        .unwrap()
        .with_fact("vc.rf_copies", json!(copies))
        .unwrap()
}
fn module(text: &str) -> ktpu_l1::text::Module {
    ktpu_l1::text::read(text, "<lint>").unwrap()
}
fn example(level: &str, name: &str) -> String {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../language/kohakutpu/language/examples")
        .join(level)
        .join("vector");
    std::fs::read_to_string(dir.join(format!("{name}.ktpu"))).unwrap()
}

#[test]
fn three_words_of_one_bank_hold_the_math_engine() {
    let src = module("image k()\n    vfma v0, v1, v2, v3\n    halt\n");
    let found = lint(&src, &machine(2)).unwrap();
    assert_eq!(found.len(), 1);
    assert_eq!(found[0].kind, Kind::Bank);
    assert!(found[0].text.contains("8 beats hold the math engine"));
    assert!(lint(&src, &machine(4)).unwrap().is_empty());
}

fn opposite() -> bool {
    let src = module("image k()\n    vunpk v0 <- l1[0] f16\n    vmul v1[1:], v0, s0\n    vmax v0, v0, v1[1:]\n    vpack v0 -> l1[64] f16\n    halt\n");
    lint(&src, &machine(2)).unwrap().is_empty()
}
#[test]
fn two_reads_in_opposite_banks_lint_clean() {
    assert!(opposite());
}

#[test]
fn every_image_is_linted_for_the_machines_copies() {
    let src = module(&example("l1", "silu"));
    assert!(lint(&src, &machine(4)).unwrap().is_empty());
    let found = lint(&src, &machine(2)).unwrap();
    assert_eq!(found.len(), 32);
    assert!(found.iter().all(|f| f.kind == Kind::Bank
        && f.text.contains("pack engine")
        && f.text.contains("statement")));
}

fn held_examples() -> Vec<String> {
    let m = machine(2);
    let mut held = Vec::new();
    for name in ["softmax", "leaky", "layernorm", "silu", "add"] {
        let text = example("l2", name);
        let p = ktpu_l2::Program::parse(&text, name, None).unwrap();
        let c = ktpu_l2::compile_l1(&p, &m, &ktpu_l2::PASSES, false, &ktpu_l2::Search::default())
            .unwrap();
        if lint(&c.l1, &m)
            .unwrap()
            .iter()
            .any(|f| f.text.contains("hold the math engine"))
        {
            held.push(name.to_string());
        }
    }
    held
}
#[test]
fn compiled_vector_examples_hold_no_math_beat_on_two_copies() {
    assert!(held_examples().is_empty());
}

#[cfg(feature = "mutants")]
#[test]
fn one_bank_is_caught() {
    assert!(!ktpu_verify::mutant::with("verify.lint/one-bank", opposite));
}

#[cfg(feature = "mutants")]
#[test]
fn placement_off_holds_the_math_engine() {
    let state = ktpu_l2::mutant::State {
        banks_off: true,
        ..Default::default()
    };
    assert!(!ktpu_l2::mutant::scoped(state, held_examples).is_empty());
}
