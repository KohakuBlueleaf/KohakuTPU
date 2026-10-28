//! Hardware facts reach the lowering from the machine file: a machine that
//! differs in a fact changes the output (vc.timing) or is refused with a
//! clause (the vector lowering's register shape, L2.VC.025).

use std::path::PathBuf;

use ktpu_l2::{compile_with, Program, Search, Strategy, Target, PASSES};
use ktpu_machine::Machine;
use serde_json::json;

#[cfg(feature = "mutants")]
use ktpu_l2::lower::vector::modulo::Core;
#[cfg(feature = "mutants")]
use ktpu_l2::mutant::{scoped, State};

fn silu() -> Program {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../language/kohakutpu/language/examples/l2/vector/silu.ktpu");
    let text = std::fs::read_to_string(path).unwrap();
    Program::parse(&text, "silu", None).unwrap()
}

fn base() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

/// The L1 text of silu, software-pipelined, on `m`; or the refusal's clause.
fn modulo(m: &Machine) -> Result<String, String> {
    let search = Search::new(Strategy::Modulo);
    compile_with(&silu(), &Target::of(m), &PASSES, false, &search)
        .map(|c| c.l1_text)
        .map_err(|e| e.clause.to_string())
}

fn slow() -> Machine {
    base().with_fact("vc.timing.math_math", json!(60)).unwrap()
}

fn narrow() -> Machine {
    base().with_fact("vc.lanes", json!(8)).unwrap()
}

#[test]
#[allow(non_snake_case)]
fn L2_VC_014_modulo_reads_the_machine_timing() {
    assert_ne!(modulo(&base()).unwrap(), modulo(&slow()).unwrap());
}

#[test]
#[allow(non_snake_case)]
fn L2_VC_025_another_register_shape_is_refused() {
    assert_eq!(modulo(&narrow()), Err("L2.VC.025".to_string()));
}

/// `examples/l2/<rel>.ktpu` on one node of multimesh_v9t6: its L1 text.
#[cfg(feature = "mutants")]
fn one_node(rel: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../language/kohakutpu/language/examples/l2")
        .join(format!("{rel}.ktpu"));
    let text = std::fs::read_to_string(path).unwrap();
    let p = Program::parse(&text, rel, None).unwrap();
    let t = Target::of(&base()).with_nodes(1);
    compile_with(&p, &t, &PASSES, false, &Search::default())
        .unwrap()
        .l1_text
}

/// One node orders against no peer: no ring and no slot wait; under
/// `l2.lower/handshake-always` it does, and still lowers.
#[cfg(feature = "mutants")]
#[test]
fn one_node_handshakes_only_under_the_mutant() {
    let rel = "fused/mlp";
    let plain = one_node(rel);
    assert!(!plain.contains("ring mesh") && !plain.contains("wait slot"));
    let forced = under("l2.lower/handshake-always", || one_node(rel));
    assert!(
        forced.contains("ring mesh") && forced.contains("wait slot"),
        "{forced}"
    );
}

#[cfg(feature = "mutants")]
fn under<T>(name: &'static str, f: impl FnOnce() -> T) -> T {
    let state = State {
        active: Some(name),
        ..State::default()
    };
    scoped(state, f)
}

/// The witnesses: with the modulo model's latencies fixed it no longer reads
/// the machine's; with the shape unchecked the refusal goes.
#[cfg(feature = "mutants")]
#[test]
fn mutants_of_the_machine_facts_are_caught() {
    let slow_core = || Core::of(&Target::of(&slow())).timing.math_math;
    assert_eq!(slow_core(), 60);
    assert_ne!(under("l2.vector/timing-constant", slow_core), 60);
    let got = under("l2.vector/shape-unchecked", || modulo(&narrow()));
    assert_ne!(got, Err("L2.VC.025".to_string()));
}
