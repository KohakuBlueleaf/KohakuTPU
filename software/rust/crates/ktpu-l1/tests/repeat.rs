//! Explicit node repetition keeps the dispatched image body independent of count.

use ktpu_l0::{BuildOpts, Facts, Op, Stmt};
use ktpu_l1::{build, text::read};
use ktpu_machine::Machine;
use std::collections::BTreeMap;

#[test]
fn repeated_vector_send_has_one_template_and_full_wait() {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let facts = Facts::of(&machine);
    let mut sizes = Vec::new();
    for count in [1, 2, 31] {
        let text = format!("target ktpu.v9\nimage k()\n    halt\nfn f.l1()\n    send vc[0] repeats={count}\n        run k()\n    barrier\n");
        let module = read(&text, "<repeat>").unwrap();
        let programs =
            build::programs(&module, "f", &BTreeMap::new(), &machine, None, None).unwrap();
        let program = &programs[0].program;
        assert!(matches!(program.steps[0], Stmt::Repeat { repeats, .. } if repeats == count));
        let builder = program.build(&facts, BuildOpts::default()).unwrap();
        let repeated = builder.steps.iter().find(|s| s.op == Op::Repeat).unwrap();
        assert_eq!((repeated.arg >> 32) & 65535, count);
        let wait: u64 = builder
            .steps
            .iter()
            .filter(|s| s.op == Op::Await)
            .map(|s| s.count)
            .sum();
        assert_eq!(wait, repeated.count * count);
        sizes.push((builder.steps.len(), builder.payloads.len()));
    }
    assert!(sizes.windows(2).all(|s| s[0] == s[1]));
}

#[test]
fn cluster_advance_keeps_addresses_in_payload_and_acc_in_register() {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let facts = Facts::of(&machine);
    let text = "target ktpu.v9\nfn f.l1()\n    r0 = set 0\n    r1 = set 0\n    label chunks\n    send mg[0] advance=1\n        fill A0 <- 0x10000 + r0 n=2\n        fill B0 <- 0x20000 + r0 n=2\n        gemm 2x2x1 a=A0 b=B0 acc=r1\n    r0 = set 256\n    r1 = set 1\n    goto chunks if r1 < 0\n";
    let module = read(text, "<advance>").unwrap();
    let programs = build::programs(&module, "f", &BTreeMap::new(), &machine, None, None).unwrap();
    let builder = programs[0]
        .program
        .build(&facts, BuildOpts::default())
        .unwrap();
    let patches: Vec<_> = builder.steps.iter().filter(|s| s.op == Op::Patch).collect();
    assert_eq!(patches.len(), 3);
    assert_eq!(patches.iter().filter(|s| s.arg & (1 << 26) != 0).count(), 2);
    assert!(patches
        .iter()
        .filter(|s| s.arg & (1 << 26) != 0)
        .all(|s| (s.arg >> 8) & 255 == 34));
    assert!(!builder
        .steps
        .iter()
        .any(|s| s.op == Op::Alu && [0x10000, 0x20000].contains(&s.arg)));
    let waits: Vec<_> = builder.steps.iter().filter(|s| s.op == Op::Await).collect();
    assert!(!waits.is_empty());
    assert!(waits
        .iter()
        .all(|s| s.count == 0 && s.flags == ktpu_l0::format::F_IMM));
}

#[test]
fn invalid_repeat_count_is_not_silently_a_single_send() {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    for count in [0, -1, 65536] {
        let text = format!("target ktpu.v9\nimage k()\n    halt\nfn f.l1()\n    send vc[0] repeats={count}\n        run k()\n");
        let module = read(&text, "<repeat>").unwrap();
        let error =
            build::programs(&module, "f", &BTreeMap::new(), &machine, None, None).unwrap_err();
        assert_eq!(error.clause, "L0.STEP.041");
    }
}
