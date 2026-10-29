//! Differential pack/unpack checks against the template emitter.

use super::*;
use crate::lower::vector::ctx::emit::typed::Emitter;
use crate::lower::vector::gen::typed::Event;
use crate::lower::vector::tree::typed as tt;
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

fn operation(
    name: &str,
    target: &str,
    args: Vec<Term>,
    mode: &str,
    dtype: &str,
    shape: Vec<i64>,
    uid: usize,
) -> Op {
    Op {
        uid,
        st: Stmt::new(name, args.clone()),
        op: name.into(),
        target: Some(target.into()),
        args,
        ty: Some(Type {
            dtype: dtype.into(),
            shape,
            space: if name == "pack" { "l1" } else { "reg" }.into(),
            mode: Some(mode.into()),
        }),
    }
}
fn node(o: Op) -> Arc<Node> {
    Arc::new(Node::Op(o))
}
fn row(mode: &str) -> Val {
    let mut v = Val::new("row", 32).with_mode(mode);
    v.cols = 128;
    for r in 0..32 {
        v.regs.insert(r, format!("%{}", r + 1));
    }
    v
}
fn source(mode: &str, unpk: &str, rows_w: i64, bcast: bool) -> Val {
    let mut v = Val::new("l1", 32).with_mode(mode);
    v.buf = 19;
    v.unpk = unpk.into();
    v.per = if unpk == "f32" {
        16
    } else if mode.starts_with("mx7") {
        4
    } else {
        8
    };
    v.rows_w = rows_w;
    v.bcast = bcast;
    v
}
fn paired(a: &Gen, z: &Emitter) {
    tt::tests::events(
        a,
        &tt::Gen {
            n: z.n,
            code: z.code.clone(),
        },
    );
}
fn state(a: &Ctx, z: &Ctx, al: &IndexMap<String, Val>, zl: &IndexMap<String, Val>) {
    assert_eq!(format!("{al:?}"), format!("{zl:?}"));
    assert_eq!(format!("{:?}", a.vals), format!("{:?}", z.vals));
    assert_eq!(a.scalars, z.scalars);
    assert_eq!(a.held, z.held);
    assert_eq!(a.pinned, z.pinned);
    assert_eq!(a.staged, z.staged);
    assert_eq!(format!("{:?}", a.descs), format!("{:?}", z.descs));
    assert_eq!(a.of, z.of);
    assert_eq!(a.rel, z.rel);
}
fn result(a: VResult<()>, z: VResult<()>, ok: bool) {
    assert_eq!(format!("{a:?}"), format!("{z:?}"));
    assert_eq!(a.is_ok(), ok, "{a:?}");
}
fn generators() -> (Gen, Emitter) {
    (
        Gen {
            n: 40,
            code: vec![],
        },
        Emitter {
            n: 40,
            code: vec![],
        },
    )
}

#[test]
fn line_modes_walks_and_counts() {
    let (mut a, mut z) = generators();
    let s = Symbol(7);
    for mode in ["f16", "gt4", "mx7a", "mx7b"] {
        for unpk in ["", "f32"] {
            for rows_w in [0, 2, 4, 8] {
                for bcast in [false, true] {
                    let src = source(mode, unpk, rows_w, bcast);
                    let d = Desc {
                        rows_w,
                        walk: Walk::flat(32, 8),
                        ..Default::default()
                    };
                    for i in [0, 1, 3, 7, 9] {
                        a.emit(unpack_line(&src, i), &["%7"]);
                        z.op(unpack_line_typed(&src, i, &s), vec![s.clone()]);
                        for dtype in ["f16", "f32"] {
                            a.emit(pack_line(&d, 23, i, mode, dtype), &["%7"]);
                            z.op(pack_line_typed(&d, 23, i, mode, dtype, &s), vec![s.clone()]);
                        }
                    }
                }
            }
        }
    }
    // Counts are omitted unless explicitly requested, even on selected operands.
    for (selection, suffix) in [
        (Select::Whole, ""),
        (Select::Chunk(3), "[3]"),
        (Select::Stride(0), "[0:]"),
        (Select::Stride(4), "[4:]"),
    ] {
        for (kind, op, arrow) in [
            (ir::Memory::Pack, "vpack", "->"),
            (ir::Memory::Unpack, "vunpk", "<-"),
        ] {
            for n in [None, Some(1), Some(4), Some(8)] {
                for rel in [false, true] {
                    let opts = n
                        .map(|n| vec![ir::kw("n", Term::int(n))])
                        .unwrap_or_default();
                    a.emit(
                        format!(
                            "{op} {{0}}{suffix} {arrow} l1[13] f16{}{}",
                            if rel { " rel" } else { "" },
                            n.map(|n| format!(" n={n}")).unwrap_or_default()
                        ),
                        &["%7"],
                    );
                    z.op(
                        ir::memory(kind, s.operand(selection), 13, Some("f16"), rel, opts).unwrap(),
                        vec![s.clone()],
                    );
                }
            }
        }
    }
    paired(&a, &z);
}

