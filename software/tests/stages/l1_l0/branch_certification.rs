use std::collections::BTreeMap;

use ktpu_isa::unit::vector::{VWord, VecCu, VecIsa};
use ktpu_l0::format::Cond;
use ktpu_l0::program::{BuildOpts, Program};
use ktpu_l0::Facts;

use super::common::card;

#[test]
fn unsafe_forward_successor_refuses_certification() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let mut p = Program::new();
    p.load(0, 4096, None, false);
    p.jump("end", Cond::Ne, 0, 0, true);
    p.wait_bell(0, 1);
    p.label("end").expect("label");
    let package = p
        .build(&facts, BuildOpts::default())
        .expect("package")
        .build(false, false);
    let target = ktpu_verify::Target::from_machine(&machine, Some(1)).expect("target");
    let findings = ktpu_verify::check(&[BTreeMap::from([(0, package)])], &target);
    assert!(
        findings.iter().any(|f| {
            f.kind == ktpu_verify::Kind::Deadlock
                && f.at.contains("node 0")
                && f.text.contains("bell 0: 0 of 1")
                && f.text.contains("path:")
                && f.text.contains("fallthrough")
        }),
        "the feasible fallthrough must report its unsatisfied bell wait: {findings:?}"
    );
}

#[test]
fn unknown_forward_branch_with_safe_successors_certifies() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let mut p = Program::new();
    p.load(0, 4096, None, false);
    p.jump("end", Cond::Ne, 0, 0, true);
    p.load(1, 8192, None, false);
    p.label("end").expect("label");
    let package = p
        .build(&facts, BuildOpts::default())
        .expect("package")
        .build(false, false);
    let target = ktpu_verify::Target::from_machine(&machine, Some(1)).expect("target");
    let nodes = BTreeMap::from([(0, package)]);
    let findings = ktpu_verify::check(std::slice::from_ref(&nodes), &target);
    assert!(
        !findings.iter().any(|f| f.kind.refuses()),
        "both successors terminate without a wait or transfer: {findings:?}"
    );
    let scheduled = ktpu_verify::schedule(&nodes, &BTreeMap::new(), &target)
        .expect("both successors have a safe dispatch order");
    let findings = ktpu_verify::check_scheduled(&scheduled, &[], &target);
    assert!(
        !findings.iter().any(|f| f.kind.refuses()),
        "the scheduled continuation must also certify: {findings:?}"
    );
}

#[test]
fn retained_branch_outcome_controls_the_next_package() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let isa = VecIsa::new(&machine).expect("vector ISA");
    let coord = facts.units["VC"][0];
    let halt = VWord::Halt.encode(&isa).expect("halt");
    let wait = VWord::Wait {
        waiter: 0,
        counter: 0,
        n: 1,
        q: 0,
    }
    .encode(&isa)
    .expect("wait");
    let imem = |addr, word| VecCu::Imem { addr, word }.encode(&isa).expect("IMEM");
    let mut first = Program::new();
    first.send(coord, vec![imem(0, halt), imem(1, halt)], vec![]);
    first.barrier();
    first.load(0, 4096, None, false);
    first.jump("waiting", Cond::Ne, 0, 0, true);
    first.jump("end", Cond::Always, 0, 0, true);
    first.label("waiting").expect("label");
    first.send(coord, vec![imem(0, wait)], vec![]);
    first.label("end").expect("label");
    let first = BTreeMap::from([(
        0,
        first
            .build(&facts, BuildOpts::default())
            .expect("first package")
            .build(false, false),
    )]);
    let mut second = Program::new();
    second.send(
        coord,
        vec![VecCu::Run { pc: 0 }.encode(&isa).expect("RUN")],
        vec![],
    );
    let second = BTreeMap::from([(
        0,
        second
            .build(&facts, BuildOpts::default())
            .expect("second package")
            .build(false, false),
    )]);
    let target = ktpu_verify::Target::from_machine(&machine, Some(1)).expect("target");
    let scheduled = ktpu_verify::schedule(&first, &BTreeMap::new(), &target)
        .expect("loading either retained image is safe");
    let findings = ktpu_verify::check_scheduled(&scheduled, std::slice::from_ref(&second), &target);
    let full = ktpu_verify::check(&[first, second], &target);
    for findings in [findings, full] {
        assert!(
            findings.iter().any(|f| {
                f.kind == ktpu_verify::Kind::Deadlock
                    && f.text.contains("vwait on the front end")
                    && f.text.contains("path:")
                    && (f.text.contains("taken") || f.text.contains("taken: true"))
            }),
            "the taken path retains a wait that blocks the second RUN: {findings:?}"
        );
    }
}

#[test]
fn complementary_forward_conditions_certify() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let mut p = Program::new();
    p.load(0, 4096, None, false);
    p.jump("end", Cond::Ne, 0, 0, true);
    p.jump("end", Cond::Eq, 0, 0, true);
    p.wait_bell(0, 1);
    p.label("end").expect("label");
    let package = p
        .build(&facts, BuildOpts::default())
        .expect("package")
        .build(false, false);
    let target = ktpu_verify::Target::from_machine(&machine, Some(1)).expect("target");
    let nodes = BTreeMap::from([(0, package)]);
    let findings = ktpu_verify::check(std::slice::from_ref(&nodes), &target);
    assert!(
        !findings.iter().any(|f| f.kind.refuses()),
        "every loaded value reaches end without waiting: {findings:?}"
    );
    let scheduled = ktpu_verify::schedule(&nodes, &BTreeMap::new(), &target)
        .expect("every feasible successor has a safe dispatch order");
    let findings = ktpu_verify::check_scheduled(&scheduled, &[], &target);
    assert!(
        !findings.iter().any(|f| f.kind.refuses()),
        "replayed predicates must exclude the absent bell: {findings:?}"
    );
}
