//! Differential checks of the independently emitted constant/prologue statements.

use super::*;
use crate::lower::links::Transfer;
use crate::lower::vector::{fmt_float, gen::print, pat};
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

fn node(op: &str, args: Vec<Term>, shape: Vec<i64>, dtype: &str, mode: &str) -> Node {
    Node::Op(Op {
        uid: 0,
        st: Stmt::new(op, args.clone()),
        op: op.into(),
        target: Some("out".into()),
        args,
        ty: Some(Type {
            dtype: dtype.into(),
            shape,
            space: "l1".into(),
            mode: Some(mode.into()),
        }),
    })
}

fn physical(name: &str) -> String {
    format!("v{}", name.strip_prefix('%').unwrap())
}

// Test-only physical assignment, limited to the operands emitted by this step.
fn resolve(t: &mut Term) {
    match t {
        Term::Name(n) | Term::View(n, _) if n.starts_with('%') => *n = physical(n),
        _ => (),
    }
}

fn print_stmt(mut st: Stmt) -> String {
    for a in &mut st.args {
        resolve(a);
    }
    let text = print::emit(std::slice::from_mut(&mut st)).unwrap();
    let parsed = ktpu_l1::text::parse(&text, "<typed>").unwrap();
    if text.contains("nan") {
        // NaN does not compare equal to itself; compare its parsed node shape instead.
        assert_eq!(format!("{:?}", vec![st]), format!("{parsed:?}"));
    } else {
        assert_eq!(vec![st], parsed);
    }
    text
}

fn paired(old: &Gen, new: &Emitter) {
    assert_eq!(old.n, new.n as usize);
    assert_eq!(old.code.len(), new.code.len());
    for (a, b) in old.code.iter().zip(&new.code) {
        match (a, b) {
            (Code::Raw(a), Event::Fixed(b)) => assert_eq!(format!("{a}\n"), print_stmt(b.clone())),
            (Code::Op(a, syms), Event::Op { stmt, syms: typed }) => {
                assert_eq!(syms, &typed.iter().map(Symbol::name).collect::<Vec<_>>());
                let names = syms.iter().map(|s| physical(s)).collect::<Vec<_>>();
                assert_eq!(
                    format!("{}\n", pat::format(a, &names)),
                    print_stmt(stmt.clone())
                );
            }
            (Code::Raw(a), Event::Open) => assert_eq!(a, "{"),
            (Code::Raw(a), Event::Close) => assert_eq!(a, "}"),
            _ => panic!("different event kinds: {a:?}, {b:?}"),
        }
    }
}

fn state(old: &Ctx, new: &Ctx) {
    assert_eq!(old.scalars, new.scalars);
    assert_eq!(old.held, new.held);
    assert_eq!(old.pinned, new.pinned);
    assert_eq!(old.staged, new.staged);
    // Val has no PartialEq; compare every field through its derived Debug.
    assert_eq!(format!("{:?}", old.vals), format!("{:?}", new.vals));
}

#[test]
fn seti_float_spellings_and_integers() {
    for f in [
        0.0,
        -0.0,
        1.0,
        -1.0,
        2.5,
        -2.5,
        1e-4,
        1e-5,
        1e15,
        1e16,
        -1e100,
        1e300,
        f64::MAX,
        f64::MIN_POSITIVE,
        f64::from_bits(1),
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NAN,
    ] {
        assert_eq!(
            print_stmt(constant_seti("s0", f)),
            format!("seti s0 = {}\n", fmt_float(f))
        );
    }
    for value in [0, 1, -1, i64::MIN, i64::MAX] {
        assert_eq!(
            print_stmt(ir::seti("s0", Term::dec(value.into()))),
            format!("seti s0 = {value}\n")
        );
    }
}

#[test]
fn ranked_constants_held_and_scalar_operands() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for hold in [false, true] {
        for overflow in [false, true] {
            let mut old = Ctx::new(&b, &t, &Env::new(), None).unwrap();
            let mut new = Ctx::new(&b, &t, &Env::new(), None).unwrap();
            old.hold = hold;
            new.hold = hold;
            let mut ops = vec![];
            let count = if overflow { t.stk_s() + 4 } else { 3 };
            for x in 0..count {
                ops.push(Arc::new(node(
                    "add",
                    vec![
                        Term::Int((x + 2).into(), ktpu_l1::text::IntStyle::Dec),
                        Term::Float(3.25),
                    ],
                    vec![4, 32],
                    "f16",
                    "f16",
                )));
            }
            ops.push(Arc::new(node(
                "add",
                vec![
                    Term::Float(-0.0),
                    Term::Float(1.0),
                    Term::Float(-1.0),
                    Term::Float(-2.5),
                    Term::Float(1e100),
                ],
                vec![4, 32],
                "f16",
                "f16",
            )));
            ops.push(Arc::new(node(
                "xrot",
                vec![Term::Float(999.0)],
                vec![4, 32],
                "f16",
                "f16",
            )));
            let ns = numbers(&ops);
            assert_eq!(ns[0], 3.25);
            assert!(!ns.contains(&999.0));
            assert!(!ns.contains(&0.0));
            let (mut a, mut z) = (Gen::default(), Emitter::default());
            old.constants(&mut a, &ops);
            new.constants_typed(&mut z, &ops);
            paired(&a, &z);
            state(&old, &new);
            for f in ns.into_iter().chain([0.0, -0.0, 1.0, -1.0]) {
                for (sel, suffix) in [
                    (Select::Whole, ""),
                    (Select::Chunk(3), "[3]"),
                    (Select::Stride(0), "[0:]"),
                    (Select::Stride(4), "[4:]"),
                ] {
                    let mut syms = vec![];
                    let mut typed_syms = vec![];
                    let operand = old.constant(&mut a, f, &mut syms, &[], suffix).unwrap();
                    let typed_operand = new
                        .constant_typed(&mut z, f, &mut typed_syms, &[], sel)
                        .unwrap();
                    assert_eq!(
                        syms,
                        typed_syms.iter().map(Symbol::name).collect::<Vec<_>>()
                    );
                    // Destination occupies placeholder zero; held operands start at one.
                    let dest = a.sym();
                    let td = z.sym();
                    let mut args = vec![dest];
                    args.extend(syms);
                    let mut ts = vec![td.clone()];
                    ts.extend(typed_syms);
                    a.emit_owned(format!("vmov {{0}}, {operand}"), args);
                    z.op(
                        ir::math(
                            "vmov",
                            td.operand(Select::Whole),
                            vec![typed_operand],
                            vec![],
                        ),
                        ts,
                    );
                    paired(&a, &z);
                    state(&old, &new);
                }
            }
        }
    }
}

