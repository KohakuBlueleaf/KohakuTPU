//! L2 to L1: the lowering alone, each hand-written L2 fixture against its L1.

#[path = "../common.rs"]
mod common;
mod events;
mod fill_throttle;
mod modulo_memory_effects;
mod release_frontier;
mod stream_credit;
mod view_progression;

use common::{fixtures, known, stage_suite, Fixture, Level, Pair};
use ktpu::{Backend, CardStack};
use ktpu_isa::Machine as Card;

fn lower(f: &Fixture, card: &Card) -> Result<Pair, String> {
    let s = CardStack::default();
    let l2 = s.parse_l2(&f.source).map_err(|e| e.to_string())?;
    let placed = s.passes(&l2, &f.name, card).map_err(|e| e.to_string())?;
    let l1 = s.lower(&placed, &f.name, card).map_err(|e| e.to_string())?;
    Ok(Pair {
        name: f.name.clone(),
        before: (Level::L2, s.print_l2(&placed)),
        after: (Level::L1, s.print_l1(&l1)),
    })
}

/// The L1 of fixture `name` under `dir`.
fn lowered(dir: &str, name: &str) -> String {
    let corpus = fixtures(&[dir], Level::L2);
    let f = corpus.iter().find(|f| f.name == name).expect("the fixture");
    lower(f, &common::card()).expect("it lowers").after.1
}

/// The L1 of the planner's own transposed read, its fixture harvested.
fn transposed(name: &str) -> String {
    lowered("tests/fixtures/l2/harvest", name)
}

#[test]
fn a_padded_pack_gathers_whole_tile_rows_and_checks_once() {
    let l1 = lowered("tests/fixtures/l2/quant", "quant_padded_heads");
    // Four live 32-column entries a row: one 256-byte burst, not four.
    assert!(l1.contains("walk=((32, 8), (288, 4),"), "{l1}");
    // Each quant clears and reads its finite status once, whatever its pieces.
    let runs = |image: &str| l1.matches(&format!(".{image}_")).count();
    assert_eq!(runs("mx7_clear"), 2 * 2 + 2, "two runs and two images");
    assert_eq!(runs("mx7_status"), 2 * 2 + 2);
    assert_eq!(l1.matches("assert_zero").count(), 2 * 2);
}

#[test]
fn padding_only_pack_does_not_read_its_source_anchor() {
    let fixture = Fixture {
        label: "padding-only-pack".into(),
        name: "padding_only".into(),
        source: r#"target ktpu.v9
fn padding_only.l2(x: f16[4, 32] @dram, q: mx7a[4, 32] @dram(gm=1, nk=1))
    quant q <- x pad=1 zero=1
"#
        .into(),
    };
    let pair = lower(&fixture, &common::card()).expect("padding-only pack");
    assert!(!pair.after.1.contains("vfill"), "{}", pair.after.1);
    let nan = |mem: &mut [u8], binds: &ktpu_sim::l1::Binds| {
        let at = binds["x"] as usize;
        for value in mem[at..at + 4 * 32 * 2].chunks_exact_mut(2) {
            value.copy_from_slice(&0x7E00u16.to_le_bytes());
        }
    };
    assert!(common::stops(&pair, false, &nan).is_none());
    assert!(common::stops(&pair, true, &nan).is_none());
}

#[test]
fn a_paired_pack_image_runs_its_loop_unguarded() {
    // Whole-row K-pair tiles make a loop body past VSKIPZ's 8-bit skip.
    let l1 = lowered(
        "kohakutpu/language/kohakutpu/language/examples/l2",
        "convert",
    );
    assert!(l1.contains("loop s0"), "{l1}");
    assert!(!l1.contains("skipz s0"), "{l1}");
}

#[test]
fn padded_heads_defer_to_one_check() {
    let l1 = lowered("tests/fixtures/l2/quant", "quant_padded_deferred");
    assert_eq!(l1.matches("barrier").count(), 1, "{l1}");
    assert_eq!(l1.matches("assert_zero").count(), 2 * 2);
}

