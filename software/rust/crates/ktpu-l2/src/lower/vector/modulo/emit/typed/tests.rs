use super::*;
use crate::lower::vector::gen::print;
use crate::lower::vector::modulo::{derive, parse};

// A synthetic core keeps emission fixtures independent of machine-file availability.
fn core() -> Core {
    Core {
        timing: Params {
            math_math: 20,
            xbar: 7,
            unpack_math: 12,
            math_pack: 20,
            cond: 2,
            seti: 3,
            mx7_issue_gap: 1,
            mx7_pack_words: 9,
            chunks: 8,
            lanes: 16,
            banks: 2,
        },
        lanes: 16,
        chunks: 8,
        rf_banks: 2,
        preds: 3,
        loop_s: 15,
    }
}

fn fixture(k: i64, stages: i64) -> (Plan, Plan, Vec<Stmt>, Vec<Ring>) {
    let core = core();
    let symbol = |n| ir::Symbol(n).operand(ir::Select::Stride(2));
    let templates = [
        ("vunpk {0}[2:] <- l1[13] f16 n=2", vec![0]),
        ("vcmplt p1[2:], {0}[2:], k1 vl=32", vec![0]),
        ("vmov {0}[2:], {1}[2:] vl=32 if=p1", vec![1, 0]),
        ("vpack {0}[2:] -> l1[101] f16 n=2", vec![1]),
    ];
    let stmts = vec![
        ir::memory(
            ir::Memory::Unpack,
            symbol(0),
            13,
            Some("f16"),
            false,
            vec![ir::kw("n", Term::int(2))],
        )
        .unwrap(),
        ir::math(
            "vcmplt",
            ir::reg("p1", ir::Select::Stride(2)),
            vec![symbol(0), Term::name("k1")],
            vec![ir::kw("vl", Term::int(32))],
        ),
        ir::math(
            "vmov",
            symbol(1),
            vec![symbol(0)],
            vec![ir::kw("vl", Term::int(32)), ir::kw("if", Term::name("p1"))],
        ),
        ir::memory(
            ir::Memory::Pack,
            symbol(1),
            101,
            Some("f16"),
            false,
            vec![ir::kw("n", Term::int(2))],
        )
        .unwrap(),
    ];
    let mut old = vec![];
    let mut typed = vec![];
    for (i, ((tmpl, ids), st)) in templates.iter().zip(&stmts).enumerate() {
        let syms: Vec<_> = ids.iter().copied().map(ir::Symbol).collect();
        let mut a = parse(
            tmpl,
            &syms.iter().map(ir::Symbol::name).collect::<Vec<_>>(),
            4,
            &core,
        );
        let mut z = derive(st, &syms, 4, &core);
        z.tmpl = (*tmpl).into();
        assert_eq!(a, z);
        let time = 10 * (i as i64 + 1) + if i > 0 { 100 * (stages - 1) } else { 0 };
        a.t = time;
        a.e = time + a.occ;
        z.t = time;
        z.e = time + z.occ;
        old.push(a);
        typed.push(z);
    }
    let p = Plan {
        ops: old,
        ii: 100,
        k,
        stages,
        phys: (0..2)
            .flat_map(|s| (0..k).map(move |j| ((format!("%{s}"), j), s * k + j)))
            .collect(),
        fill_t: Some(0),
        drain_t: Some(50 + 100 * (stages - 1)),
        s0: 0,
        core,
    };
    let mut z = p.clone();
    z.ops = typed;
    let rings = vec![
        Ring {
            ad: 7,
            kind: "load",
            base: 11,
            words: 8,
            slots: (0..k).map(|j| 11 + 16 * j).collect(),
        },
        Ring {
            ad: 4,
            kind: "store",
            base: 99,
            words: 8,
            slots: (0..k).map(|j| 99 + 16 * j).collect(),
        },
    ];
    (p, z, stmts, rings)
}

fn paired(
    a: &Plan,
    z: &Plan,
    stmts: &[Stmt],
    rings: &[Ring],
    trips: i64,
    tail: &str,
) -> (bool, bool) {
    start_periodicity();
    let old = super::super::emit(a, rings, trips, &HashMap::new(), tail);
    let got = emit(z, stmts, rings, trips, &HashMap::new(), tail);
    let (_, nonperiodic) = check_periodicity();
    match (old, got) {
        (Ok(Some((lines, cycles))), Ok(Some((events, actual_cycles)))) => {
            assert_eq!(cycles, actual_cycles);
            let actual = print::emit(&mut ir::nest(&events).unwrap()).unwrap();
            assert_eq!(
                actual,
                crate::lower::vector::indent(&lines).join("\n") + "\n"
            );
            (true, nonperiodic)
        }
        (Ok(None), Ok(None)) => (false, nonperiodic),
        (Err(a), Err(z)) => {
            assert_eq!(a, z);
            (false, nonperiodic)
        }
        (a, z) => panic!("emission mismatch: {a:?} / {z:?}"),
    }
}

