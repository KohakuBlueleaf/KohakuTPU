use std::collections::HashMap;

use ktpu_l1::text::{parse, Stmt, Term};
use ktpu_l2::lower::vector::gen::typed as ir;
use ktpu_l2::lower::vector::modulo::{
    internals::{emit, emit_exhaustive, last_intervals, last_materialized},
    Core, MOp, Plan, Ring,
};
use ktpu_l2::target::Target;

use super::common::card;

fn stream() -> (Plan, Vec<Stmt>, [Ring; 2]) {
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
    (plan, stmts, rings)
}

#[test]
fn kernel_tail_cannot_read_past_the_source() {
    let (plan, stmts, rings) = stream();
    let result = emit(&plan, &stmts, &rings, 4, &HashMap::new(), "kernel")
        .expect("valid register ownership");
    assert!(
        result.is_none(),
        "a fifth fill has no source transfer permission"
    );
}

fn effects(stmts: &[Stmt], scalars: &mut HashMap<String, i128>, out: &mut Vec<Stmt>) {
    for st in stmts {
        match st.op.as_str() {
            "seti" => {
                let Term::Assign(reg, value) = &st.args[0] else {
                    panic!("scalar assignment")
                };
                scalars.insert(
                    reg.as_name().expect("register").into(),
                    value.as_int().expect("loop count"),
                );
            }
            "loop" => {
                let count = scalars[st.args[0].as_name().expect("loop register")];
                for _ in 0..count {
                    effects(&st.body, scalars, out);
                }
            }
            "vsync" => {}
            _ => out.push(st.clone()),
        }
    }
}

#[test]
fn periodic_emission_materializes_only_the_image_and_keeps_every_memory_effect() {
    let (mut plan, stmts, mut rings) = stream();
    plan.k = 2;
    plan.phys.insert(("%x".into(), 1), 1);
    rings[0].slots.push(1);
    rings[1].slots.push(9);
    let mut first_work = None;
    let mut first_intervals = None;
    for trips in [8, 128] {
        let (events, cycles) = emit(&plan, &stmts, &rings, trips, &HashMap::new(), "explicit")
            .expect("valid register ownership")
            .expect("explicit tail");
        let work = last_materialized();
        let intervals = last_intervals();
        if let Some(previous) = first_intervals {
            assert_eq!(intervals, previous, "schedule replay is periodic");
        }
        first_intervals = Some(intervals);
        if let Some(previous) = first_work {
            assert_eq!(work, previous, "operand spelling is independent of trips");
        } else {
            first_work = Some(work);
        }
        let image = ir::nest(events).expect("structured loop");
        let (reference, reference_cycles) =
            emit_exhaustive(&plan, &stmts, &rings, trips, &HashMap::new(), "explicit")
                .expect("exhaustive register ownership")
                .expect("exhaustive explicit tail");
        assert!(
            intervals < last_intervals(),
            "the reference visits every trip"
        );
        assert_eq!(cycles, reference_cycles);
        assert_eq!(
            ktpu_l1::text::emit(&image),
            ktpu_l1::text::emit(&ir::nest(reference).expect("reference loop")),
            "periodic replay preserves all sync slacks and loop counts"
        );
        let mut actual = Vec::new();
        effects(&image, &mut HashMap::new(), &mut actual);
        let mut expected = Vec::new();
        // The placed order is U, P, drain, fill in each initiation interval.
        // The first interval only fills; the last has no next fill.
        for n in 0..=trips {
            if n > 0 {
                let copy = (n - 1) % 2;
                for (index, template) in stmts.iter().enumerate() {
                    let mut st = template.clone();
                    st.args[0] = Term::name(format!("v{copy}"));
                    st.args[2] = ir::l1(rings[index].slots[copy as usize]);
                    expected.push(st);
                }
                expected.push(
                    ir::memory(
                        ir::Memory::Drain,
                        Term::name("a1"),
                        rings[1].slots[copy as usize],
                        None,
                        true,
                        vec![],
                    )
                    .expect("drain"),
                );
            }
            if n < trips {
                expected.push(
                    ir::memory(
                        ir::Memory::Fill,
                        Term::name("a0"),
                        rings[0].slots[(n % 2) as usize],
                        None,
                        true,
                        vec![],
                    )
                    .expect("fill"),
                );
            }
        }
        assert_eq!(ktpu_l1::text::emit(&actual), ktpu_l1::text::emit(&expected));
    }
    // The kernel tail ends before the next fill when trips is odd for K=2.
    let (kernel, cycles) = emit(&plan, &stmts, &rings, 129, &HashMap::new(), "kernel")
        .expect("kernel dataflow")
        .expect("in-domain kernel tail");
    let (reference, reference_cycles) =
        emit_exhaustive(&plan, &stmts, &rings, 129, &HashMap::new(), "kernel")
            .expect("exhaustive kernel dataflow")
            .expect("exhaustive kernel tail");
    assert_eq!(cycles, reference_cycles);
    assert_eq!(
        ktpu_l1::text::emit(&ir::nest(kernel).expect("kernel loop")),
        ktpu_l1::text::emit(&ir::nest(reference).expect("reference kernel loop"))
    );
}
