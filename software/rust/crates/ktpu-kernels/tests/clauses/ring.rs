//! The product ring (`Knobs::ring_depth`): its shape on every machine, and
//! the kernels it gives for each epilogue the kernel without it takes.

use super::{card, lines, text};
use ktpu_graph as g;
use ktpu_kernels::expr;
use ktpu_kernels::gen::matmul::{self as mm, ring_rows, Knobs, Ring};
use ktpu_kernels::gen::vector::{Kind, Operand};
use ktpu_kernels::gen::Hw;
use ktpu_machine::{Machine, Pairing, PAIRING};
use serde_json::json;

fn ring(depth: i64) -> Knobs {
    Knobs {
        ring_depth: depth,
        ..mm::defaults()
    }
}

/// The widest tile `mach` plans under `pairing`: M and K of 4096, N four of
/// the widest blocks.
fn widest(mach: &Machine, pairing: Pairing) -> mm::Mm {
    let n = 4 * Hw::of(mach).max_block();
    mm::plan(1, 4096, n, 4096, true, 4, pairing, mach, &mm::defaults()).unwrap()
}

#[test]
fn the_widest_tile_rings_at_depth_one_on_every_machine() {
    let names = ktpu_machine::names(&ktpu_machine::machines_dir());
    assert!(names.len() > 1);
    let full = Operand::full(0, 0);
    for name in names {
        let mach = Machine::named(&name).unwrap();
        for pairing in [Pairing::Vector, Pairing::Unpaired] {
            let p = widest(&mach, pairing);
            let r = mm::ring_resolve(&p, &expr::load(0), &full, &ring(1), &mach);
            assert_eq!(r.depth, 1, "{name} {pairing:?}: {p:?} {r:?}");
        }
    }
    // A staging store a sixteenth the size bounds the tile to hold a wave.
    let small = card()
        .with_fact("die.staging.entries", json!(1024))
        .unwrap();
    let (big, bounded) = (widest(&card(), PAIRING), widest(&small, PAIRING));
    assert!(ring_rows(&small) < big.tm, "{}", ring_rows(&small));
    assert!(bounded.tm <= ring_rows(&small) && bounded.tm < big.tm);
    let r = mm::ring_resolve(&bounded, &expr::load(0), &full, &ring(1), &small);
    assert_eq!(r.depth, 1);
}

#[test]
fn the_ring_holds_what_the_staging_room_holds() {
    let p = mm::plan(2, 256, 256, 64, false, 4, PAIRING, &card(), &mm::defaults()).unwrap();
    let full = Operand::full(0, 0);
    let r = |d| mm::ring_resolve(&p, &expr::load(0), &full, &ring(d), &card());
    let wave = 2 * r(1).lanes * p.tm * p.tn;
    let most = card().stage_room() / wave;
    assert!(most >= 2, "{most}");
    assert_eq!((r(0).depth, r(1).depth, r(2).depth), (0, 1, 2));
    assert_eq!(r(most + 5).depth, most);
}

#[test]
fn a_reduction_or_a_statistic_rings_whole_rows() {
    let p = mm::plan(1, 64, 1024, 64, true, 16, PAIRING, &card(), &mm::defaults()).unwrap();
    assert!(p.n / p.tn > 1, "{p:?}");
    let sum = expr::reduce(expr::RedOp::Sum, expr::load(0));
    let (row, full) = (Operand::new(Kind::Row), Operand::full(0, 0));
    for (e, out) in [(&sum, &full), (&expr::load(0), &row)] {
        let whole = mm::ring_resolve(&p, e, out, &ring(1), &card());
        assert_eq!((whole.tiles, whole.lanes), (p.n / p.tn, 1));
    }
    let one = mm::ring_resolve(&p, &expr::load(0), &full, &ring(1), &card());
    assert_eq!((one.tiles, one.lanes), (1, 2));
}

/// With `split_vc` a lone row group's rows split over every core.
#[test]
fn split_vc_shares_a_row_group_over_the_cores() {
    let p = mm::plan(1, 32, 256, 64, true, 4, PAIRING, &card(), &mm::defaults()).unwrap();
    let full = Operand::full(0, 0);
    let split = Knobs {
        split_vc: true,
        ..ring(1)
    };
    let r = |k: &Knobs| mm::ring_resolve(&p, &expr::load(0), &full, k, &card());
    assert_eq!((r(&ring(1)).lanes, r(&ring(1)).parts), (1, 1));
    assert_eq!(r(&split).parts, Hw::of(&card()).vc);
}