#[test]
fn modulo_typed_tails_rings_predicates_and_loops() {
    let mut emitted = 0;
    let mut refused = 0;
    for k in 2..=8 {
        for stages in 1..=3 {
            let (a, mut z, stmts, rings) = fixture(k, stages);
            // Only the diagnostic mnemonic survives; operands cannot come from this bridge.
            for op in &mut z.ops {
                op.tmpl = format!(
                    "{} not an instruction",
                    op.tmpl.split_whitespace().next().unwrap()
                );
            }
            for trips in [1, 2, 3, 7, 8, 9, 17] {
                for tail in TAILS {
                    if paired(&a, &z, &stmts, &rings, trips, tail).0 {
                        emitted += 1;
                    } else {
                        refused += 1;
                    }
                }
            }
        }
    }
    assert!(emitted > 0 && refused > 0);
    eprintln!("typed modulo fixture: emitted={emitted} refused={refused}; mismatches=0");
    let (a, z, stmts, rings) = fixture(8, 1);
    let mut scheduled = 0;
    for regs in [8, 16, 32] {
        for lead in [0, 25, 100] {
            let pins = HashMap::new();
            let old =
                crate::lower::vector::modulo::plan(&a.ops, regs, &pins, 8, 400, lead, 1, &a.core);
            let got =
                crate::lower::vector::modulo::plan(&z.ops, regs, &pins, 8, 400, lead, 1, &z.core);
            assert_eq!(old.is_some(), got.is_some());
            if let (Some(old), Some(got)) = (old, got) {
                assert_eq!(old.ops, got.ops);
                assert_eq!(old.phys, got.phys);
                for trips in [1, 7, 17] {
                    for tail in TAILS {
                        paired(&old, &got, &stmts, &rings, trips, tail);
                        scheduled += 1;
                    }
                }
            }
        }
    }
    assert!(scheduled > 0);
    eprintln!("typed modulo scheduled fixture: tails={scheduled}; mismatches=0");
}

#[test]
fn modulo_typed_periodicity_and_error_verdicts() {
    let (mut a, _, _, _) = fixture(2, 1);
    let st = ir::math(
        "vmov",
        ir::Symbol(0).operand(ir::Select::Whole),
        vec![Term::name("k0")],
        vec![],
    );
    let mut op = parse("vmov {0}, k0", &["%0".into()], 4, &a.core);
    op.t = 100;
    op.e = 108;
    a.ops = vec![op];
    a.fill_t = None;
    a.drain_t = None;
    let mut z = a.clone();
    z.ops[0] = derive(&st, &[ir::Symbol(0)], 4, &a.core);
    z.ops[0].t = 100;
    z.ops[0].e = 108;
    assert_eq!(paired(&a, &z, &[st], &[], 6, "explicit"), (false, true));
    let (a, z, stmts, mut rings) = fixture(2, 1);
    rings[0].base = 1000;
    assert!(!paired(&a, &z, &stmts, &rings, 6, "explicit").0);
    let mut late_a = a.clone();
    let mut late_z = z.clone();
    late_a.fill_t = Some(50);
    late_z.fill_t = Some(50);
    assert!(
        super::super::emit(&late_a, &rings, 6, &HashMap::new(), "explicit")
            .unwrap()
            .is_none()
    );
    assert!(!paired(&late_a, &late_z, &stmts, &rings, 6, "explicit").0);
    let (mut a, mut z, stmts, rings) = fixture(2, 1);
    a.ops[0].t = 15;
    z.ops[0].t = 15;
    a.ops[1].t = 5;
    z.ops[1].t = 5;
    assert!(!paired(&a, &z, &stmts, &rings, 6, "explicit").0);
}

#[test]
fn modulo_typed_instance_field_boundaries() {
    let (a, _, _, rings) = fixture(6, 1);
    let st = ir::math(
        "vmov",
        ir::Symbol(1).operand(ir::Select::Stride(0)),
        vec![ir::crossbar(
            "xor",
            ir::Symbol(0).operand(ir::Select::Chunk(3)),
            1,
        )],
        vec![
            ir::kw("unless", Term::name("p1")),
            ir::kw("vl", Term::int(16)),
        ],
    );
    let op = parse(
        "vmov {0}[0:], xor({1}[3], 1) unless=p1 vl=16",
        &["%1".into(), "%0".into()],
        4,
        &a.core,
    );
    let pinned = HashMap::from([("%0".into(), 27)]);
    for j in -7..=13 {
        let copy = floor_mod(j, a.k);
        let pc = 1 + floor_mod(copy, pred_copies(a.k, a.core.preds));
        let got = instance(&st, &op, &a, &rings, &pinned, j).unwrap();
        let text = print::emit(&mut [got]).unwrap();
        assert_eq!(
            text,
            format!(
                "vmov v{}[0:], xor(v27[3], 1) unless=p{pc} vl=16\n",
                a.phys[&("%1".into(), copy)]
            )
        );
    }
    let mut x = ir::seti("s1", Term::Float(f64::NAN));
    let mut y = x.clone();
    x.line = 10;
    y.line = 20;
    y.col = 5;
    assert!(same_stmt(&x, &y));
    y.args = vec![ir::kw("s1", Term::Float(-0.0))];
    x.args = vec![ir::kw("s1", Term::Float(0.0))];
    assert!(!same_stmt(&x, &y));
}
