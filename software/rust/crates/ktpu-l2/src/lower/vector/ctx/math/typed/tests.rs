//! Math differential fixtures: events, operand slots, bytes and state.

use super::*;
use crate::lower::vector::gen::{print, typed::Event};
use crate::lower::vector::pat;
use ktpu_machine::Machine;

fn body() -> Body {
    Body {
        name: "k".into(),
        params: vec![],
        scratch: IndexMap::new(),
        stmts: vec![],
        reader: Default::default(),
        macros: IndexMap::new(),
        buffers: IndexMap::new(),
        vregs: 32,
        vcopies: 4,
        fill: (0, 1),
    }
}

fn op_for(name: &str, args: Vec<Term>, stat: bool) -> Op {
    Op {
        uid: 0,
        st: Stmt::new(name, args.clone()),
        op: name.into(),
        target: Some("out".into()),
        args,
        ty: Some(Type {
            dtype: "f32".into(),
            shape: if stat { vec![16] } else { vec![16, 32] },
            space: "reg".into(),
            mode: Some("f16".into()),
        }),
    }
}

fn phys(n: &str) -> String {
    format!("v{}", n.strip_prefix('%').unwrap())
}

fn resolve(t: &mut Term) {
    match t {
        Term::Name(n) if n.starts_with('%') => *n = phys(n),
        Term::View(n, args) | Term::Call(n, args) => {
            if n.starts_with('%') {
                *n = phys(n);
            }
            for a in args {
                resolve(a);
            }
        }
        Term::Assign(a, b) | Term::AddAssign(a, b) => {
            resolve(a);
            resolve(b);
        }
        Term::Neg(a) | Term::Kw(_, a) => resolve(a),
        Term::Tuple(args) => {
            for a in args {
                resolve(a);
            }
        }
        Term::Slice(a, b) => {
            for t in a.iter_mut().chain(b.iter_mut()) {
                resolve(t);
            }
        }
        _ => (),
    }
}

fn paired(a: &Gen, z: &Emitter) -> String {
    assert_eq!(a.n, z.n as usize);
    assert_eq!(a.code.len(), z.code.len());
    let mut out = String::new();
    for (old, new) in a.code.iter().zip(&z.code) {
        let (text, st) = match (old, new) {
            (Code::Raw(text), Event::Fixed(st)) => (text.clone(), st),
            (Code::Op(text, syms), Event::Op { stmt, syms: typed }) => {
                assert_eq!(syms, &typed.iter().map(Symbol::name).collect::<Vec<_>>());
                (
                    pat::format(text, &syms.iter().map(|s| phys(s)).collect::<Vec<_>>()),
                    stmt,
                )
            }
            _ => panic!("different event kinds: {old:?} {new:?}"),
        };
        let mut st = st.clone();
        for t in &mut st.args {
            resolve(t);
        }
        let printed = print::emit(std::slice::from_mut(&mut st)).unwrap();
        assert_eq!(printed.as_bytes(), format!("{text}\n").as_bytes());
        assert_eq!(
            vec![st],
            ktpu_l1::text::parse(&format!("{text}\n"), "<template>").unwrap()
        );
        out.push_str(&printed);
    }
    out
}

fn state(a: &Ctx, z: &Ctx, al: &IndexMap<String, Val>, zl: &IndexMap<String, Val>) {
    assert_eq!(a.scalars, z.scalars);
    assert_eq!(a.held, z.held);
    assert_eq!(a.pinned, z.pinned);
    assert_eq!(a.staged, z.staged);
    assert_eq!(format!("{:?}", a.vals), format!("{:?}", z.vals));
    assert_eq!(format!("{al:?}"), format!("{zl:?}"));
}

fn locals(stat: bool) -> IndexMap<String, Val> {
    ["a", "b", "c", "out", "stats"]
        .into_iter()
        .enumerate()
        .map(|(j, name)| {
            let mut v = Val::new(
                if stat || name == "stats" {
                    "stat"
                } else {
                    "row"
                },
                16,
            );
            v.cols = 32;
            for r in 0..16 {
                v.regs.insert(r, format!("%{}", j * 16 + r as usize + 1));
            }
            (name.into(), v)
        })
        .collect()
}

