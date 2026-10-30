//! Step and event differential checks against the template trees.

use super::*;
use crate::lower::vector::{
    gen::{print, Code, Gen as OldGen},
    pat, tree as old, BINARY, REDUCE, TERNARY, UNARY,
};

fn resolve(t: &mut Term) {
    match t {
        Term::Name(n) if n.starts_with('%') => *n = format!("v{}", &n[1..]),
        Term::View(n, args) | Term::Call(n, args) => {
            if n.starts_with('%') {
                *n = format!("v{}", &n[1..]);
            }
            for a in args {
                resolve(a);
            }
        }
        Term::Assign(a, b) => {
            resolve(a);
            resolve(b);
        }
        Term::Neg(a) | Term::Kw(_, a) => resolve(a),
        Term::Slice(a, b) => {
            for t in a.iter_mut().chain(b.iter_mut()) {
                resolve(t);
            }
        }
        _ => (),
    }
}

fn instruction(a: &old::Insn, z: &Insn) {
    assert_eq!(a.1, z.syms.iter().map(Symbol::name).collect::<Vec<_>>());
    let text = format!(
        "{}\n",
        pat::format(
            &a.0,
            &a.1.iter()
                .map(|s| format!("v{}", &s[1..]))
                .collect::<Vec<_>>()
        )
    );
    let mut st = z.stmt.clone();
    for t in &mut st.args {
        resolve(t);
    }
    let printed = print::emit(std::slice::from_mut(&mut st)).unwrap();
    assert_eq!(printed.as_bytes(), text.as_bytes());
    assert_eq!(vec![st], ktpu_l1::text::parse(&text, "<legacy>").unwrap());
}

pub(crate) fn events(a: &OldGen, z: &Gen) {
    assert_eq!(a.n, z.n as usize);
    assert_eq!(a.code.len(), z.code.len());
    for (a, z) in a.code.iter().zip(&z.code) {
        match (a, z) {
            (Code::Op(t, s), Event::Op { stmt, syms }) => instruction(
                &(t.clone(), s.clone()),
                &Insn {
                    stmt: stmt.clone(),
                    syms: syms.clone(),
                },
            ),
            (Code::Raw(a), Event::Fixed(st)) => {
                let mut st = st.clone();
                assert_eq!(
                    format!("{a}\n"),
                    print::emit(std::slice::from_mut(&mut st)).unwrap()
                );
            }
            (Code::Raw(a), Event::Open) => assert_eq!(a, "{"),
            (Code::Raw(a), Event::Close) => assert_eq!(a, "}"),
            _ => panic!("event kind mismatch {a:?} {z:?}"),
        }
    }
}

fn steps(a: &old::Steps, z: &Steps) {
    assert_eq!(a.len(), z.len());
    for (a, z) in a.iter().zip(z) {
        assert_eq!(a.len(), z.len());
        for (a, z) in a.iter().zip(z) {
            instruction(a, z);
        }
    }
}

