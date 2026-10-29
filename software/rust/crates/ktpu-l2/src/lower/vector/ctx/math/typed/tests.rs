//! Math differential fixtures: events, operand slots, bytes and state.

use super::*;
use crate::lower::vector::gen::{print, typed::Event};
use crate::lower::vector::{pat, Risk};
use crate::mutant::{scoped, State};
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
    run_with(o, per_band, inplace, present, &[(3.5, "s0")])
}

/// `run` with the scalar file holding `scalars`.
fn run_with(
    o: &Op,
    per_band: i64,
    inplace: bool,
    present: bool,
    scalars: &[(f64, &str)],
) -> (Gen, Emitter, String) {
    run_at(o, per_band, inplace, present, scalars, Risk::ALL)
}

/// `run_with`, the op's EXP2 / EXP2D argument at `risk`.
fn run_at(
    o: &Op,
    per_band: i64,
    inplace: bool,
    present: bool,
    scalars: &[(f64, &str)],
    risk: Risk,
) -> (Gen, Emitter, String) {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    for c in [&mut a, &mut z] {
        c.guards.insert(o.uid, risk);
        c.per_band = per_band;
        c.tile = 16;
        c.inplace = inplace;
        c.held.insert(FKey::of(9.25), "%81".into());
        c.pinned.insert("%81".into());
        for &(f, s) in scalars {
            c.scalars.insert(FKey::of(f), s.into());
        }
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
                    let first = text.lines().find(|l| !l.starts_with("seti ")).unwrap();
                    let guard = match op {
                        "exp2" => "vmax ",
                        "exp2d" => "vabs ",
                        _ => code,
                    };
                    assert!(first.starts_with(guard), "{op}: {first}");
                    assert!(text.lines().any(|l| l.starts_with(&format!("{code} "))));
                    assert_eq!(text.contains("vl=16"), stat && per == 0);
                }
            }
        }
    }
}

/// The ops of `z`, each with the value of every register a `seti` set before it.
fn ops_and_settings(z: &Emitter) -> Vec<(Stmt, Vec<Symbol>, HashMap<String, f64>)> {
    let mut set: HashMap<String, f64> = HashMap::new();
    let mut out = Vec::new();
    for e in &z.code {
        match e {
            Event::Fixed(st) if st.op == "seti" => {
                let Term::Assign(reg, value) = &st.args[0] else {
                    panic!("{st:?}")
                };
                let value = match value.as_ref() {
                    Term::Neg(f) => -number(f).unwrap(),
                    f => number(f).unwrap(),
                };
                set.insert(reg.as_name().unwrap().to_string(), value);
            }
            Event::Op { stmt, syms } => out.push((stmt.clone(), syms.clone(), set.clone())),
            _ => {}
        }
    }
    out
}