#[test]
fn a_tile_of_whole_source_words_exchanges_words_and_lanes() {
    let l1 = transposed("rows_8021be9bb8");
    assert!(l1.contains("gt4 walk=(4, 16)"));
    assert!(!l1.contains("bcast("), "lanes placed one by one");
}

#[test]
fn a_column_tail_exchanges_its_half_tile() {
    let l1 = transposed("rows_4b277ed3d8");
    assert!(l1.contains("gt4 walk=(4, 16) n=4"));
}

#[test]
fn a_pack_of_transposed_rows_exchanges_words_and_lanes() {
    let l1 = transposed("quant_ec317a9aa7");
    assert!(l1.contains("gt4 walk=(4, 16)"));
    assert!(!l1.contains("bcast("), "lanes placed one by one");
}

#[test]
fn a_packed_value_reads_back_once_its_pack_is_done() {
    let l1 = lowered("tests/fixtures/l2/fused", "packed_readback");
    let pack = l1.find("vpack").expect("the pack");
    let wait = l1
        .find("vsync unpack on=pack")
        .expect("the read-back waits");
    assert!(pack < wait, "{l1}");
}

#[test]
fn a_packed_value_read_back_computes_what_its_l2_does() {
    let corpus = fixtures(&["tests/fixtures/l2/fused"], Level::L2);
    for name in ["packed_readback", "compensated_readback"] {
        let f = corpus.iter().find(|f| f.name == name).expect("the fixture");
        let pair = lower(f, &common::card()).expect("it lowers");
        // The L1's values on seeded inputs against the L2's.
        assert_eq!(common::compare(&pair), Ok(None), "{name}");
    }
}

#[test]
fn a_checked_pack_drains_one_record_a_core() {
    let l1 = lowered("tests/fixtures/l2/fused", "checked_pack");
    let runs = |image: &str| l1.matches(&format!(".{image}_")).count();
    // One image each, run once on each core.
    assert_eq!(runs("mx7_clear"), 1 + 2, "{l1}");
    assert_eq!(runs("mx7_status"), 1 + 2, "{l1}");
    assert_eq!(l1.matches("assert_zero").count(), 2, "{l1}");
    assert_eq!(l1.matches("vmax v31, v31, s14").count(), 1, "{l1}");
    assert!(l1.contains("vexp2d"), "{l1}");
}

/// Whether the checked-pack fixture's L2 and L1 stop with a NaN in `x`.
fn nan_stops() -> (bool, bool) {
    let corpus = fixtures(&["tests/fixtures/l2/fused"], Level::L2);
    let f = corpus
        .iter()
        .find(|f| f.name == "checked_pack")
        .expect("the fixture");
    let pair = lower(f, &common::card()).expect("it lowers");
    // Row 13, column 40: core 1's second trip.
    let nan = |mem: &mut [u8], binds: &ktpu_sim::l1::Binds| {
        let at = (binds["x"] + 2 * (13 * 64 + 40)) as usize;
        mem[at..at + 2].copy_from_slice(&0x7E00u16.to_le_bytes());
    };
    let l2 = common::stops(&pair, false, &nan);
    let l1 = common::stops(&pair, true, &nan);
    assert_eq!(common::stops(&pair, true, &|_, _| {}), None);
    (l2.is_some(), l1.is_some())
}

#[test]
fn a_nan_in_a_checked_pack_fails_the_kernel() {
    assert_eq!(nan_stops(), (true, true));
    // Unclassified, the lowered kernel completes on the NaN.
    let unchecked = ktpu::mutant::with("l2.vector/unchecked-pack", nan_stops);
    assert_eq!(
        unchecked.expect("a mutant this build carries"),
        (true, false)
    );
}

#[test]
fn the_lowering_keeps_every_fixture() {
    let corpus = fixtures(
        &[
            "kohakutpu/language/kohakutpu/language/examples/l2",
            "tests/fixtures/l2",
        ],
        Level::L2,
    );
    let lowering = [
        "l2.lower",
        "l2.mover",
        "l2.vector",
        "l2.packing",
        "l2.readmap",
        "l2.cluster",
        "l2.sync",
        "l2.body",
        "l2.fill",
        "l2.buffer",
        "l2.memory",
        "l2.banks",
        "l2.class",
        "l2.retile",
    ];
    stage_suite(&corpus, lower, &lowering, &known(include_str!("known.txt")));
}