#[test]
fn staging_reuse_pressure_and_block_edges() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    let mut old = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    let mut new = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    let (mut a, mut z) = (Gen::default(), Emitter::default());
    let stage = stage_regs(&t);
    for step in 0..5 {
        if step == 2 {
            a.raw("{");
            z.code.push(Event::Open);
        }
        if step == 3 {
            a.raw("}");
            z.code.push(Event::Close);
        }
        let before = a.code.len();
        let x = old.constant(&mut a, 42.5, &mut vec![], &[], "").unwrap();
        let y = new
            .constant_typed(&mut z, 42.5, &mut vec![], &[], Select::Whole)
            .unwrap();
        assert_eq!(y, Term::name(&x));
        assert_eq!(
            a.code.len() - before,
            usize::from(matches!(step, 0 | 2 | 3))
        );
        paired(&a, &z);
        state(&old, &new);
    }
    let x = old
        .constant(&mut a, 43.5, &mut vec![], &stage[..1], "")
        .unwrap();
    let y = new
        .constant_typed(
            &mut z,
            43.5,
            &mut vec![],
            &[Term::name(&stage[0])],
            Select::Whole,
        )
        .unwrap();
    assert_eq!(x, stage[1]);
    assert_eq!(y, Term::name(&stage[1]));
    assert!(matches!(
        old.constant(&mut a, 44.5, &mut vec![], &stage, ""),
        Err(VErr::Pressure)
    ));
    assert!(matches!(
        new.constant_typed(
            &mut z,
            44.5,
            &mut vec![],
            &stage.iter().map(Term::name).collect::<Vec<_>>(),
            Select::Whole
        ),
        Err(VErr::Pressure)
    ));
    paired(&a, &z);
    state(&old, &new);
}

#[test]
fn prologue_load_recv_pinned_unpacks() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for pin in [false, true] {
        for (dtype, mode, rows_w, bcast) in [
            ("f16", "f16", 0, false),
            ("f16", "gt4", 0, false),
            ("f32", "f16", 0, false),
            ("f16", "f16", 4, false),
            ("f16", "f16", 4, true),
        ] {
            for recv in [false, true] {
                let mut old = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                let mut new = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                for c in [&mut old, &mut new] {
                    c.pin = pin;
                    c.descs.push(Desc {
                        ad: 3,
                        buf: vec![48],
                        rows_w,
                        bcast,
                        ..Desc::default()
                    });
                    c.of.insert(0, 0);
                    c.plan.insert(
                        0,
                        PlanX::T(Transfer {
                            src: "mg[0]".into(),
                            dst: "vc[0]".into(),
                            side: "l1".into(),
                            at: 96,
                            size: 64,
                            shape: vec![16, 32],
                            flow: false,
                            slots: vec![],
                            per: 0,
                        }),
                    );
                }
                let (mut a, mut z) = (Gen::default(), Emitter::default());
                let n = node(
                    if recv { "recv" } else { "load" },
                    vec![],
                    vec![16, 32],
                    dtype,
                    mode,
                );
                old.prologue(&mut a, &n).unwrap();
                new.prologue_typed(&mut z, &n).unwrap();
                paired(&a, &z);
                state(&old, &new);
                let n = node("unpack", vec![Term::name("out")], vec![16, 32], dtype, mode);
                old.prologue(&mut a, &n).unwrap();
                new.prologue_typed(&mut z, &n).unwrap();
                paired(&a, &z);
                state(&old, &new);
                assert_eq!(a.code.len() > if recv { 1 } else { 2 }, pin);
            }
        }
    }
}

#[test]
fn prologue_refusals_match() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for n in [
        node("add", vec![], vec![4, 32], "f16", "f16"),
        node(
            "unpack",
            vec![Term::name("missing")],
            vec![4, 32],
            "f16",
            "f16",
        ),
        node("unpack", vec![Term::name("src")], vec![4], "f16", "gt4"),
    ] {
        let mut old = Ctx::new(&b, &t, &Env::new(), None).unwrap();
        let mut new = Ctx::new(&b, &t, &Env::new(), None).unwrap();
        old.vals.insert("src".into(), Val::new("l1", 1));
        new.vals = old.vals.clone();
        let (mut a, mut z) = (Gen::default(), Emitter::default());
        assert_eq!(
            format!("{:?}", old.prologue(&mut a, &n).unwrap_err()),
            format!("{:?}", new.prologue_typed(&mut z, &n).unwrap_err())
        );
        paired(&a, &z);
        state(&old, &new);
    }
}