#[test]
fn fused_unfused_trees_all_levels_and_orders() {
    for (_, code) in REDUCE {
        for fused in [false, true] {
            for into in [None, Some(Symbol(99))] {
                for size in [1, 2, 3, 4, 5, 8, 9] {
                    for reverse in [false, true] {
                        let mut regs: Vec<Vec<Symbol>> = (0..size)
                            .map(|i| vec![Symbol(1 + 2 * i), Symbol(2 + 2 * i)])
                            .collect();
                        if reverse {
                            regs.reverse();
                        }
                        let strings: Vec<Vec<String>> = regs
                            .iter()
                            .map(|r| r.iter().map(Symbol::name).collect())
                            .collect();
                        let (mut a, mut z) = (
                            OldGen {
                                n: 100,
                                code: vec![],
                            },
                            Gen {
                                n: 100,
                                code: vec![],
                            },
                        );
                        let (at, ad) = old::tree4(
                            &mut a,
                            code,
                            &strings,
                            into.as_ref().map(Symbol::name),
                            fused,
                        );
                        let (zt, zd) = tree4(&mut z, code, &regs, into.clone(), fused);
                        assert_eq!(ad, zd.name());
                        steps(&at, &zt);
                        events(&a, &z);
                        old::interleave(&mut a, &[at]);
                        interleave(&mut z, &[zt]);
                        events(&a, &z);
                    }
                }
                // Every source row can occupy every input slot; all output chunks are compared.
                for (rotation, zero_sign) in (0..8).flat_map(|r| ZeroSign::ALL.map(|z| (r, z))) {
                    let mut regs: Vec<Vec<Symbol>> = (0..8)
                        .map(|i| vec![Symbol(1 + 2 * i), Symbol(2 + 2 * i)])
                        .collect();
                    regs.rotate_left(rotation);
                    let strings: Vec<Vec<String>> = regs
                        .iter()
                        .map(|r| r.iter().map(Symbol::name).collect())
                        .collect();
                    let (mut a, mut z) = (
                        OldGen {
                            n: 100,
                            code: vec![],
                        },
                        Gen {
                            n: 100,
                            code: vec![],
                        },
                    );
                    let (at, ad) = old::tree(
                        &mut a,
                        code,
                        &strings,
                        fused,
                        into.as_ref().map(Symbol::name),
                        zero_sign,
                    );
                    let (zt, zd) = tree(&mut z, code, &regs, fused, into.clone(), zero_sign);
                    assert_eq!(ad, zd.name());
                    steps(&at, &zt);
                    events(&a, &z);
                    old::interleave(&mut a, &[at]);
                    interleave(&mut z, &[zt]);
                    events(&a, &z);
                    let merges = z.code.iter().any(|e| format!("{e:?}").contains("merge"));
                    assert_eq!(merges, !zero_sign.picks(code), "{code} {zero_sign:?}");
                }
            }
        }
    }
}

#[test]
fn compact_all_band_permutations() {
    for a in 1..=4 {
        for b in 1..=4 {
            for c in 1..=4 {
                for d in 1..=4 {
                    let order = [a, b, c, d];
                    if order
                        .iter()
                        .enumerate()
                        .any(|(i, x)| order[..i].contains(x))
                    {
                        continue;
                    }
                    let syms: Vec<_> = order.into_iter().map(Symbol).collect();
                    for (zero_sign, ops) in [(ZeroSign::Native, 15), (ZeroSign::Preserve, 16)] {
                        let (mut a, mut z) = (
                            OldGen {
                                n: 100,
                                code: vec![],
                            },
                            Gen {
                                n: 100,
                                code: vec![],
                            },
                        );
                        let names = syms.iter().map(Symbol::name).collect::<Vec<_>>();
                        let ad = old::compact(&mut a, &names, zero_sign);
                        let zd = compact(&mut z, &syms, zero_sign);
                        assert_eq!(ad, zd.name());
                        assert_eq!(z.code.len(), ops);
                        events(&a, &z);
                    }
                }
            }
        }
    }
}

#[test]
fn interleave_unequal_step_widths() {
    let (mut a, mut z) = (
        OldGen {
            n: 100,
            code: vec![],
        },
        Gen {
            n: 100,
            code: vec![],
        },
    );
    let (mut ats, mut zts) = (vec![], vec![]);
    // Three and four inputs have the same level count, but unequal first-level widths.
    for n in [3, 4] {
        let regs: Vec<Vec<Symbol>> = (1..=n).map(|i| vec![Symbol(i)]).collect();
        let strings = regs
            .iter()
            .map(|r| r.iter().map(Symbol::name).collect())
            .collect::<Vec<_>>();
        let (at, ad) = old::tree4(&mut a, "vmax", &strings, None, false);
        let (zt, zd) = tree4(&mut z, "vmax", &regs, None, false);
        assert_eq!(ad, zd.name());
        steps(&at, &zt);
        ats.push(at);
        zts.push(zt);
    }
    old::interleave(&mut a, &ats);
    interleave(&mut z, &zts);
    events(&a, &z);
}

fn producer(op: &str, sources: Vec<Term>, template: &str, syms: &[Symbol]) -> (OldGen, Gen) {
    let mut a = OldGen {
        n: 100,
        code: vec![],
    };
    let mut z = Gen {
        n: 100,
        code: vec![],
    };
    a.emit_owned(template, syms.iter().map(Symbol::name).collect());
    z.emit(insn(op, whole(&syms[0]), sources, None, syms));
    (a, z)
}

