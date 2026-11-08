//! The L1 image scheduler alone: each fixture's lowered L1 on the L1 simulator
//! before and after it.

#[path = "../common.rs"]
mod common;
mod memory_dependencies;
mod timing;
mod unary_semantics;

use common::{fixtures, known, stage_suite, Fixture, Level, Pair};
use ktpu::{Backend, CardStack};
use ktpu_isa::Machine as Card;

fn schedule(f: &Fixture, card: &Card) -> Result<Pair, String> {
    let s = CardStack::default();
    let l2 = s.parse_l2(&f.source).map_err(|e| e.to_string())?;
    let placed = s.passes(&l2, &f.name, card).map_err(|e| e.to_string())?;
    let l1 = s.lower(&placed, &f.name, card).map_err(|e| e.to_string())?;
    let done = s.schedule(&l1, card).map_err(|e| e.to_string())?;
    Ok(Pair {
        name: f.name.clone(),
        before: (Level::L1, s.print_l1(&l1)),
        after: (Level::L1, s.print_l1(&done)),
    })
}

#[test]
fn the_scheduler_keeps_every_fixture() {
    let corpus = fixtures(
        &[
            "kohakutpu/language/kohakutpu/language/examples/l2",
            "tests/fixtures/l2",
        ],
        Level::L2,
    );
    let scheduling = [
        "l1.sched",
        "l1.text",
        "l1.body",
        "l1.vc",
        "l1.facts",
        "l1.expand",
        "l1.verify",
        "l1.unit",
        "l1.ruled",
    ];
    stage_suite(
        &corpus,
        schedule,
        &scheduling,
        &known(include_str!("known.txt")),
    );
}