/// Each guard form against the unguarded lowering
/// (`l2.vector/exp2-wrapped-domain`): opcode order and every operand,
/// predicate and constant it reads, at each risk range analysis leaves.
#[test]
fn exp2_domain_guard_order_and_wiring() {
    let a = || Term::name("a");
    let low = Risk {
        high: false,
        low: true,
    };
    let high = Risk {
        high: true,
        low: false,
    };
    for (op, risk, guard) in [
        (
            "exp2",
            Risk::ALL,
            ["vmax", "vcmplt", "vadd", "vexp2"].as_slice(),
        ),
        ("exp2", high, ["vmax", "vcmplt", "vadd", "vexp2"].as_slice()),
        ("exp2", low, ["vmax", "vexp2"].as_slice()),
        ("exp2", Risk::NONE, ["vexp2"].as_slice()),
        (
            "exp2d",
            Risk::ALL,
            ["vabs", "vcmplt", "vadd", "vmov", "vexp2d"].as_slice(),
        ),
        (
            "exp2d",
            high,
            ["vcmplt", "vadd", "vmov", "vexp2d"].as_slice(),
        ),
        (
            "exp2d",
            low,
            ["vcmpgt", "vadd", "vmov", "vexp2d"].as_slice(),
        ),
        ("exp2d", Risk::NONE, ["vexp2d"].as_slice()),
    ] {
        let args = if op == "exp2" {
            vec![a()]
        } else {
            vec![a(), Term::name("b")]
        };
        for (stat, per) in [(false, 0), (false, 2), (true, 0), (true, 2)] {
            for (inplace, present) in [(false, false), (false, true), (true, true)] {
                for scalars in [
                    vec![(3.5, "s0")],
                    vec![
                        (3.5, "s0"),
                        (EXP2_RANGE, "s1"),
                        (EXP2_FLOOR, "s2"),
                        (EXP2D_LOW, "s3"),
                    ],
                ] {
                    let o = op_for(op, args.clone(), stat);
                    let (_, z, _) = run_at(&o, per, inplace, present, &scalars, risk);
                    let plain = scoped(
                        State {
                            active: Some(crate::lower::vector::DOMAIN_MUTANT),
                            ..State::default()
                        },
                        || run_at(&o, per, inplace, present, &scalars, risk).1,
                    );
                    let value = |t: &Term, set: &HashMap<String, f64>| {
                        let reg = t.as_name().unwrap();
                        scalars
                            .iter()
                            .find(|(_, s)| *s == reg)
                            .map(|(f, _)| *f)
                            .unwrap_or_else(|| set[reg])
                    };
                    let stride = stat && per == 0;
                    let sel = if stride {
                        Select::Stride(0)
                    } else {
                        Select::Whole
                    };
                    let vl = stride.then(|| Term::int(16));

                    let unguarded = ops_and_settings(&plain);
                    let guarded = ops_and_settings(&z);
                    assert_eq!(guarded.len(), unguarded.len() * guard.len());
                    for (want, ops) in unguarded.iter().zip(guarded.chunks(guard.len())) {
                        let names: Vec<&str> =
                            ops.iter().map(|(st, _, _)| st.op.as_str()).collect();
                        assert_eq!(names, guard, "{op} at {risk:?}");
                        let (math, math_syms, _) = ops.last().unwrap();
                        if guard.len() == 1 {
                            assert_eq!((&want.0, &want.1), (math, math_syms));
                            continue;
                        }
                        assert_eq!(want.0.op, math.op);
                        let src = &want.0.args[1];
                        let t = &math_syms[1];
                        let x = t.operand(sel);
                        assert!(t.0 > 81 && math_syms[0] != *t, "a fresh temporary");
                        let mut rewired = want.0.clone();
                        rewired.args[0] = math.args[0].clone();
                        rewired.args[1] = x.clone();
                        assert_eq!(&rewired, math);
                        if inplace {
                            assert_eq!(want.0.args[0], math.args[0]);
                        }
                        for (st, syms, _) in &ops[..ops.len() - 1] {
                            let cmp = st.op.starts_with("vcmp");
                            assert_eq!(st.args[0], if cmp { Term::name("p1") } else { x.clone() });
                            assert_eq!(syms[0], *t);
                            assert_eq!(st.kw("vl"), vl.as_ref());
                        }
                        let src_syms = &want.1[1..2];
                        let at = |name: &str| ops.iter().find(|(st, _, _)| st.op == name);
                        if let Some((max, s0, set0)) = at("vmax") {
                            assert_eq!(max.positional()[1], src);
                            assert_eq!(value(max.positional()[2], set0), EXP2_FLOOR);
                            assert_eq!(s0[1..], *src_syms);
                        }
                        if let Some((abs, s0, _)) = at("vabs") {
                            assert_eq!(abs.positional()[1..], [src]);
                            assert_eq!(s0[1..], *src_syms);
                        }
                        let Some((cmp, s1, set1)) = at("vcmplt").or(at("vcmpgt")) else {
                            assert_eq!(names, ["vmax", "vexp2"]);
                            continue;
                        };
                        assert!(cmp.kw("if").is_none() && cmp.kw("unless").is_none());
                        let bound = value(cmp.positional()[2], set1);
                        if names[0] == "vcmplt" || names[0] == "vcmpgt" {
                            // Only one side at risk: the compare reads `a` itself.
                            assert_eq!(cmp.positional()[1], src);
                            assert_eq!(s1[1..], *src_syms);
                            let want_bound = if risk.high { EXP2_RANGE } else { EXP2D_LOW };
                            assert_eq!(bound, want_bound);
                            assert_eq!(cmp.op, if risk.high { "vcmplt" } else { "vcmpgt" });
                        } else {
                            assert_eq!(cmp.positional()[1], &x);
                            assert_eq!(s1.len(), 1);
                            assert_eq!(bound, EXP2_RANGE);
                        }
                        if op == "exp2" {
                            let (add, s2, _) = at("vadd").unwrap();
                            assert_eq!(add.positional()[1..], [&x, &x]);
                            assert_eq!(add.kw("unless"), Some(&Term::name("p1")));
                            assert!(add.kw("if").is_none());
                            assert_eq!(s2, &vec![t.clone()]);
                        } else {
                            let (add, s2, _) = at("vadd").unwrap();
                            let (mov, s3, _) = at("vmov").unwrap();
                            assert_eq!(add.positional()[1..], [src, src]);
                            assert!(add.kw("if").is_none() && add.kw("unless").is_none());
                            assert_eq!(mov.positional()[1], src);
                            assert_eq!(mov.kw("if"), Some(&Term::name("p1")));
                            assert!(mov.kw("unless").is_none());
                            assert_eq!(s2[1..], *src_syms);
                            assert_eq!(s3[1..], *src_syms);
                        }
                    }
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
