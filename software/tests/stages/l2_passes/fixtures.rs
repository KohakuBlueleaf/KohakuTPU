//! The L2 passes alone: each fixture on the L2 simulator before and after them.

#[path = "../common.rs"]
mod common;
mod select_semantics;

use common::{card, fixtures, known, software, stage_suite, Fixture, Level, Pair};
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

/// Three instances over two cores in each of two phases: 3 + 3, never 4 + 2.
#[test]
fn a_remainder_rotates_over_the_units() {
    let path = software().join("tests/fixtures/l2/vector/rotate.ktpu");
    let source = std::fs::read_to_string(path).expect("the fixture");
    let s = CardStack::default();
    let l2 = s.parse_l2(&source).expect("legal L2");
    let done = s.passes(&l2, "tiny_rotate", &card()).expect("the passes");
    let text = s.print_l2(&done);
    assert!(text.contains("on vc[c % 2]"), "{text}");
    assert!(text.contains("on vc[(c+1) % 2]"), "{text}");
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

#[test]
fn shared_fills_follow_each_instances_execution_scope() {
    let source = r#"target ktpu.v9
fn partial.l2(a: mx7a[4, 64] @dram(gm=1, nk=2), b: mx7b[32, 64] @dram(gn=8, nk=2), out: f16[8, 32] @dram(tile=1x8))
    par n in 0..1 on node[n]
        par u in 0..2 on mg[u]
            for i in 0..(2-u)
                acc = zero : f32[4, 32] @acc
                av = fill a[0:4, 0:64] : mx7a[4, 64] @A
                bv = fill b[0:32, 0:64] : mx7b[32, 64] @B
                acc = gemm av, bv, acc : f32[4, 32] @acc
                drain acc -> out[u*4:+4, 0:32]
"#;
    for (source, uniform) in [
        (source.to_string(), false),
        (source.replace("(2-u)", "2"), true),
    ] {
        let program =
            ktpu_l2::Program::parse(&source, "partial", Some("partial")).expect("partial wave");
        let target = ktpu_l2::target::Target::of(&card());
        let shared = ktpu_l2::passes::share::share(&program, &target).expect("share inference");
        assert_eq!(shared.text().contains("share=cross"), uniform);
        // Execute the sole node's schedule on the single-node L1 simulator.
        let (params, stmts) = ktpu_l2::passes::body_of(&shared).expect("share body");
        let local = ktpu_l2::passes::write(&shared, &params, &stmts[0].body).expect("local share");
        let l1 = ktpu_l2::lower::lower(&local, &target, &ktpu_l2::Search::default())
            .expect("every shared stream has the same fills");
        let pair = Pair {
            name: "partial".into(),
            before: (Level::L2, source),
            after: (Level::L1, l1),
        };
        assert_eq!(
            common::compare(&pair),
            Ok(None),
            "each live tile is computed"
        );
    }
}
