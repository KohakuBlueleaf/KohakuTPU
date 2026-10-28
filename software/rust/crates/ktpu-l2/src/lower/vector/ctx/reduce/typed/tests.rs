//! Reduction context and interleaved event differential tests.

use super::*;
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
fn op_for(name: &str, arg: Term) -> Op {
    Op {
        uid: 0,
        st: Stmt::new(name, vec![arg.clone()]),
        op: name.into(),
        target: Some("out".into()),
        args: vec![arg],
        ty: Some(Type {
            dtype: "f32".into(),
            shape: vec![16],
            space: "reg".into(),
            mode: None,
        }),
    }
}
fn slice(lo: i128, hi: i128) -> Term {
    Term::View(
        "a".into(),
        vec![
            Term::Slice(None, None),
            Term::Slice(Some(Box::new(Term::int(lo))), Some(Box::new(Term::int(hi)))),
        ],
    )
}
fn run(o: &Op, per: i64, inplace: bool, fuse: bool, groups: &[Vec<i64>], want_ok: bool) {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
    for c in [&mut a, &mut z] {
        c.per_band = per;
        c.inplace = inplace;
        c.tile = 16;
    }
    let mut al = IndexMap::new();
    for (i, name) in ["a", "b", "product", "out"].into_iter().enumerate() {
        if name == "out" && !inplace {
            continue;
        }
        let mut v = Val::new(if name == "out" { "stat" } else { "row" }, 16);
        v.cols = 32 * per.max(1);
        for r in 0..16 {
            v.regs.insert(r, format!("%{}", i * 16 + r as usize + 1));
        }
        al.insert(name.into(), v);
    }
    let mut zl = al.clone();
    let mut fused = IndexMap::new();
    if fuse {
        fused.insert("product".into(), (Term::name("a"), Term::name("b")));
    }
    let mut ag = Gen {
        n: 100,
        code: vec![],
    };
    let mut zg = tt::Gen {
        n: 100,
        code: vec![],
    };
    let ar = a.reduces(&mut ag, o, groups, &mut al, &fused);
    let zr = z.reduces_typed(&mut zg, o, groups, &mut zl, &fused);
    assert_eq!(ar.is_ok(), want_ok, "{ar:?}");
    assert_eq!(format!("{ar:?}"), format!("{zr:?}"));
    tt::tests::events(&ag, &zg);
    assert_eq!(format!("{al:?}"), format!("{zl:?}"));
    assert_eq!(format!("{:?}", a.vals), format!("{:?}", z.vals));
    assert_eq!(a.pinned, z.pinned);
    assert_eq!(a.scalars, z.scalars);
    assert_eq!(a.held, z.held);
    assert_eq!(a.staged, z.staged);
}

#[test]
fn reductions_fused_groups_slices_and_inplace() {
    for (op, _) in REDUCE {
        for fused in [false, true] {
            for inplace in [false, true] {
                let o = op_for(op, Term::name(if fused { "product" } else { "a" }));
                run(
                    &o,
                    0,
                    inplace,
                    fused,
                    &[(0..8).collect(), (8..16).collect()],
                    true,
                );
                for per in [1, 2, 3, 4, 8] {
                    let grps = vec![(0..per).collect(), (per..2 * per).collect()];
                    run(&o, per, inplace, fused, &grps, true);
                }
            }
        }
        for (lo, hi) in [(0, 32), (32, 96), (0, 128), (64, 128)] {
            run(
                &op_for(op, slice(lo, hi)),
                4,
                true,
                false,
                &[(0..4).collect(), (4..8).collect()],
                true,
            );
        }
    }
}

#[test]
fn reduction_refusals_preserve_partial_state() {
    for arg in [
        slice(1, 32),
        slice(0, 33),
        slice(32, 32),
        slice(0, 160),
        Term::View("a".into(), vec![Term::int(0)]),
        Term::View(
            "a".into(),
            vec![
                Term::int(0),
                Term::Slice(Some(Box::new(Term::int(0))), Some(Box::new(Term::int(32)))),
            ],
        ),
    ] {
        run(
            &op_for("rsum", arg),
            4,
            false,
            false,
            &[(0..4).collect()],
            false,
        );
    }
    run(
        &op_for("rsum", Term::name("missing")),
        4,
        false,
        false,
        &[(0..4).collect()],
        false,
    );
    run(
        &op_for("rsum", Term::name("a")),
        0,
        false,
        false,
        &[(0..8).collect(), vec![8, 9]],
        false,
    );
    let mut o = op_for("rsum", Term::name("a"));
    o.ty = None;
    run(&o, 0, false, false, &[(0..8).collect()], false);
}