#[test]
fn quantiser_every_producer_and_chunk_order() {
    for table in [
        &UNARY[..],
        &BINARY[..],
        &TERNARY[..],
        &[("sel", "vsel")][..],
    ] {
        for &(op, code) in table {
            let arity = if UNARY.iter().any(|(n, _)| *n == op) {
                1
            } else if BINARY.iter().any(|(n, _)| *n == op) {
                2
            } else {
                3
            };
            // Every source position: symbolic, scalar or constant register.
            for pattern in 0..3usize.pow(arity as u32) {
                let (mut srcs, mut text, mut syms) = (vec![], vec![], vec![Symbol(1)]);
                let mut p = pattern;
                for _ in 0..arity {
                    match p % 3 {
                        0 => {
                            let s = Symbol(syms.len() as u32 + 1);
                            text.push(format!("{{{}}}", syms.len()));
                            srcs.push(whole(&s));
                            syms.push(s);
                        }
                        1 => {
                            text.push("s12".into());
                            srcs.push(Term::name("s12"));
                        }
                        _ => {
                            text.push("k2".into());
                            srcs.push(Term::name("k2"));
                        }
                    }
                    p /= 3;
                }
                let (mut a, mut z) = producer(
                    code,
                    srcs,
                    &format!("{code} {{0}}, {}", text.join(", ")),
                    &syms,
                );
                // Trailing fixed/block events and unrelated ops are ignored by reverse scan.
                a.raw("vbar");
                z.code.push(Event::Fixed(Stmt::new("vbar", vec![])));
                a.raw("{");
                z.code.push(Event::Open);
                a.raw("}");
                z.code.push(Event::Close);
                a.emit("vmov {0}, k0", &["%90"]);
                z.emit(insn(
                    "vmov",
                    whole(&Symbol(90)),
                    vec![Term::name("k0")],
                    None,
                    &[Symbol(90)],
                ));
                assert!(old::quantiser_order(&mut a, "%1", "%99"));
                assert!(quantiser_order(&mut z, &Symbol(1), &Symbol(99)));
                events(&a, &z);
                for (chunk_index, event) in z.code[..8].iter().enumerate() {
                    let Event::Op { stmt, .. } = event else {
                        panic!()
                    };
                    assert_eq!(stmt.args[0], chunk(&Symbol(99), chunk_index as u64));
                    let i = chunk_index / 2;
                    let q = chunk_index % 2;
                    for t in &stmt.args[1..arity + 1] {
                        if let Term::View(_, s) = t {
                            assert_eq!(
                                s,
                                &vec![Term::Slice(
                                    Some(Box::new(Term::int((4 * q + i) as i128))),
                                    None
                                )]
                            );
                        }
                    }
                }
            }
        }
    }
    // Duplicate and unused metadata are not reconstructed from the operand tree.
    let syms = [Symbol(1), Symbol(2), Symbol(2), Symbol(3)];
    let (mut a, mut z) = producer(
        "vadd",
        vec![whole(&Symbol(2)), whole(&Symbol(2))],
        "vadd {0}, {1}, {2}",
        &syms,
    );
    assert!(old::quantiser_order(&mut a, "%1", "%99"));
    assert!(quantiser_order(&mut z, &Symbol(1), &Symbol(99)));
    events(&a, &z);
}

#[test]
fn quantiser_slot_zero_and_reordered_sources() {
    for (srcs, template, syms) in [
        (
            vec![whole(&Symbol(1)), Term::name("k1")],
            "vadd {0}, {0}, k1",
            vec![Symbol(1)],
        ),
        (
            vec![whole(&Symbol(3)), whole(&Symbol(2))],
            "vadd {0}, {2}, {1}",
            vec![Symbol(1), Symbol(2), Symbol(3)],
        ),
        (
            vec![whole(&Symbol(3)), whole(&Symbol(3))],
            "vadd {0}, {2}, {2}",
            vec![Symbol(1), Symbol(2), Symbol(3)],
        ),
    ] {
        let (mut a, mut z) = producer("vadd", srcs, template, &syms);
        assert!(old::quantiser_order(&mut a, "%1", "%99"));
        assert!(quantiser_order(&mut z, &Symbol(1), &Symbol(99)));
        events(&a, &z);
    }
}

