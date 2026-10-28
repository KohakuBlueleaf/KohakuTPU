//! The Python framework tests of the program builder and the dispatch-engine
//! lowering (`compiler/tests/test_program.py`, `test_package_engine.py`),
//! and the mover relocation (`tests/test_package_mover.py`), ported where no
//! clause test witnessed the same property; under
//! `--features mutants` each fails under a builder or lowering mutant.

#[path = "common/mod.rs"]
mod common;

use ktpu_l0::engine::{self, entries, REL, WAIT};
use ktpu_l0::format::F_POSTED;
use ktpu_l0::mover::R_HDR;
use ktpu_l0::{
    BuildOpts, Facts, IsaAddresses, Op, Package, PackageBuilder, Program, Step, Unit, Word,
};

fn f() -> Facts {
    common::v9t6()
}

fn w(v: u64) -> Word {
    Word::from_u64(v)
}

/// A DISPATCH's `(unit coord, count)`.
type Dispatched = ((u64, u64), u64);
/// An ENGINE step's `(address, value)` entries.
type Entries = Vec<(u64, u64)>;

const A: (u32, u32) = (1, 1);
const B: (u32, u32) = (2, 1);

/// `(op, count)` of every DISPATCH and AWAIT: sends between syncs, a wait,
/// a barrier.
pub fn syncs_facts() -> Vec<(Op, u64)> {
    let mut p = Program::new();
    p.send(A, vec![w(1)], vec![])
        .send(B, vec![w(2)], vec![])
        .send(A, vec![w(3), w(4)], vec![]);
    p.wait(A, None).unwrap();
    p.send(A, vec![w(5)], vec![]).barrier();
    p.build(&f(), BuildOpts::default())
        .unwrap()
        .steps
        .iter()
        .filter(|s| matches!(s.op, Op::Dispatch | Op::Await))
        .map(|s| (s.op, s.count))
        .collect()
}

#[test]
fn explicit_repetition_is_constant_size_and_counts_all_completions() {
    let mut sizes = Vec::new();
    for repeats in [1, 2, 100] {
        let mut p = Program::new();
        p.send(A, vec![w(9)], vec![]);
        p.repeat(A, vec![w(10), w(11)], repeats, vec![(0, 0, 16, 1)]);
        p.wait(A, None).unwrap();
        let b = p.build(&f(), BuildOpts::default()).unwrap();
        sizes.push((b.steps.len(), b.payloads.len()));
        let repeated = b.steps.iter().find(|s| s.op == Op::Repeat).unwrap();
        assert_eq!(repeated.count, 2);
        assert_eq!((repeated.arg >> 32) & 0xffff, repeats);
        let count: u64 = b
            .steps
            .iter()
            .filter(|s| s.op == Op::Await)
            .map(|s| s.count)
            .sum();
        assert_eq!(count, 1 + 2 * repeats);
        assert_ne!(count, 2 * repeats);
    }
    assert!(sizes.windows(2).all(|w| w[0] == w[1]));
    let mut p = Program::new();
    p.repeat(A, vec![w(0)], 0, vec![]);
    assert_eq!(
        p.build(&f(), BuildOpts::default()).unwrap_err().clause,
        "L0.STEP.041"
    );
}

#[test]
fn sends_between_syncs_are_one_dispatch_per_unit_and_waits_count_words() {
    use Op::*;
    assert_eq!(
        syncs_facts(),
        [
            (Dispatch, 3),
            (Dispatch, 1),
            (Await, 3),
            (Dispatch, 1),
            (Await, 1),
            (Await, 1)
        ]
    );
}

/// `(op, count)` of every DISPATCH and AWAIT: a wait up to a mark, with
/// words sent to the unit after the mark and another unit between.
pub fn mark_facts() -> Vec<(Op, u64)> {
    let mut p = Program::new();
    p.send(A, vec![w(1), w(2)], vec![]);
    let tok = p.mark(A);
    p.send(A, vec![w(3), w(4), w(5)], vec![])
        .send(B, vec![w(6)], vec![]);
    p.wait(A, Some(tok)).unwrap();
    p.send(B, vec![w(7)], vec![]);
    p.build(&f(), BuildOpts::default())
        .unwrap()
        .steps
        .iter()
        .filter(|s| matches!(s.op, Op::Dispatch | Op::Await))
        .map(|s| (s.op, s.count))
        .collect()
}