#[test]
fn row_relative_and_statistic_unpacks() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for (mode, unpk, rows_w) in [
        ("f16", "", 0),
        ("gt4", "", 0),
        ("f16", "f32", 0),
        ("gt4", "mx7a", 0),
        ("gt4", "mx7b", 0),
        ("f16", "", 4),
    ] {
        for relative in [false, true] {
            for bcast in [false, true] {
                let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                let o = operation(
                    "unpack",
                    "out",
                    vec![Term::name("src")],
                    mode,
                    "f32",
                    vec![32, 128],
                    7,
                );
                for c in [&mut a, &mut z] {
                    c.tile = 16;
                    c.vals
                        .insert("src".into(), source(mode, unpk, rows_w, bcast));
                    if relative {
                        c.rel = Some((7, "src".into(), 0, 16));
                    }
                }
                let ops = vec![node(o)];
                let rows = [9, 0, 1, 9, 8, 15];
                let (mut ag, mut zg) = generators();
                let mut al = IndexMap::new();
                let mut zl = al.clone();
                for only in [Some("absent"), None, Some("out")] {
                    result(
                        a.unpacks(&mut ag, &ops, &rows, &mut al, only),
                        z.unpacks_typed(&mut zg, &ops, &rows, &mut zl, only),
                        true,
                    );
                    paired(&ag, &zg);
                    state(&a, &z, &al, &zl);
                }
                assert!(!ag.code.is_empty());
            }
        }
    }
    for per in [0, 1, 2, 4] {
        for lane in [None, Some(0), Some(7), Some(15), Some(-1), Some(16)] {
            let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
            let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
            for c in [&mut a, &mut z] {
                c.per_band = per;
                c.tile = 32;
                c.vals.insert("src".into(), source("f16", "", 0, false));
            }
            let mut args = vec![Term::name("src")];
            if let Some(l) = lane {
                args.push(ir::kw("lane", Term::dec(l)));
            }
            let ops = vec![node(operation(
                "unpack",
                "out",
                args,
                "gt4",
                "f32",
                vec![32],
                7,
            ))];
            let rows = [31, 0, 1, 8, 16, 31, 7];
            let (mut ag, mut zg) = generators();
            let mut al = IndexMap::new();
            let mut zl = al.clone();
            let ok = lane.map(|l| (0..16).contains(&l)).unwrap_or(per != 0);
            result(
                a.unpacks(&mut ag, &ops, &rows, &mut al, None),
                z.unpacks_typed(&mut zg, &ops, &rows, &mut zl, None),
                ok,
            );
            paired(&ag, &zg);
            state(&a, &z, &al, &zl);
            if ok {
                assert!(ag
                    .code
                    .iter()
                    .any(|c| matches!(c, Code::Op(s,_) if s.ends_with("n=1"))));
            }
        }
    }
}