fn run(o: &Op, per_band: i64, inplace: bool, present: bool) -> (Gen, Emitter, String) {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    for c in [&mut a, &mut z] {
        c.per_band = per_band;
        c.tile = 16;
        c.inplace = inplace;
        c.held.insert(FKey::of(9.25), "%81".into());
        c.pinned.insert("%81".into());
        c.scalars.insert(FKey::of(3.5), "s0".into());
    }
    let mut al = locals(o.ty.as_ref().unwrap().shape.len() == 1);
    if !present {
        al.shift_remove("out");
    }
    let mut zl = al.clone();
    let mut ag = Gen {
        n: 81,
        code: vec![],
    };
    let mut zg = Emitter {
        n: 81,
        code: vec![],
    };
    // Repeated and unsorted rows exercise once/group deduplication.
    let rows = [9, 0, 1, 9, 8, 15];
    let ar = a.math(&mut ag, o, &rows, &mut al);
    let zr = z.math_typed(&mut zg, o, &rows, &mut zl);
    assert_eq!(format!("{ar:?}"), format!("{zr:?}"));
    assert!(ar.is_ok(), "unexpected refusal: {ar:?}");
    state(&a, &z, &al, &zl);
    let text = paired(&ag, &zg);
    (ag, zg, text)
}

#[test]
fn every_opcode_rows_statistics_and_inplace() {
    for table in [
        &UNARY[..],
        &BINARY[..],
        &TERNARY[..],
        &CMP[..],
        &[("sel", "vsel")][..],
    ] {
        for &(op, code) in table {
            let arity = if is_in(&UNARY, op) {
                1
            } else if is_in(&BINARY, op) || is_in(&CMP, op) {
                2
            } else {
                3
            };
            for (stat, per) in [(false, 0), (false, 2), (true, 0), (true, 2)] {
                if stat && per == 0 && (is_in(&CMP, op) || op == "sel") {
                    continue;
                }
                for (inplace, present) in [(false, false), (false, true), (true, true)] {
                    let args = ["a", "b", "c"]
                        .iter()
                        .take(arity)
                        .map(|s| Term::name(*s))
                        .collect();
                    let (_, _, text) = run(&op_for(op, args, stat), per, inplace, present);
                    assert!(text.starts_with(code));
                    assert_eq!(text.contains("vl=16"), stat && per == 0);
                }
            }
        }
    }
}

#[test]
fn compare_symbols_and_predicate_sequence() {
    for (op, _) in CMP {
        for stat in [false, true] {
            let (a, z, _) = run(
                &op_for(op, vec![Term::name("a"), Term::name("a")], stat),
                2,
                false,
                false,
            );
            for chunk in z.code.chunks_exact(3) {
                let Event::Op { stmt, syms } = &chunk[0] else {
                    panic!()
                };
                assert_eq!(stmt.args[0], Term::name("p1"));
                assert_eq!(syms.len(), 3);
                assert_eq!(syms[1], syms[2]);
                assert!(!stmt.args.contains(&syms[0].operand(Select::Whole)));
                let Event::Op {
                    stmt: zero,
                    syms: zs,
                } = &chunk[1]
                else {
                    panic!()
                };
                let Event::Op {
                    stmt: one,
                    syms: os,
                } = &chunk[2]
                else {
                    panic!()
                };
                assert_eq!(zs, &syms[..1]);
                assert_eq!(os, zs);
                assert_eq!(zero.args[1], Term::name("k0"));
                assert_eq!(one.args[1], Term::name("k1"));
                assert_eq!(one.args[2], ir::kw("if", Term::name("p1")));
            }
            assert_eq!(a.code.len() % 3, 0);
        }
    }
}

#[test]
fn constant_operands_held_scalar_k_and_staged() {
    for (stat, per) in [(false, 0), (true, 0), (true, 2)] {
        for args in [
            vec![Term::Float(9.25), Term::Float(3.5), Term::Float(-1.0)],
            vec![Term::Float(0.0), Term::Float(1.0), Term::Float(2.75)],
            vec![Term::Float(2.75), Term::Float(2.75), Term::name("a")],
        ] {
            let (_, _, text) = run(&op_for("fma", args, stat), per, false, false);
            assert!(text.contains("vfma"));
        }
    }
}

