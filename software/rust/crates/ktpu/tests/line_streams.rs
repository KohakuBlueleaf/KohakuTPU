//! The class copy/transpose tail is one bounded walk, not one stream per word.
#![cfg(feature = "stack")]

use ktpu::{compile, Cache, Compiled, Input, Stack};
use ktpu_machine::Machine;

fn class_copy(words: u64, mode: u64) -> Compiled {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let text = format!("target ktpu.v9\n\nfn copy.l2(x: f16[{words}, 16] @dram, y: f16[{words}, 16] @dram)\n    copy y <- x walk=(({words}, 32),) dwalk=(({words}, 32),) classed=1 node_loop={mode}\n");
    compile(&Stack, &Input::L2(text), Some("copy"), &m, &Cache::off()).unwrap()
}

#[test]
fn class_copies_have_no_partial_line_competitors_including_a_tail_and_a_cut() {
    for words in [1, 2, 15, 32, 17001] {
        for mode in [1, 2] {
            let c = class_copy(words, mode);
            assert!(
                !c.verdict.refused,
                "words {words}, mode {mode}: {:?}",
                c.verdict
            );
            assert!(!c.l1.contains("linear_move"));
        }
    }
}

#[cfg(feature = "mutants")]
#[test]
fn word_stream_mutant_is_refused_by_the_line_checker() {
    let c = ktpu::mutant::with("l2.mover/word-streams", || class_copy(2, 2)).unwrap();
    assert!(c.verdict.refused, "{:?}", c.verdict);
    assert!(format!("{:?}", c.verdict).contains("L1.VER.017"));
}
