use super::*;
use crate::lower::vector::{
    assign_typed,
    gen::{print, Code},
    pat,
};
use crate::target::Target;
use ktpu_machine::Machine;

fn core() -> Core {
    Core::of(&Target::of(&Machine::named("multimesh_v9t6").unwrap()))
}

fn op(tmpl: &str, stmt: Stmt, ids: &[u32]) -> (Code, Event) {
    let syms: Vec<_> = ids.iter().copied().map(ir::Symbol).collect();
    (
        Code::Op(tmpl.into(), syms.iter().map(ir::Symbol::name).collect()),
        Event::Op { stmt, syms },
    )
}

fn operand(n: u32, select: ir::Select) -> Term {
    ir::Symbol(n).operand(select)
}

fn bytes(code: &[Event]) -> String {
    let mut stmts = ir::nest(code).unwrap();
    print::emit(&mut stmts).unwrap()
}

fn compare(pairs: &[(Code, Event)], pinned: &HashSet<String>) {
    let legacy: Vec<_> = pairs.iter().map(|p| p.0.clone()).collect();
    let typed: Vec<_> = pairs.iter().map(|p| p.1.clone()).collect();
    let core = core();
    for copies in [2, 3, 4] {
        assert_eq!(
            choose(&typed, copies, &core),
            super::super::choose(&legacy, copies, &core)
        );
        let old = super::super::rotate(legacy.clone(), copies, &core);
        let new = rotate(typed.clone(), copies, &core);
        for regs in 0..=8 {
            let (a, at) = assign_typed::reference(&old, pinned, regs);
            let (z, zt) = assign_typed::assign(&new, pinned, regs);
            assert_eq!(at, zt, "partial allocation trace regs={regs}");
            match (a, z) {
                (Ok(a), Ok(z)) => assert_eq!(
                    bytes(&z),
                    format!("{}\n", crate::lower::vector::indent(&a).join("\n"))
                ),
                (Err(a), Err(z)) => assert_eq!(format!("{a:?}"), format!("{z:?}")),
                (a, z) => panic!("allocation differs {a:?} / {z:?}"),
            }
        }
    }
}

#[test]
fn encounter_order_and_allocation() {
    use ir::Select::Whole as W;
    let mut pairs = vec![];
    for n in 1..=3 {
        pairs.push(op(
            "vmov {0}, k1",
            ir::math("vmov", operand(n, W), vec![Term::name("k1")], vec![]),
            &[n],
        ));
    }
    pairs.push(op(
        "vfma {0}, {1}, {2}, {3}",
        ir::math(
            "vfma",
            operand(4, W),
            vec![operand(1, W), operand(2, W), operand(3, W)],
            vec![],
        ),
        &[4, 1, 2, 3],
    ));
    pairs.push(op(
        "vcmplt p1, {1}, {2}",
        ir::math(
            "vcmplt",
            Term::name("p1"),
            vec![operand(4, W), operand(4, W)],
            vec![],
        ),
        &[5, 4, 4],
    ));
    pairs.push(op(
        "vmov {0}, k0",
        ir::math("vmov", operand(5, W), vec![Term::name("k0")], vec![]),
        &[5],
    ));
    pairs.push(op(
        "vmov {0}, k1 if=p1",
        ir::math(
            "vmov",
            operand(5, W),
            vec![Term::name("k1")],
            vec![ir::kw("if", Term::name("p1"))],
        ),
        &[5],
    ));
    pairs.push(op(
        "vadd {0}, {1}, {1}",
        ir::math(
            "vadd",
            operand(6, W),
            vec![operand(5, W), operand(5, W)],
            vec![],
        ),
        &[6, 5],
    ));
    // A symbol's old mapping remains after death; metadata, not occurrences, owns its lifetime.
    pairs.push(op(
        "vmov {0}, k1",
        ir::math("vmov", operand(7, W), vec![Term::name("k1")], vec![]),
        &[7, 8],
    ));
    for pinned in [HashSet::new(), ["%1".into(), "%5".into()].into()] {
        compare(&pairs, &pinned);
    }
}