#[test]
fn f32_population_records_keep_gt4_rows_across_publication() {
    let m = common::card();
    let mut t = ktpu_kernels::trace::Trace::new(
        "f32_population_records",
        ktpu_kernels::trace::Facts::of(&m),
        &m.die.target,
    );
    let source = t
        .port(
            "x",
            ktpu_graph::Role::In,
            ktpu_kernels::trace::buffer::f16(&[4, 32]).expect("source spec"),
        )
        .expect("source");
    let state_spec = ktpu_kernels::trace::buffer::spec(
        &[4, 32],
        ktpu_graph::Dtype::F32,
        vec![],
        ktpu_graph::BufSpace::Dram,
    )
    .expect("state spec");
    let state = t
        .port("state", ktpu_graph::Role::Out, state_spec.clone())
        .expect("state");
    let output = t
        .port("out", ktpu_graph::Role::Out, state_spec)
        .expect("output");
    t.par(
        1,
        ktpu_graph::UnitKind::Vc,
        ktpu_kernels::trace::control::At::Var,
        |t, _| {
            t.range(1, |t, _| {
                let x = t.load(&source.whole(), true)?;
                let fine = t.math(
                    ktpu_graph::ValueOp::Add,
                    &[x.into(), (1.0 / 32768.0).into()],
                )?;
                t.store(&state.whole(), fine.into())
            })
        },
    )
    .expect("publish");
    t.par(
        1,
        ktpu_graph::UnitKind::Vc,
        ktpu_kernels::trace::control::At::Var,
        |t, _| {
            t.range(1, |t, _| {
                let x = t.load(&source.whole(), true)?;
                let published = t.load(&state.whole(), true)?;
                let difference = t.math(ktpu_graph::ValueOp::Sub, &[published.into(), x.into()])?;
                t.store(&output.whole(), difference.into())
            })
        },
    )
    .expect("consume");
    let graph = t.graph().expect("graph");
    let text = ktpu_graph::lower(&graph).expect("L2");
    let fixture = Fixture {
        label: "F32 population publication".into(),
        source: text.text(),
        name: text.name,
    };
    let pair = lower(&fixture, &m).expect("F32 GT4 lowers");
    assert_eq!(common::compare(&pair), Ok(None));
}

#[test]
fn partial_population_reduction_ignores_centered_padding() {
    let m = common::card();
    let mut t = ktpu_kernels::trace::Trace::new(
        "partial_population",
        ktpu_kernels::trace::Facts::of(&m),
        &m.die.target,
    );
    let spec = ktpu_kernels::trace::buffer::f16(&[4, 32]).expect("rows");
    let source = t
        .port("x", ktpu_graph::Role::In, spec.clone())
        .expect("source");
    let output = t.port("out", ktpu_graph::Role::Out, spec).expect("output");
    t.par(
        1,
        ktpu_graph::UnitKind::Vc,
        ktpu_kernels::trace::control::At::Var,
        |t, _| {
            t.range(1, |t, _| {
                let x = t.load(&source.whole(), true)?;
                let centered = t.math(ktpu_graph::ValueOp::Sub, &[x.into(), 1.0.into()])?;
                let square = t.math(
                    ktpu_graph::ValueOp::Mul,
                    &[centered.into(), centered.into()],
                )?;
                let sum = t.reduce_over(
                    ktpu_graph::ValueOp::Rsum,
                    square.into(),
                    ktpu_kernels::trace::vector::Fold {
                        valid_cols: Some(17),
                        carry: false,
                    },
                )?;
                let rows = t.broadcast(sum, 32)?;
                t.store(&output.whole(), rows.into())
            })
        },
    )
    .expect("partial population");
    let graph = t.graph().expect("graph");
    let text = ktpu_graph::lower(&graph).expect("L2");
    let fixture = Fixture {
        label: "partial population reduction".into(),
        source: text.text(),
        name: text.name,
    };
    let pair = lower(&fixture, &m).expect("partial population lowers");
    assert_eq!(common::compare(&pair), Ok(None));
}
