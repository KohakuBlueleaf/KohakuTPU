//! L2 to L1: the lowering alone, each hand-written L2 fixture against its L1.

#[path = "../common.rs"]
mod common;

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
