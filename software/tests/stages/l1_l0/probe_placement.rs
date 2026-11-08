//! Relocatable staging parameters and arenas retain their destination route.

use ktpu::{Backend, CardStack};
use ktpu_isa::Machine;

#[test]
fn staging_publication_uses_the_declared_node_before_binding() {
    let machine = Machine::named("multimesh_v9t6").expect("machine");
    let backend = CardStack::default();
    for scratch in [false, true] {
        for fenced in [true, false] {
            let flag = if fenced { "" } else { " plain" };
            let source = format!(
                "target ktpu.v9
fn publish.l1(y: u64[1] @stage(node=2))
    node 1
        store y <- 7
        ring mesh=2 slot=1 amount=1{flag}
    node 2
        wait slot 1 n=1 consume
        r0 = load y
"
            );
            let module = backend.parse_l1(&source).expect("parse");
            let caller = Vec::new();
            let programs = backend
                .programs(
                    &module,
                    "publish",
                    scratch.then_some(caller.as_slice()),
                    &machine,
                )
                .expect("programs");
            backend
                .order(&programs, &machine)
                .expect("relocatable order");
            let found = backend.check(&programs, &machine).expect("check");
            if fenced {
                assert!(!found.iter().any(|f| backend.refuses(f)), "{found:?}");
            } else {
                assert!(found.iter().any(|f| f.clause == "L1.VER.009"), "{found:?}");
            }
        }
    }
}
