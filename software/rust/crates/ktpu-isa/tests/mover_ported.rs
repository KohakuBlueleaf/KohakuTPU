//! `tests/test_package_mover.py`'s long conversion: a converting move past
//! the move bound is several moves, each of whole entries.

use ktpu_isa::unit::mover::*;
use ktpu_isa::{Isa, Machine, MoverOp};

const BASES: u64 = (1 << 40) - 1;

fn isa() -> Isa {
    Isa::new(&Machine::named("multimesh_v9t6").unwrap()).unwrap()
}

/// `(per, in_words, out_words, word_bytes)` from the machine: the f16 entries
/// one move carries, the words of an f16 and of an mx7 entry, a word's bytes.
fn shape() -> (u64, u64, u64, u64) {
    let m = isa().mover;
    let (inw, outw) = (m.f16_entry_words as u64, m.mx7_entry_words as u64);
    (m.move_words / inw, inw, outw, m.word_bytes)
}

/// The GO count, source and destination bases, and the walkers' counts of a
/// conversion of `entries` entries.
pub fn long_facts(entries: u64) -> (usize, Vec<u64>, Vec<u64>, Vec<u64>) {
    let writes = isa()
        .mover
        .writes(&MoverOp::Quantise {
            src: 0x40_0000,
            dst: 0x400_0000,
            entries: entries as i64,
            b: false,
        })
        .unwrap();
    let gos = writes
        .iter()
        .filter(|(r, v)| *r == R_CTRL && v & GO != 0)
        .count();
    let hdrs: Vec<u64> = writes
        .iter()
        .filter(|(r, _)| *r == R_HDR)
        .map(|(_, v)| v >> 4 & BASES)
        .collect();
    let counts = writes
        .iter()
        .filter(|(r, v)| *r == R_DIM && v & 1 == 0)
        .map(|(_, v)| v >> 4 & 0xFFFF)
        .collect();
    (
        gos,
        hdrs.iter().step_by(2).copied().collect(),
        hdrs.iter().skip(1).step_by(2).copied().collect(),
        counts,
    )
}

#[test]
fn a_long_conversion_is_several_whole_entry_moves() {
    let (per, inw, outw, wb) = shape();
    let (gos, src, dst, counts) = long_facts(3 * per + 5);
    assert_eq!(gos, 4);
    let at = |base: u64, words: u64| -> Vec<u64> {
        (0..4).map(|k| base + k * per * words * wb).collect()
    };
    assert_eq!(src, at(0x40_0000, inw));
    assert_eq!(dst, at(0x400_0000, outw));
    assert_eq!(counts, [per * inw, per * inw, per * inw, 5 * inw]);
}

#[cfg(feature = "mutants")]
#[test]
fn a_long_conversion_breaks_under_the_move_bound() {
    let entries = 3 * shape().0 + 5;
    assert_ne!(
        ktpu_isa::mutant::with("isa.mover/limits", || long_facts(entries)),
        long_facts(entries)
    );
}
