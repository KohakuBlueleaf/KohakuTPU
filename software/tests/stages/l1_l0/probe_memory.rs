//! Physical ownership and publication across host-separated probe packages.

use std::collections::BTreeMap;

use ktpu_isa::Machine;
use ktpu_l0::format::{Alu, Cond};
use ktpu_l0::program::{BuildOpts, Program};
use ktpu_l0::{Facts, Package};
use ktpu_verify::{check, check_scheduled, schedule, Kind, Target};

fn package(machine: &Machine, node: u32, program: &Program) -> Package {
    let facts = Facts::of_mesh(machine, node).expect("facts");
    program
        .build(&facts, BuildOpts::default())
        .expect("package")
        .build(false, false)
}

#[test]
fn cpu_strobes_preserve_other_words() {
    let machine = Machine::named("multimesh_v9t6").expect("machine");
    let target = Target::from_machine(&machine, Some(3)).expect("target");
    for overlap in [false, true] {
        let nodes = (0..3)
            .map(|n| {
                let mut p = Program::new();
                let offset = if overlap { 0 } else { u64::from(n) * 8 };
                p.store(vec![(4096 + offset, u64::from(n) + 1)], false);
                (n, package(&machine, n, &p))
            })
            .collect();
        let found = check(&[nodes], &target);
        if overlap {
            assert!(found.iter().any(|f| f.kind == Kind::Race), "{found:?}");
        } else {
            assert!(!found.iter().any(|f| f.kind.refuses()), "{found:?}");
        }
    }
}

#[test]
fn host_barriers_bound_slot_contributors() {
    let machine = Machine::named("multimesh_v9t6").expect("machine");
    let target = Target::from_machine(&machine, Some(3)).expect("target");
    for (count, fenced) in [(2, true), (1, true), (2, false)] {
        let mut nodes = BTreeMap::new();
        for n in 1..3 {
            let mut p = Program::new();
            p.store(vec![(4096 + u64::from(n) * 64, 1)], false);
            p.ring(0, 2, 1, fenced);
            nodes.insert(n, package(&machine, n, &p));
        }
        let mut p = Program::new();
        p.wait_slot(2, count, true);
        p.load(0, 4160, None, false);
        p.load(1, 4224, None, false);
        nodes.insert(0, package(&machine, 0, &p));
        let scheduled = schedule(&nodes, &BTreeMap::new(), &target).expect("schedule");
        for found in [
            check(&[nodes.clone(), nodes.clone(), nodes.clone()], &target),
            check_scheduled(&scheduled, &[nodes.clone(), nodes.clone()], &target),
        ] {
            if count == 2 && fenced {
                assert!(!found.iter().any(|f| f.kind.refuses()), "{found:?}");
            } else {
                assert!(found.iter().any(|f| f.kind == Kind::Race), "{found:?}");
            }
        }
    }
}

#[test]
fn pinned_floor_does_not_rescan_physical_history() {
    let machine = Machine::named("multimesh_v9t6").expect("machine");
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let mut writer = Program::new();
    for word in 0..256 {
        writer.store(vec![(4096 + 64 * word, word)], false);
    }
    writer.alu(0, Alu::Mov, 0, 0, true);
    writer.label("work").expect("label");
    writer.alu(0, Alu::Add, 0, 1, true);
    writer.jump("work", Cond::Ltu, 0, 2000, true);
    writer.ring(1, 2, 1, true);
    let mut reader = Program::new();
    reader.wait_slot(2, 1, true);
    reader.load(1, 4096, None, false);
    let nodes = BTreeMap::from([
        (0, package(&machine, 0, &writer)),
        (1, package(&machine, 1, &reader)),
    ]);
    let before = ktpu_verify::work::retirement_visits();
    let found = check(&[nodes], &target);
    let visits = ktpu_verify::work::retirement_visits() - before;
    assert!(!found.iter().any(|f| f.kind.refuses()), "{found:?}");
    assert!(visits < 32, "a pinned reader cannot retire writer histories: {visits}");
}

