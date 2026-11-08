//! Feedback credits bound a shared counter without selecting an arrival order.

use std::collections::BTreeMap;

use ktpu_isa::Machine;
use ktpu_l0::format::{Alu, Cond};
use ktpu_l0::program::{BuildOpts, Program};
use ktpu_l0::Facts;
use ktpu_verify::{check, check_scheduled, schedule, Kind, Target};

#[test]
fn feedback_gather_acquires_every_replay_reader() {
    let machine = Machine::named("multimesh_v9t6").expect("machine");
    let target = Target::from_machine(&machine, Some(3)).expect("target");
    for mode in ["gated", "nonconsuming", "bypass", "other_sender"] {
        let mut nodes = BTreeMap::new();
        for node in 0..3 {
            let mut p = Program::new();
            p.alu(0, Alu::Mov, 0, 0, true);
            p.label("replay").expect("label");
            if node == 0 {
                p.store(vec![(4096, 7)], false);
                p.ring(1, 1, 1, true);
                p.ring(2, 1, 1, true);
                p.wait_slot(2, 2, true);
            } else {
                if node == 1 && mode == "bypass" {
                    p.jump("read", Cond::Eq, 0, 1, true);
                }
                p.wait_slot(1, 1, node != 1 || mode != "nonconsuming");
                p.label("read").expect("label");
                p.load(1, 4096, None, false);
                if node == 2 && mode == "other_sender" {
                    p.ring(1, 1, 1, true);
                }
                p.ring(0, 2, 1, true);
            }
            p.alu(0, Alu::Add, 0, 1, true);
            p.jump("replay", Cond::Ltu, 0, 2, true);
            let facts = Facts::of_mesh(&machine, node).expect("facts");
            let package = p.build(&facts, BuildOpts::default()).expect("package");
            nodes.insert(node, package.build(false, false));
        }
        let scheduled = schedule(&nodes, &BTreeMap::new(), &target).expect("schedule");
        for found in [
            check(std::slice::from_ref(&nodes), &target),
            check_scheduled(&scheduled, &[], &target),
        ] {
            if mode == "gated" {
                assert!(!found.iter().any(|f| f.kind.refuses()), "{found:?}");
            } else {
                assert!(
                    found
                        .iter()
                        .any(|f| f.kind == Kind::Race && f.text.contains("0x1000")),
                    "{mode}: {found:?}"
                );
            }
        }
    }
}