#[test]
fn broadcast_operand_slots_and_temporaries() {
    let broadcast = || Term::View("stats".into(), vec![Term::NewAxis]);
    for op in ["add", "fma", "sel", "cmplt", "mov"] {
        let arity = if op == "mov" {
            1
        } else if op == "add" || op == "cmplt" {
            2
        } else {
            3
        };
        for slot in 0..arity {
            for per in [0, 2] {
                let mut args = vec![Term::name("a"); arity];
                args[slot] = broadcast();
                let (_, _, text) = run(&op_for(op, args, false), per, false, false);
                assert_eq!(text.contains("bcast("), per == 0);
                if per == 0 {
                    assert!(text.contains("lane=1, sh=3"));
                }
            }
        }
    }
    // On-B bcast uses source slot two, not the template's original slot one.
    let (_, _, text) = run(
        &op_for("add", vec![Term::name("a"), broadcast()], false),
        0,
        false,
        false,
    );
    assert!(text
        .lines()
        .all(|l| l.starts_with("vadd") && l.contains("bcast(v")));
    let (_, _, text) = run(
        &op_for("fma", vec![broadcast(), broadcast(), broadcast()], false),
        0,
        false,
        false,
    );
    assert!(text.contains("vmul") && text.contains("vfma"));
}

#[test]
fn xlane_xrot_and_merge_operands() {
    for (op, mode) in XLANE {
        for k in [1, 8, 15] {
            for inplace in [false, true] {
                let (_, _, text) = run(
                    &op_for(op, vec![Term::name("a"), Term::int(k)], false),
                    0,
                    inplace,
                    inplace,
                );
                assert!(text.contains(&format!("{mode}(v")));
            }
        }
    }
    // merge belongs to tree.rs, not math/xlane; witness its constructor only.
    for (select, k, vl, template) in [
        (Select::Whole, 8, None, "vadd {0}, {1}, merge({1}, 8)"),
        (
            Select::Stride(4),
            4,
            Some(64),
            "vadd {0}, {0}, merge({0}[4:], 4) vl=64",
        ),
        (
            Select::Stride(2),
            2,
            Some(32),
            "vadd {0}, {0}, merge({0}[2:], 2) vl=32",
        ),
    ] {
        let mut a = Gen { n: 2, code: vec![] };
        let mut z = Emitter { n: 2, code: vec![] };
        let (source, syms) = if k == 8 {
            a.emit(template, &["%1", "%2"]);
            (Symbol(2), vec![Symbol(1), Symbol(2)])
        } else {
            a.emit(template, &["%1"]);
            (Symbol(1), vec![Symbol(1)])
        };
        z.op(
            ir::math(
                "vadd",
                Symbol(1).operand(Select::Whole),
                vec![
                    source.operand(Select::Whole),
                    ir::crossbar("merge", source.operand(select), k),
                ],
                vl.map(|v| vec![ir::kw("vl", Term::int(v))])
                    .unwrap_or_default(),
            ),
            syms,
        );
        paired(&a, &z);
    }
}

#[test]
fn pressure_and_refusals_keep_partial_state() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    let mut cases = vec![
        op_for(
            "fma",
            vec![Term::Float(2.25), Term::Float(3.25), Term::Float(4.25)],
            false,
        ),
        op_for(
            "fma",
            vec![Term::Float(2.25), Term::Float(3.25), Term::Float(4.25)],
            true,
        ),
        op_for("sel", vec![Term::name("a"); 3], true),
        op_for("cmplt", vec![Term::name("a"); 2], true),
        op_for("unknown", vec![], false),
        op_for("add", vec![], false),
        op_for("add", vec![Term::name("missing"); 2], false),
        op_for("xlane", vec![Term::name("a"), Term::int(0)], false),
        op_for("xrot", vec![Term::name("a"), Term::int(8)], true),
    ];
    let mut invalid = op_for("add", vec![], false);
    invalid.ty = None;
    cases.push(invalid);
    for (i, o) in cases.iter().enumerate() {
        let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
        let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
        let mut al = locals(o.ty.as_ref().is_some_and(|t| t.shape.len() == 1));
        al.shift_remove("out");
        let mut zl = al.clone();
        let mut ag = Gen {
            n: 80,
            code: vec![],
        };
        let mut zg = Emitter {
            n: 80,
            code: vec![],
        };
        let ar = a.math(&mut ag, o, &[0], &mut al).unwrap_err();
        let zr = z.math_typed(&mut zg, o, &[0], &mut zl).unwrap_err();
        if i < 2 {
            assert!(matches!(ar, VErr::Pressure));
            assert_eq!(ag.code.len(), 2);
        }
        assert_eq!(format!("{ar:?}"), format!("{zr:?}"));
        paired(&ag, &zg);
        state(&a, &z, &al, &zl);
    }
}