#[test]
fn quantiser_refusals_leave_events_untouched() {
    let s = Symbol(2);
    let cases = vec![
        (
            "vadd {0}[0:], {1}, k1",
            ir::math(
                "vadd",
                chunk(&Symbol(1), 0),
                vec![whole(&s), Term::name("k1")],
                vec![],
            ),
            vec![Symbol(1), s.clone()],
        ),
        (
            "vadd {0}, {1}[0], k1",
            ir::math(
                "vadd",
                whole(&Symbol(1)),
                vec![s.operand(Select::Chunk(0)), Term::name("k1")],
                vec![],
            ),
            vec![Symbol(1), s.clone()],
        ),
        (
            "vadd {0}, {1}[4:], k1",
            ir::math(
                "vadd",
                whole(&Symbol(1)),
                vec![chunk(&s, 4), Term::name("k1")],
                vec![],
            ),
            vec![Symbol(1), s.clone()],
        ),
        (
            "vadd {0}, {1}, k1 vl=16",
            ir::math(
                "vadd",
                whole(&Symbol(1)),
                vec![whole(&s), Term::name("k1")],
                vec![ir::kw("vl", Term::int(16))],
            ),
            vec![Symbol(1), s.clone()],
        ),
        (
            "vmov {0}, {1} if=p1",
            ir::math(
                "vmov",
                whole(&Symbol(1)),
                vec![whole(&s)],
                vec![ir::kw("if", Term::name("p1"))],
            ),
            vec![Symbol(1), s.clone()],
        ),
        (
            "vadd {0}, {1}, k1",
            ir::math(
                "vadd",
                whole(&Symbol(1)),
                vec![whole(&Symbol(1)), Term::name("k1")],
                vec![],
            ),
            vec![Symbol(1), Symbol(1)],
        ),
        (
            "vcmplt p1, {1}, k1",
            ir::math(
                "vcmplt",
                Term::name("p1"),
                vec![whole(&s), Term::name("k1")],
                vec![],
            ),
            vec![Symbol(1), s.clone()],
        ),
        (
            "vcmpeq {0}, {1}, k1",
            ir::math(
                "vcmpeq",
                whole(&Symbol(1)),
                vec![whole(&s), Term::name("k1")],
                vec![],
            ),
            vec![Symbol(1), s.clone()],
        ),
        (
            "vmov p1, {1}",
            ir::math("vmov", Term::name("p1"), vec![whole(&s)], vec![]),
            vec![Symbol(1), s.clone()],
        ),
    ];
    for (tmpl, stmt, syms) in cases {
        refuse(tmpl, stmt, syms);
    }
    for (t, text) in [
        (Term::name("v7"), "v7"),
        (Term::name("p1"), "p1"),
        (Term::name("s"), "s"),
        (Term::name("k_bad"), "k_bad"),
        (Term::int(2), "2"),
        (ir::crossbar("xor", whole(&s), 8), "xor({1}, 8)"),
        (
            ir::bcast(s.operand(Select::Chunk(0)), 0, 3),
            "bcast({1}[0], lane=0, sh=3)",
        ),
    ] {
        refuse(
            &format!("vmov {{0}}, {text}"),
            ir::math("vmov", whole(&Symbol(1)), vec![t], vec![]),
            vec![Symbol(1), s.clone()],
        );
    }
    // No producer, or a later use before the producer, cannot be rewritten.
    for used in [false, true] {
        let (mut a, mut z) = producer("vmov", vec![Term::name("k0")], "vmov {0}, k0", &[Symbol(1)]);
        if used {
            a.emit("vmov {0}, {1}", &["%3", "%1"]);
            z.emit(insn(
                "vmov",
                whole(&Symbol(3)),
                vec![whole(&Symbol(1))],
                None,
                &[Symbol(3), Symbol(1)],
            ));
        }
        let sym = if used { Symbol(1) } else { Symbol(77) };
        let ac = a.code.clone();
        let zc = z.clone();
        assert!(!old::quantiser_order(&mut a, &sym.name(), "%99"));
        assert!(!quantiser_order(&mut z, &sym, &Symbol(99)));
        assert_eq!(a.code, ac);
        assert_eq!(z, zc);
        events(&a, &z);
    }
}