fn stores(c: &mut Ctx, rows_w: i64, send: bool) -> Vec<Arc<Node>> {
    // The third descriptor replaces the first, preserving the first buffer's slot.
    c.descs = (0..3)
        .map(|i| Desc {
            buf: if i == 1 {
                vec![101, 201]
            } else {
                vec![17, 117]
            },
            rows_w,
            walk: Walk::flat(32, 8),
            ..Default::default()
        })
        .collect();
    (0..3)
        .map(|i| {
            c.of.insert(10 + i, i);
            node(operation(
                if send { "send" } else { "store" },
                "unused",
                if send {
                    vec![Term::name("out")]
                } else {
                    vec![Term::name("mem"), Term::int(0), Term::name("out")]
                },
                "f16",
                "f16",
                vec![32, 128],
                10 + i,
            ))
        })
        .collect()
}

#[test]
fn packs_modes_destinations_and_mx7_order() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for (dtype, mode) in [
        ("f16", "f16"),
        ("f32", "f16"),
        ("f16", "gt4"),
        ("mx7a", "gt4"),
        ("mx7b", "gt4"),
    ] {
        for rows_w in [0, 4] {
            for parity in [0, 1] {
                for send in [false, true] {
                    for producer in [false, true] {
                        for extra_read in [false, true] {
                            let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                            let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                            a.tile = 16;
                            z.tile = 16;
                            let mut ops = vec![node(operation(
                                "pack",
                                "out",
                                vec![Term::name("src")],
                                mode,
                                dtype,
                                vec![32, 128],
                                7,
                            ))];
                            ops.extend(stores(&mut a, rows_w, send));
                            stores(&mut z, rows_w, send);
                            if extra_read {
                                ops.push(node(operation(
                                    "mov",
                                    "other",
                                    vec![Term::name("src")],
                                    mode,
                                    "f32",
                                    vec![32, 128],
                                    8,
                                )));
                            }
                            let (mut ag, mut zg) = generators();
                            let mut al = IndexMap::from([
                                ("src".into(), row(mode)),
                                ("input".into(), row(mode)),
                            ]);
                            let mut zl = al.clone();
                            let rows = [2, 0, 3];
                            if producer {
                                let m = operation(
                                    "add",
                                    "src",
                                    vec![Term::name("input"), Term::int(1)],
                                    mode,
                                    "f32",
                                    vec![32, 128],
                                    9,
                                );
                                result(
                                    a.math(&mut ag, &m, &rows, &mut al),
                                    z.math_typed(&mut zg, &m, &rows, &mut zl),
                                    true,
                                );
                                paired(&ag, &zg);
                                state(&a, &z, &al, &zl);
                            }
                            result(
                                a.packs(&mut ag, &ops, &rows, &mut al, parity),
                                z.packs_typed(&mut zg, &ops, &rows, &mut zl, parity),
                                true,
                            );
                            paired(&ag, &zg);
                            state(&a, &z, &al, &zl);
                            assert_eq!(al["out"].buf, if parity == 0 { 17 } else { 117 });
                            assert_eq!(
                                ag.code
                                    .iter()
                                    .filter(|e| matches!(e,Code::Op(s,_) if s.starts_with("vpack")))
                                    .count(),
                                6
                            );
                            if dtype.starts_with("mx7") {
                                let moves = ag
                                    .code
                                    .iter()
                                    .filter(|e| matches!(e,Code::Op(s,_) if s.starts_with("vmov") || s.starts_with("vsel")))
                                    .count();
                                // A sent pack can feed a K pair: its zeros lift
                                // (NUM.MX7.008), one whole-register vsel a row in order.
                                let ordered = if send { rows.len() } else { 0 };
                                assert_eq!(
                                    moves,
                                    if producer && !extra_read { ordered } else { 24 }
                                );
                            }
                        }
                    }
                }
            }
        }
    }
}

