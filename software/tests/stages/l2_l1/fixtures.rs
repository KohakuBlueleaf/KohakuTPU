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