#[test]
fn selected_crossbar_walks_and_predicates() {
    use ir::Select::{Chunk as C, Stride as S, Whole as W};
    for (text, select) in [("", W), ("[0:]", S(0)), ("[7:]", S(7)), ("[3]", C(3))] {
        for mode in ["f16", "f32", "gt4", "mx7a", "mx7b"] {
            for pred in [None, Some("if"), Some("unless")] {
                let mut options = vec![ir::kw("vl", Term::int(32))];
                let suffix = pred.map(|p| format!(" {p}=p1")).unwrap_or_default();
                if let Some(p) = pred {
                    options.push(ir::kw(p, Term::name("p1")));
                }
                let pairs = vec![
                    op(
                        &format!("vunpk {{0}}{text} <- l1[8] {mode}"),
                        ir::instruction(
                            "vunpk",
                            vec![
                                operand(1, select),
                                Term::Arrow("<-".into()),
                                ir::l1(8),
                                Term::name(mode),
                            ],
                            vec![],
                        ),
                        &[1],
                    ),
                    op(
                        &format!("vfma {{0}}, {{1}}{text}, xor({{2}}[0:], 1), {{3}} vl=32{suffix}"),
                        ir::math(
                            "vfma",
                            operand(4, W),
                            vec![
                                operand(1, select),
                                ir::crossbar("xor", operand(2, S(0)), 1),
                                operand(3, W),
                            ],
                            options,
                        ),
                        &[4, 1, 2, 3],
                    ),
                    op(
                        "vpack {0} -> l1[16] f16 n=1",
                        ir::instruction(
                            "vpack",
                            vec![
                                operand(4, W),
                                Term::Arrow("->".into()),
                                ir::l1(16),
                                Term::name("f16"),
                            ],
                            vec![ir::kw("n", Term::int(1))],
                        ),
                        &[4],
                    ),
                ];
                compare(&pairs, &HashSet::new());
            }
        }
    }
}

#[test]
fn rotation_edits_preserve_original_count_and_metadata() {
    use ir::Select::{Chunk as C, Stride as S, Whole as W};
    for (text, select) in [("", W), ("[0:]", S(0)), ("[7:]", S(7)), ("[3]", C(3))] {
        for explicit in [false, true] {
            let options = if explicit {
                vec![ir::kw("n", Term::int(1))]
            } else {
                vec![]
            };
            let suffix = if explicit { " n=1" } else { "" };
            let (a, mut z) = op(
                &format!("vunpk {{0}}{text} <- l1[8] f16{suffix}"),
                ir::instruction(
                    "vunpk",
                    vec![
                        operand(1, select),
                        Term::Arrow("<-".into()),
                        ir::l1(8),
                        Term::name("f16"),
                    ],
                    options,
                ),
                &[1, 2],
            );
            for symbol in ["%1", "%2"] {
                let rho = [(symbol.to_string(), 1)].into();
                let Code::Op(t, s) = &a else { unreachable!() };
                let Event::Op { stmt, syms } = &mut z else {
                    unreachable!()
                };
                let mut stmt = stmt.clone();
                apply(&mut stmt, syms, &rho, 8);
                let expected = super::super::apply(t, s, &rho, 8);
                let (_, trace) =
                    assign_typed::reference(std::slice::from_ref(&a), &HashSet::new(), 8);
                assert_eq!(trace, [("%1".into(), 0), ("%2".into(), 1)]);
                let (got, _) = assign_typed::assign(
                    &[Event::Op {
                        stmt,
                        syms: syms.clone(),
                    }],
                    &HashSet::new(),
                    8,
                );
                assert_eq!(
                    bytes(&got.unwrap()),
                    format!("{}\n", pat::format(&expected, &["v0".into(), "v1".into()]))
                );
            }
        }
    }
}

#[cfg(feature = "mutants")]
#[test]
fn bank_mutant_options_match() {
    for active in [
        "l2.banks/rotates-none",
        "l2.banks/rotates-predicated",
        "l2.banks/drops-op",
    ] {
        crate::mutant::scoped(
            crate::mutant::State {
                active: Some(active),
                ..Default::default()
            },
            selected_crossbar_walks_and_predicates,
        );
    }
}