/// Every epilogue form: (expression, operands, output, quantum).
fn epilogues() -> Vec<(&'static str, expr::E, Vec<Operand>, Operand, i64)> {
    let add = |a: expr::Operand, b: expr::Operand| expr::op("add", vec![a, b]).unwrap();
    let full = Operand::full(256, 0);
    let col = Operand {
        offset: 32,
        ..Operand::new(Kind::Col)
    };
    let stat = Operand::new(Kind::Row);
    let scalar = Operand {
        offset: 3,
        ..Operand::new(Kind::Scalar)
    };
    let relu = expr::op("max", vec![expr::load(0).into(), 0.0.into()]).unwrap();
    vec![
        ("relu", relu, vec![], Operand::full(256, 0), 4),
        (
            "full",
            add(expr::load(0).into(), expr::load(1).into()),
            vec![full],
            Operand::full(256, 0),
            4,
        ),
        (
            "col",
            add(expr::load(0).into(), expr::load(1).into()),
            vec![col],
            Operand::full(256, 0),
            4,
        ),
        (
            "stat",
            add(expr::load(0).into(), expr::load(1).into()),
            vec![stat],
            Operand::full(256, 0),
            16,
        ),
        (
            "scalar",
            add(expr::load(0).into(), expr::load(1).into()),
            vec![scalar],
            Operand::full(256, 0),
            4,
        ),
        (
            "sum",
            expr::reduce(expr::RedOp::Sum, expr::load(0)),
            vec![],
            Operand::full(256, 0),
            4,
        ),
        (
            "sum_row",
            expr::reduce(expr::RedOp::Sum, expr::load(0)),
            vec![],
            Operand::new(Kind::Row),
            16,
        ),
    ]
}

/// The ring takes every epilogue the kernel without it takes, under each
/// knob (a node loop of `pipe=depth` over `@stage` slots, no DRAM product
/// scratch), and refuses what it refuses, by the same clause.
#[test]
fn the_ring_takes_every_epilogue() {
    let mut built = std::collections::BTreeSet::new();
    let knobs = [
        ring(1),
        ring(2),
        Knobs {
            split_vc: true,
            ..ring(2)
        },
        Knobs {
            tail_bands: true,
            ..ring(2)
        },
    ];
    for (name, e, extras, out, quantum) in epilogues() {
        for (bt, m, pad_m) in [(1, 64, false), (2, 197, true)] {
            let plan = Knobs {
                pad_m,
                ..mm::defaults()
            };
            let p = mm::plan(bt, m, 256, 64, false, quantum, PAIRING, &card(), &plan).unwrap();
            let base = mm::mm_kernel(&p, &e, &extras, &out, &mm::defaults(), &card());
            for k in &knobs {
                let r = mm::ring_resolve(&p, &e, &out, k, &card());
                assert_eq!(r.depth, k.ring_depth, "{name}");
                let ringed = mm::mm_kernel(&p, &e, &extras, &out, k, &card());
                let ringed = match (&base, ringed) {
                    (Ok(_), Ok(k)) => k,
                    (Err(a), Err(b)) => {
                        assert_eq!(a.clause, b.clause, "{name} {k:?}");
                        continue;
                    }
                    (a, b) => panic!("{name} {k:?}: {:?} / {:?}", a.as_ref().err(), b.err()),
                };
                built.insert(name);
                let t = text(&ringed);
                assert!(
                    t.contains(&format!("pipe={}", k.ring_depth)),
                    "{name} {k:?}\n{t}"
                );
                let scratch: Vec<g::BufSpace> =
                    ringed.graph.scratch.iter().map(|s| s.spec.space).collect();
                assert_eq!(scratch, vec![g::BufSpace::Stage], "{name} {k:?}");
                let vc = Hw::of(&card()).vc;
                assert!(r.lanes * r.parts <= vc.max(r.lanes), "{name}: {r:?}");
                assert!(!lines(&ringed, "par").is_empty());
            }
        }
    }
    eprintln!("built: {built:?}");
    for name in ["relu", "full", "col", "stat", "scalar", "sum"] {
        assert!(built.contains(name), "{name}: {built:?}");
    }
}

/// A ring the staging room cannot hold one wave of is no ring: the
/// kernel is the kernel without it.
#[test]
fn a_ring_past_the_room_is_the_dram_product() {
    let p = mm::plan(1, 64, 96, 64, true, 4, PAIRING, &card(), &mm::defaults()).unwrap();
    let small = card().with_fact("die.staging.entries", json!(64)).unwrap();
    let sum = expr::reduce(expr::RedOp::Sum, expr::load(0));
    let full = Operand::full(96, 0);
    assert_eq!(
        mm::ring_resolve(&p, &sum, &full, &ring(2), &card()).depth,
        2
    );
    let r: Ring = mm::ring_resolve(&p, &sum, &full, &ring(2), &small);
    assert_eq!(r.depth, 0, "{r:?}");
    let a = mm::mm_kernel(&p, &sum, &[], &full, &ring(2), &small).unwrap();
    let b = mm::mm_kernel(&p, &sum, &[], &full, &mm::defaults(), &small).unwrap();
    assert_eq!(a.graph, b.graph);
}