#[test]
fn a_wait_up_to_a_mark_awaits_only_the_words_before_it() {
    use Op::*;
    assert_eq!(
        mark_facts(),
        [
            (Dispatch, 2),
            (Dispatch, 3),
            (Dispatch, 1),
            (Await, 2),
            (Dispatch, 1),
            (Await, 3),
            (Await, 2)
        ]
    );
}

/// Each DISPATCH's `(unit coord, count)`, and each unit's words before the
/// first barrier in order: 1200 / 800 words, then 320 / 240, at the
/// machine's half credit (`unit.inst_depth / 2`).
pub fn stream_facts() -> (Vec<Dispatched>, bool) {
    let ws = |base: u64, n: u64| (0..n).map(|k| w(base + k)).collect::<Vec<_>>();
    let mut p = Program::new();
    p.send(A, ws(0, 1200), vec![])
        .send(B, ws(10_000, 800), vec![]);
    p.barrier();
    p.send(A, ws(0, 320), vec![]).send(B, ws(0, 240), vec![]);
    let pkg = p.build(&f(), BuildOpts::default()).unwrap();
    let units: Vec<(u64, u64)> = pkg.units.iter().map(|u| (u.x, u.y)).collect();
    let got = pkg
        .steps
        .iter()
        .filter(|s| s.op == Op::Dispatch)
        .map(|s| (units[s.unit as usize], s.count))
        .collect();
    let first = pkg.steps.iter().position(|s| s.op == Op::Barrier).unwrap();
    let mut words: Vec<Vec<Word>> = vec![vec![]; units.len()];
    for s in &pkg.steps[..first] {
        if s.op == Op::Dispatch {
            let (a, n) = (s.arg as usize, s.count as usize);
            words[s.unit as usize].extend_from_slice(&pkg.payloads[a..a + n]);
        }
    }
    let ua = units
        .iter()
        .position(|u| *u == (A.0 as u64, A.1 as u64))
        .unwrap();
    let ub = units
        .iter()
        .position(|u| *u == (B.0 as u64, B.1 as u64))
        .unwrap();
    let in_order = words[ua] == ws(0, 1200) && words[ub] == ws(10_000, 800);
    (got, in_order)
}

#[test]
fn a_stream_past_its_credit_interleaves_in_credit_sized_steps() {
    let (a, b) = ((A.0 as u64, A.1 as u64), (B.0 as u64, B.1 as u64));
    let half = 256;
    let mut want: Vec<Dispatched> = vec![];
    for _ in 0..3 {
        want.extend([(a, half), (b, half)]);
    }
    want.extend([(a, half), (b, 32), (a, 176)]);
    want.extend([(a, half), (b, 240), (a, 64)]);
    let (got, in_order) = stream_facts();
    assert_eq!(got, want);
    assert!(in_order, "each unit's words in order");
}

const MG: u64 = 0x4D47;
const VC: u64 = 0x5643;
/// Memory port (3, 1).
const PORT: u64 = 1 << 16 | 1 << 8 | 3;
const GO_MOVE: u64 = 1 << 16 | 5;

fn small() -> Package {
    let unit = |type_code, x, y, fetch| Unit {
        type_code,
        x,
        y,
        mesh: 0,
        credit: 512,
        fetch,
    };
    Package {
        units: vec![unit(MG, 1, 0, PORT), unit(VC, 1, 2, 0)],
        steps: vec![
            Step::new(Op::Dispatch, 0, 3, 0, 0),
            Step::new(Op::Await, 0, 3, 0, 0),
            Step::new(Op::Dispatch, 1, 1, 3, 0),
            Step::new(Op::Mover, 0, 1, 4, F_POSTED),
            Step::new(Op::Mwait, 0, 1, 0, 0),
            Step::new(Op::Ring, 1, 7, 0, 0),
            Step::new(Op::Signal, 0, 9, 0, 0),
            Step::new(Op::Barrier, 0, 0, 0, 0),
        ],
        payloads: vec![
            Word([0x11, 0, 0, 1 << 8]),
            w(0x22),
            w(0x33),
            Word([0xA, 0xB, 0xC, 0xD]),
            Word([0x08, 0xA0, 0, GO_MOVE]),
        ],
        ..Package::default()
    }
}

/// The lowered small package's step ops, its first ENGINE step's entries
/// and its last's.
pub fn small_facts() -> (Vec<Op>, Entries, Entries) {
    let low = engine::lower(&small(), &f()).unwrap();
    (
        low.steps.iter().map(|s| s.op).collect(),
        entries(&low, &low.steps[0]),
        entries(&low, &low.steps[2]),
    )
}