#[test]
fn compact_statistics_one_word_and_final_band() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for nb in [1, 4, 8] {
        for per in [1, 2, 4] {
            for parity in [0, 1] {
                for final_band in [false, true] {
                    let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                    let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
                    for c in [&mut a, &mut z] {
                        c.per_band = per;
                        c.tile = nb * per;
                    }
                    let mut ops = vec![node(operation(
                        "pack",
                        "out",
                        vec![Term::name("src")],
                        "f16",
                        "f16",
                        vec![if nb == 1 { 16 } else { 4 * nb }],
                        7,
                    ))];
                    ops.extend(stores(&mut a, 0, false));
                    stores(&mut z, 0, false);
                    let mut stat = row("gt4");
                    stat.kind = "stat";
                    let mut al = IndexMap::from([("src".into(), stat)]);
                    let mut zl = al.clone();
                    let (mut ag, mut zg) = generators();
                    let rows = if final_band {
                        vec![(nb - 1) * per]
                    } else {
                        vec![]
                    };
                    result(
                        a.packs(&mut ag, &ops, &rows, &mut al, parity),
                        z.packs_typed(&mut zg, &ops, &rows, &mut zl, parity),
                        true,
                    );
                    paired(&ag, &zg);
                    state(&a, &z, &al, &zl);
                    let packed: Vec<_> = ag
                        .code
                        .iter()
                        .filter_map(|e| match e {
                            Code::Op(s, _) if s.starts_with("vpack") => Some(s),
                            _ => None,
                        })
                        .collect();
                    assert_eq!(
                        packed.len(),
                        if final_band {
                            2 * (if nb == 1 { 1 } else { nb / 4 }) as usize
                        } else {
                            0
                        }
                    );
                    assert!(packed.iter().all(|s| s.ends_with("f16 n=1")));
                }
            }
        }
    }
}

#[test]
fn reloads_block_edges_relative_exclusion_and_unpin_fusions() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for relative in [false, true] {
        for fused in [false, true] {
            let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
            let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
            let ops = vec![
                node(operation(
                    "unpack",
                    "x",
                    vec![Term::name("src")],
                    "f16",
                    "f32",
                    vec![32, 128],
                    7,
                )),
                node(operation(
                    "unpack",
                    "y",
                    vec![Term::name("src")],
                    "f16",
                    "f32",
                    vec![32, 128],
                    8,
                )),
            ];
            for c in [&mut a, &mut z] {
                c.tile = 16;
                c.pin = false;
                c.pre = ops.clone();
                c.vals.insert("src".into(), source("f16", "", 4, false));
                if relative {
                    c.rel = Some((7, "src".into(), 0, 16));
                }
            }
            let o = operation(
                "add",
                "out",
                vec![
                    Term::name(if fused { "product" } else { "x" }),
                    Term::name("x"),
                    Term::name("y"),
                ],
                "f16",
                "f32",
                vec![32, 128],
                9,
            );
            let f = IndexMap::from([("product".into(), (Term::name("x"), Term::name("y")))]);
            let unpin_op = operation(
                "mov",
                "out",
                vec![
                    Term::name(if fused { "product" } else { "x" }),
                    Term::name(if fused { "product" } else { "y" }),
                ],
                "f16",
                "f32",
                vec![32, 128],
                10,
            );
            let mut al = IndexMap::new();
            let mut zl = al.clone();
            let (mut ag, mut zg) = generators();
            let mut aa = HashSet::new();
            let mut za = aa.clone();
            for edge in [None, Some(true), Some(false)] {
                if let Some(open) = edge {
                    ag.raw(if open { "{" } else { "}" });
                    zg.code.push(if open { Event::Open } else { Event::Close });
                    aa.clear();
                    za.clear();
                }
                let rows = [3, 0, 3, 1];
                result(
                    a.reunpack(&mut ag, &ops, &o, &rows, &mut al, &mut aa),
                    z.reunpack_typed(&mut zg, &ops, &o, &rows, &mut zl, &mut za),
                    true,
                );
                assert_eq!(aa, za);
                assert_eq!(aa.contains("x"), !relative);
                paired(&ag, &zg);
                state(&a, &z, &al, &zl);
                let before = ag.code.len();
                result(
                    a.reunpack(&mut ag, &ops, &o, &rows, &mut al, &mut aa),
                    z.reunpack_typed(&mut zg, &ops, &o, &rows, &mut zl, &mut za),
                    true,
                );
                assert_eq!(ag.code.len(), before);
                paired(&ag, &zg);
                aa.clear();
                za.clear();
                result(
                    a.unpin(
                        &mut ag,
                        &unpin_op,
                        &rows,
                        &mut al,
                        &mut aa,
                        if fused { Some(&f) } else { None },
                    ),
                    z.unpin_typed(
                        &mut zg,
                        &unpin_op,
                        &rows,
                        &mut zl,
                        &mut za,
                        if fused { Some(&f) } else { None },
                    ),
                    true,
                );
                assert_eq!(aa, za);
                assert_eq!(aa.len(), 2);
                paired(&ag, &zg);
                state(&a, &z, &al, &zl);
                let before = ag.code.len();
                result(
                    a.unpin(&mut ag, &unpin_op, &rows, &mut al, &mut aa, Some(&f)),
                    z.unpin_typed(&mut zg, &unpin_op, &rows, &mut zl, &mut za, Some(&f)),
                    true,
                );
                assert_eq!(ag.code.len(), before);
                paired(&ag, &zg);
                state(&a, &z, &al, &zl);
            }
        }
    }
}