#[test]
fn scalar_poll_exits_retain_observation_relations() {
    let machine = Machine::named("multimesh_v9t6").expect("machine");
    let target = Target::from_machine(&machine, Some(1)).expect("target");
    for unsafe_count in [false, true] {
        let mut p = Program::new();
        p.ctrl(vec![(0x1c8, 1)], false);
        p.alu(3, Alu::Mov, 0, 0, true);
        p.label("rise").expect("label");
        p.load(4, 0x1c8, None, true);
        p.alu(3, Alu::Add, 3, 1, true);
        p.alu(5, Alu::Mul, 4, 4, true);
        p.alu(5, Alu::Add, 5, 3, false);
        p.jump("rise", Cond::Ltu, 5, 4, true);
        if unsafe_count {
            p.jump("fall_setup", Cond::Ne, 3, 2, true);
        } else {
            p.jump("fall_setup", Cond::Ne, 4, 0, true);
            p.jump("fall_setup", Cond::Eq, 3, 4, true);
        }
        p.wait_bell(0, 1);
        p.label("fall_setup").expect("label");
        p.alu(7, Alu::Mov, 0, 0, true);
        p.label("fall").expect("label");
        p.load(6, 0x1c8, None, true);
        p.alu(7, Alu::Add, 7, 1, true);
        p.jump("fall", Cond::Ne, 6, 0, true);
        p.jump("end", Cond::Eq, 6, 0, true);
        p.wait_bell(0, 1);
        p.label("end").expect("label");
        let nodes = BTreeMap::from([(0, package(&machine, 0, &p))]);
        let found = check(std::slice::from_ref(&nodes), &target);
        if unsafe_count {
            assert!(found.iter().any(|f| f.kind == Kind::Deadlock), "{found:?}");
        } else {
            assert!(!found.iter().any(|f| f.kind.refuses()), "{found:?}");
            let scheduled = schedule(&nodes, &BTreeMap::new(), &target).expect("schedule");
            let found = check_scheduled(&scheduled, &[], &target);
            assert!(!found.iter().any(|f| f.kind.refuses()), "{found:?}");
            let mut other_cpu = target.clone();
            other_cpu.facts.node_poll = None;
            let found = check(&[nodes], &other_cpu);
            assert!(found.iter().any(|f| f.kind == Kind::Loop), "{found:?}");
        }
    }
    for (offset, condition, flush) in [
        (0x1c8, Cond::Eq, true),
        (0x10, Cond::Ne, true),
        (0x1c8, Cond::Ne, false),
    ] {
        let mut p = Program::new();
        if flush {
            p.ctrl(vec![(0x1c8, 1)], false);
        }
        p.alu(1, Alu::Mov, 0, 0, true);
        p.label("poll").expect("label");
        p.load(2, offset, None, true);
        p.alu(1, Alu::Add, 1, 1, true);
        p.jump("poll", condition, 2, 0, true);
        let found = check(&[BTreeMap::from([(0, package(&machine, 0, &p))])], &target);
        assert!(found.iter().any(|f| f.kind == Kind::Loop), "{found:?}");
    }
}

#[test]
fn flush_completion_does_not_publish_uncached_remote_stores() {
    let machine = Machine::named("multimesh_v9t6").expect("machine");
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let mut p = Program::new();
    p.store(vec![(4096, 1)], false);
    p.ctrl(vec![(0x1c8, 1)], false);
    p.alu(1, Alu::Mov, 0, 0, true);
    p.label("fall").expect("label");
    p.load(2, 0x1c8, None, true);
    p.alu(1, Alu::Add, 1, 1, true);
    p.jump("fall", Cond::Ne, 2, 0, true);
    p.ring(0, 2, 1, false);
    let mut reader = Program::new();
    reader.wait_slot(2, 1, true);
    reader.load(0, 4096, None, false);
    let found = check(
        &[BTreeMap::from([
            (1, package(&machine, 1, &p)),
            (0, package(&machine, 0, &reader)),
        ])],
        &target,
    );
    assert!(found.iter().any(|f| f.kind == Kind::Race), "{found:?}");
    assert!(!found.iter().any(|f| f.kind == Kind::Loop), "{found:?}");
}
