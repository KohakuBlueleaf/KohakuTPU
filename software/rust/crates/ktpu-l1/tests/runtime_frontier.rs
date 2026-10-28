//! Runtime-frontier waits survive dynamic IMEM residency rewriting.
use std::collections::BTreeMap;

use ktpu_l0::format::F_IMM;
use ktpu_l0::Op;
use ktpu_l1::{
    build::{self, Builder, Opts},
    lower::{imem::Cores, residency},
    read,
};
use ktpu_machine::Machine;

#[test]
fn dynamic_residency_keeps_runtime_waits_after_dispatch_elision() {
    let machine = Machine::named("multimesh_v9t6").unwrap();
    let text = "target ktpu.v9\nimage k()\n    halt\nfn f.l1()\n    r0 = set 0\n    label again\n    send vc[0]\n        run k()\n    wait vc[0]\n    r0 = add r0, 1\n    goto again if r0 < 3\n";
    let module = read(text, "<frontier>").unwrap();
    let programs = build::programs(&module, "f", &BTreeMap::new(), &machine, None, None).unwrap();
    let builder = Builder::new(&machine).unwrap();
    let p = builder
        .build(&programs[0], Opts::default())
        .unwrap()
        .build(true, false);
    let expected = p
        .steps
        .iter()
        .filter(|s| s.op == Op::Await && s.flags == F_IMM)
        .count();
    assert!(expected > 0);
    let mut cores = Cores::new(&machine).unwrap();
    for _ in 0..2 {
        let result = residency::package(&p, &mut cores).unwrap();
        assert_eq!(
            result
                .steps
                .iter()
                .filter(|s| s.op == Op::Await && s.flags == F_IMM)
                .count(),
            expected
        );
    }
}