#[test]
fn pack_unpack_refusals_and_partial_state() {
    let b = body();
    let t = Target::of(&Machine::named("multimesh_v9t6").unwrap());
    for case in 0..8 {
        let mut a = Ctx::new(&b, &t, &Env::new(), None).unwrap();
        let mut z = Ctx::new(&b, &t, &Env::new(), None).unwrap();
        let mut src = row("f16");
        if case >= 3 {
            src.kind = "stat";
        }
        let mut al = IndexMap::from([("src".into(), src)]);
        let mut o = operation(
            "pack",
            "out",
            vec![Term::name(if case == 0 { "missing" } else { "src" })],
            "f16",
            if case == 2 { "mx7a" } else { "f16" },
            if case == 3 {
                vec![16, 128]
            } else {
                vec![if case == 5 { 17 } else { 16 }]
            },
            7,
        );
        let mut ops = vec![];
        if case != 1 {
            ops = stores(&mut a, 0, false);
            stores(&mut z, 0, false);
        }
        if case != 4 {
            a.per_band = 2;
            z.per_band = 2;
        }
        a.tile = 8;
        z.tile = 8;
        if case >= 6 {
            o = operation(
                "unpack",
                "out",
                vec![Term::name("src")],
                if case == 6 { "gt4" } else { "f16" },
                "f32",
                vec![32, 128],
                7,
            );
            al.insert("src".into(), source("f16", "", 0, false));
            if case == 7 {
                al.get_mut("src").unwrap().kind = "row";
            }
        }
        ops.insert(0, node(o));
        let (mut ag, mut zg) = generators();
        // A preceding valid operation leaves observable progress on refusal.
        al.insert("good".into(), source("f16", "", 0, false));
        let mut zl = al.clone();
        let valid = vec![node(operation(
            "unpack",
            "prior",
            vec![Term::name("good")],
            "f16",
            "f32",
            vec![32, 128],
            99,
        ))];
        result(
            a.unpacks(&mut ag, &valid, &[0, 1], &mut al, None),
            z.unpacks_typed(&mut zg, &valid, &[0, 1], &mut zl, None),
            true,
        );
        assert_eq!(ag.code.len(), 2);
        if case < 6 {
            result(
                a.packs(&mut ag, &ops, &[0], &mut al, 0),
                z.packs_typed(&mut zg, &ops, &[0], &mut zl, 0),
                false,
            );
        } else {
            result(
                a.unpacks(&mut ag, &ops, &[0], &mut al, None),
                z.unpacks_typed(&mut zg, &ops, &[0], &mut zl, None),
                false,
            );
        }
        paired(&ag, &zg);
        state(&a, &z, &al, &zl);
    }
}
