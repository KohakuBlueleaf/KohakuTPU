use std::collections::HashMap;

use ktpu_l1::text::{parse, Term};
use ktpu_l2::lower::vector::modulo::{internals::emit, Core, MOp, Plan, Ring};
use ktpu_l2::target::Target;

use super::common::card;

#[test]
fn kernel_tail_cannot_read_past_the_source() {
    let target = Target::of(&card());
    let mut stmts = parse(
        "vunpk v0 <- l1[0] f16 n=1\nvpack v0 -> l1[8] f16 n=1",
        "modulo",
    )
    .expect("template");
    for st in &mut stmts {
        st.args[0] = Term::name("%x");
    }
    let ops = [('U', 8, 0), ('P', 9, 8)]
        .into_iter()
        .enumerate()
        .map(|(i, (eng, t, l1))| MOp {
            op: stmts[i].op.clone(),
            syms: vec!["%x".into()],
            eng,
            reads: if eng == 'P' {
                vec!["%x".into()]
            } else {
                vec![]
            },
            writes: if eng == 'U' {
                vec!["%x".into()]
            } else {
                vec![]
            },
            occ: 1,
            l1: Some(l1),
            t,
            e: t + 1,
            deps: vec![],
            xreads: vec![],
        })
        .collect();
    let plan = Plan {
        ops,
        ii: 8,
        k: 1,
        stages: 2,
        phys: HashMap::from([(("%x".into(), 0), 0)]),
        fill_t: Some(3),
        drain_t: Some(10),
        s0: 0,
        core: Core::of(&target),
    };
    let rings = [
        Ring {
            ad: 0,
            kind: "load",
            base: 0,
            words: 1,
            slots: vec![0],
        },
        Ring {
            ad: 1,
            kind: "store",
            base: 8,
            words: 1,
            slots: vec![8],
        },
    ];
    let result = emit(&plan, &stmts, &rings, 4, &HashMap::new(), "kernel")
        .expect("valid register ownership");
    assert!(
        result.is_none(),
        "a fifth fill has no source transfer permission"
    );
}
