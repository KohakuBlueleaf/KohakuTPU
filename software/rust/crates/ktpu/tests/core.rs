//! The pipeline builds for the machine's vector core (`vc.regs`,
//! `vc.rf_copies`): multimesh_v9t6's is 128 registers, 2 read copies, not
//! the text default (32 / 4, L1.MOD.003).

#![cfg(feature = "stack")]

use ktpu::{Backend, Stack};
use ktpu_machine::Machine;
use serde_json::Value;
use std::path::Path;

const BOARD: &str = "multimesh_v9t6";

fn v9t6() -> Machine {
    let m = Machine::named(BOARD).unwrap();
    assert_eq!((m.vc.regs, m.vc.rf_copies), (128, 2), "{BOARD}'s core");
    m
}

/// A golden kernel's L2 text with its `target` line naming no core.
fn l2_naming_no_core() -> String {
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/e2e/examples-dsl-activation.json");
    let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    case["l2"]
        .as_str()
        .unwrap()
        .lines()
        .map(|l| match l.trim_start().starts_with("target ") {
            true => l
                .split_whitespace()
                .filter(|w| !w.starts_with("vregs=") && !w.starts_with("vcopies="))
                .collect::<Vec<_>>()
                .join(" "),
            false => l.to_string(),
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n"
}

#[test]
fn lowering_without_the_passes_takes_the_machines_core() {
    let m = v9t6();
    let p = Stack.parse_l2(&l2_naming_no_core()).unwrap();
    let l1 = Stack.lower(&p, &p.name, &m).unwrap();
    assert_eq!((l1.vregs, l1.vcopies), (128, 2));
}

#[test]
fn an_l1_naming_no_core_is_scheduled_for_the_machines() {
    let m = v9t6();
    let p = Stack.parse_l2(&l2_naming_no_core()).unwrap();
    let l1 = Stack
        .lower(&Stack.passes(&p, &p.name, &m).unwrap(), &p.name, &m)
        .unwrap();
    let text: String = l1
        .source
        .lines()
        .map(|l| match l.trim_start().starts_with("target ") {
            true => "target ktpu.v9".to_string(),
            false => l.to_string(),
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    let bare = Stack.parse_l1(&text).unwrap();
    assert_eq!((bare.vregs, bare.vcopies), (32, 4), "the text default");
    let got = Stack.schedule(&bare, &m).unwrap();
    assert_eq!((got.vregs, got.vcopies), (128, 2));
    assert_eq!(
        Stack.print_l1(&got),
        Stack.print_l1(&Stack.schedule(&l1, &m).unwrap()),
        "the same schedule as the L1 that names the machine's core"
    );
}

#[test]
fn an_l1_naming_another_core_is_refused() {
    let m = v9t6();
    let p = Stack.parse_l2(&l2_naming_no_core()).unwrap();
    let l1 = Stack
        .lower(&Stack.passes(&p, &p.name, &m).unwrap(), &p.name, &m)
        .unwrap();
    let text: String = l1
        .source
        .lines()
        .map(|l| match l.trim_start().starts_with("target ") {
            true => "target ktpu.v9 vregs=64".to_string(),
            false => l.to_string(),
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    let other = Stack.parse_l1(&text).unwrap();
    let e = Stack.schedule(&other, &m).unwrap_err();
    assert_eq!(e.clause, "STACK.CORE", "{e}");
}