#[test]
fn a_small_package_lowers_to_the_engine_writes_and_waits() {
    let (ops, first, tail) = small_facts();
    assert_eq!(ops, [Op::Engine, Op::Signal, Op::Engine]);
    assert_eq!(
        first,
        [
            (0, 1 << 24 | 0x0103),
            (1, 0),
            (2, 0),
            (3, 1 << 40 | 1 << 30),
            (4 | REL, 0x60 << 8 | 3),
            (5, 1),
            (WAIT, 3),
            (0, 2 << 8 | 1),
            (1, 0xA),
            (2, 0xB),
            (3, 0xC),
            (4, 0xD),
            (5, 1),
            (9, 0xA0),
            (8, GO_MOVE),
            (WAIT, 16 << 56 | 1),
            (26, 7 << 8 | 1),
        ]
    );
    let waits = [(WAIT, 3), (WAIT, 1 << 56 | 1), (WAIT, 16 << 56 | 1)];
    assert_eq!(tail, [waits, waits].concat());
}

/// The buffer and relocation counts of a conversion between two spans, and
/// the walker bases of the package bound at 0x80_0000 and 0x90_0000
/// (`tests/test_package_mover.py`).
pub fn relocated_facts() -> (usize, usize, Vec<u64>) {
    let machine = ktpu_isa::Machine::named("multimesh_v9t6").unwrap();
    let isa = ktpu_isa::Isa::new(&machine).unwrap();
    let writes: Vec<(u64, u64)> = isa
        .mover
        .writes(&ktpu_isa::MoverOp::Quantise {
            src: 0x40_0100,
            dst: 0x50_0080,
            entries: 2,
            b: false,
        })
        .unwrap()
        .into_iter()
        .map(|(r, v)| (r as u64, v))
        .collect();
    let addresses = IsaAddresses::new(&machine).unwrap();
    let f = f();
    let mut b = PackageBuilder::new(&f, f.unit_types(), 512);
    b.spans_from(&[(0x40_0000, 0x1000), (0x50_0000, 0x1000)].into());
    b.mover(&writes, Some(&addresses), false);
    let pkg = b.build(false, false);
    let bases = pkg
        .bound(&[0x80_0000, 0x90_0000])
        .unwrap()
        .iter()
        .flat_map(|w| [0, 128].map(|half| (w.get(half, 64), w.get(half + 64, 64))))
        .filter(|&(r, _)| r == R_HDR)
        .map(|(_, v)| v >> 4 & ((1 << 40) - 1))
        .collect();
    (pkg.buffers.len(), pkg.relocs.len(), bases)
}

#[test]
fn mover_bases_are_relocated() {
    assert_eq!(relocated_facts(), (2, 2, vec![0x80_0100, 0x90_0080]));
}

/// Each test above fails under a builder or lowering mutant.
#[cfg(feature = "mutants")]
mod mutants {
    use super::*;
    use ktpu_l0::mutant::with;
    use std::panic::{catch_unwind, AssertUnwindSafe};

    fn breaks<T: PartialEq + std::fmt::Debug>(name: &'static str, facts: fn() -> T) {
        let want = facts();
        if let Ok(got) = catch_unwind(AssertUnwindSafe(|| with(name, facts))) {
            assert_ne!(got, want, "mutant {name} does not break the facts");
        }
    }

    #[test]
    fn repeat_count_mutant_fails_the_completion_witness() {
        assert!(catch_unwind(AssertUnwindSafe(|| with(
            "l0.program/repeat-count",
            explicit_repetition_is_constant_size_and_counts_all_completions
        )))
        .is_err());
    }

    #[test]
    fn syncs_break_under_await_plus_one() {
        breaks("l0.build/await_plus_one", syncs_facts);
        breaks("l0.build/await_plus_one", mark_facts);
    }

    #[test]
    fn stream_breaks_under_inst_depth() {
        breaks("l0.program/inst_depth", stream_facts);
    }

    #[test]
    fn small_breaks_under_lowering_codes() {
        for m in [
            "l0.lower/wait_code",
            "l0.lower/mover_code",
            "l0.lower/ring_code",
        ] {
            breaks(m, small_facts);
        }
    }

    #[test]
    fn relocation_breaks_under_binding_mutants() {
        for m in ["l0.bind/default_wins", "l0.bind/clobber"] {
            breaks(m, relocated_facts);
        }
    }
}
