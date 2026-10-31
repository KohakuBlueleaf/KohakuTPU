//! The L2 passes alone: each fixture on the L2 simulator before and after them.

#[path = "../common.rs"]
mod common;

use common::{fixtures, known, stage_suite, Fixture, Level, Pair};
use ktpu::{Backend, CardStack};
use ktpu_isa::Machine as Card;

fn passes(f: &Fixture, card: &Card) -> Result<Pair, String> {
    let s = CardStack::default();
    let l2 = s.parse_l2(&f.source).map_err(|e| e.to_string())?;
    let done = s.passes(&l2, &f.name, card).map_err(|e| e.to_string())?;
    Ok(Pair {
        name: f.name.clone(),
        before: (Level::L2, f.source.clone()),
        after: (Level::L2, s.print_l2(&done)),
    })
}

#[test]
fn the_passes_keep_every_fixture() {
    let corpus = fixtures(
        &[
            "kohakutpu/language/kohakutpu/language/examples/l2",
            "tests/fixtures/l2",
        ],
        Level::L2,
    );
    stage_suite(
        &corpus,
        passes,
        &["l2.passes", "l2.zero_sign", "l2.pipeline"],
        &known(include_str!("known.txt")),
    );
}