fn refuse(tmpl: &str, stmt: Stmt, syms: Vec<Symbol>) {
    let mut a = OldGen {
        n: 100,
        code: vec![Code::Op(
            tmpl.into(),
            syms.iter().map(Symbol::name).collect(),
        )],
    };
    let mut z = Gen {
        n: 100,
        code: vec![Event::Op { stmt, syms }],
    };
    let ac = a.code.clone();
    let zc = z.clone();
    assert!(!old::quantiser_order(&mut a, "%1", "%99"), "{tmpl}");
    assert!(!quantiser_order(&mut z, &Symbol(1), &Symbol(99)), "{tmpl}");
    assert_eq!(a.code, ac);
    assert_eq!(z, zc);
    events(&a, &z);
}

#[cfg(feature = "mutants")]
#[test]
fn disabled_rewrite_and_tree_options() {
    use crate::mutant::{scoped, State};
    scoped(
        State {
            active: Some("l2.vector/no-quantiser-order"),
            ..State::default()
        },
        || {
            let (mut a, mut z) =
                producer("vmov", vec![Term::name("k0")], "vmov {0}, k0", &[Symbol(1)]);
            let ac = a.code.clone();
            let zc = z.clone();
            assert!(!old::quantiser_order(&mut a, "%1", "%99"));
            assert!(!quantiser_order(&mut z, &Symbol(1), &Symbol(99)));
            assert_eq!(a.code, ac);
            assert_eq!(z, zc);
            events(&a, &z);
        },
    );
    scoped(
        State {
            active: Some("l2.vector/band-no-broadcast"),
            ..State::default()
        },
        || {
            let (mut a, mut z) = (
                OldGen {
                    n: 100,
                    code: vec![],
                },
                Gen {
                    n: 100,
                    code: vec![],
                },
            );
            let (at, ad) = old::tree4(&mut a, "vmax", &[vec!["%1".into()]], None, false);
            let (zt, zd) = tree4(&mut z, "vmax", &[vec![Symbol(1)]], None, false);
            assert_eq!(ad, zd.name());
            assert_eq!(zt.len(), 5);
            steps(&at, &zt);
            events(&a, &z);
        },
    );
    scoped(
        State {
            active: Some("l2.vector/compact-reversed"),
            ..State::default()
        },
        compact_all_band_permutations,
    );
}

#[test]
fn quantiser_last_definition_and_plain_predicate() {
    for (text, t, expected) in [
        ("{0}", whole(&Symbol(1)), true),
        ("s123", Term::name("s123"), true),
        ("k0", Term::name("k0"), true),
        ("s", Term::name("s"), false),
        ("v1", Term::name("v1"), false),
        ("{0}[0:]", chunk(&Symbol(1), 0), false),
    ] {
        assert_eq!(old::plain(text), expected);
        assert_eq!(plain(&t, &[Symbol(1)]), expected);
    }
    let (mut a, mut z) = producer("vmov", vec![Term::name("k0")], "vmov {0}, k0", &[Symbol(1)]);
    a.emit("vmov {0}, {1}", &["%3", "%1"]);
    z.emit(insn(
        "vmov",
        whole(&Symbol(3)),
        vec![whole(&Symbol(1))],
        None,
        &[Symbol(3), Symbol(1)],
    ));
    a.emit("vmov {0}, k1", &["%1"]);
    z.emit(insn(
        "vmov",
        whole(&Symbol(1)),
        vec![Term::name("k1")],
        None,
        &[Symbol(1)],
    ));
    assert!(old::quantiser_order(&mut a, "%1", "%99"));
    assert!(quantiser_order(&mut z, &Symbol(1), &Symbol(99)));
    events(&a, &z);
}
