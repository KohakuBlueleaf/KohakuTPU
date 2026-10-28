//! docs/spec/stack/l0/dispatch-engine.md (the lowering's clauses)

use super::*;
use ktpu_l0::engine::{self, entries, il_code, mover_code, IL_RING, NONE, REL, WAIT};
use ktpu_l0::{replay, Op, Step, WordOps};

fn s(op: Op, unit: u64, count: u64, arg: u64) -> Step {
    Step::new(op, unit, count, arg, 0)
}

fn all_entries(p: &ktpu_l0::Package) -> Vec<(u64, u64)> {
    let low = engine::lower(p, &f()).unwrap();
    low.steps
        .iter()
        .filter(|s| s.op == Op::Engine)
        .flat_map(|s| entries(&low, s))
        .collect()
}

#[test]
fn l0_de_023_stream_packs_entries_three_a_payload_after_the_packages_own() {
    use engine::Entry::{Put, Wait};
    let p = pkg(vec![mg(2, 3)], vec![s(Op::Dispatch, 0, 1, 0)], vec![7]);
    let writes: Vec<_> = (0..5).map(|k| Put(engine::ARG0 + k % 4, k)).collect();
    let mut all = writes.clone();
    all.extend([Wait(0, 0), Wait(0, 1)]);
    let out = engine::stream(&p, &f(), &all);
    assert_eq!(out.steps.len(), 1);
    let st = out.steps[0];
    assert_eq!((st.op, st.count, st.arg), (Op::Engine, 6, 1));
    assert_eq!(out.payloads.len(), 1 + 2);
    assert_eq!(out.payloads[0], p.payloads[0]);
    let got = entries(&out, &st);
    let want: Vec<(u64, u64)> = (0..5)
        .map(|k| (engine::ARG0 + k % 4, k))
        .chain([(WAIT, 1)])
        .collect();
    assert_eq!(got, want);
    // A stream that ends inside a payload pads it with NONE.
    let one = engine::stream(&p, &f(), &writes[..1]);
    assert_eq!(one.payloads[1].get(8, 8), NONE);
}

#[test]
fn l0_de_002_wait_code_is_32() {
    assert_eq!(WAIT, 32);
    let e = all_entries(&pkg(vec![], vec![s(Op::Mwait, 0, 1, 0)], vec![]));
    assert_eq!(e[0].0, 32);
}

#[test]
fn l0_de_004_mailbox_and_mover_codes() {
    assert_eq!(mover_code(0x00), 8);
    assert_eq!(mover_code(0x78), 23);
    let e = all_entries(&pkg(
        vec![mg(2, 3)],
        vec![s(Op::Dispatch, 0, 1, 0)],
        vec![7],
    ));
    let codes: Vec<u64> = e.iter().map(|x| x.0).collect();
    assert_eq!(&codes[..6], [0, 1, 2, 3, 4, 5]);
    assert_eq!(e[0].1, 3 << 8 | 2);
}

#[test]
fn l0_de_005_ring_is_code_26() {
    assert_eq!(il_code(IL_RING), 26);
}

#[test]
fn l0_de_006_wait_value_src_and_12_bit_want() {
    let f = f();
    assert_eq!(f.ctr_mask(), 0xFFF);
    let e = all_entries(&pkg(
        vec![mg(1, 1)],
        vec![s(Op::Await, 0, 0x1003, 0)],
        vec![],
    ));
    assert_eq!(e[0], (WAIT, 0x003));
    let e = all_entries(&pkg(vec![], vec![s(Op::Mwait, 0, 7, 0)], vec![]));
    assert_eq!(e[0], (WAIT, 16 << 56 | 7));
}

#[test]
fn l0_de_007_in_flight_bounded_by_credit() {
    let p = pkg(
        vec![with_port(mg(1, 1))],
        vec![s(Op::Dispatch, 0, 3000, 0)],
        vec![0; 3000],
    );
    // Replay with every WAIT met exactly, its 12-bit want unwrapped against
    // the words sent so far.
    let mut sent: i64 = 0;
    let mut done: i64 = 0;
    let mut most = 0;
    for (code, v) in all_entries(&p) {
        if code == WAIT && v >> 56 == 0 {
            let mut want = (v & 0xFFF) as i64;
            let base = sent - 2048;
            want += (base - want + 4095).div_euclid(4096) * 4096;
            done = done.max(want);
        } else if code == 4 | REL {
            sent += (v & 0xFF) as i64;
            most = most.max(sent - done);
        }
    }
    assert!(most <= 512, "{most} in flight");
    assert!(most > 255);
}

#[test]
fn l0_de_021_mailbox_writes_deduplicated_per_engine_step() {
    let p = pkg(
        vec![with_port(mg(1, 1))],
        vec![
            s(Op::Dispatch, 0, 1, 0),
            s(Op::Dispatch, 0, 1, 1),
            s(Op::Signal, 0, 0, 0),
            s(Op::Dispatch, 0, 1, 0),
        ],
        vec![1, 2],
    );
    let low = engine::lower(&p, &f()).unwrap();
    let first = entries(&low, &low.steps[0]);
    let dst = first.iter().filter(|x| x.0 == 0).count();
    assert_eq!(dst, 1);
    let third = entries(&low, &low.steps[2]);
    assert_eq!(third[0].0, 0);
}

#[test]
fn l0_de_022_same_units_header_payloads_appended() {
    let mut p = pkg(
        vec![with_port(mg(1, 1))],
        vec![s(Op::Dispatch, 0, 2, 0)],
        vec![1, 2],
    );
    p.signature = 5;
    p.ack_reserve = 1;
    p.checksum = true;
    let low = engine::lower(&p, &f()).unwrap();
    assert_eq!(low.units, p.units);
    assert_eq!((low.signature, low.ack_reserve, low.checksum), (5, 1, true));
    assert_eq!(&low.payloads[..2], &p.payloads[..]);
    assert_eq!(low.steps[0].arg, 2);
}

#[test]
fn l0_de_023_packing_three_codes_then_values() {
    let p = pkg(
        vec![],
        vec![s(Op::Mwait, 0, 1, 0), s(Op::Mwait, 0, 2, 0)],
        vec![],
    );
    let low = engine::lower(&p, &f()).unwrap();
    assert_eq!(low.steps[0].count, 2);
    assert_eq!(
        low.payloads[0],
        Word([32 | 32 << 8 | NONE << 16, 16 << 56 | 1, 16 << 56 | 2, 0])
    );
}

#[test]
fn l0_de_024_rel_marks_payload_offsets() {
    let p = pkg(
        vec![with_port(mg(1, 1))],
        vec![s(Op::Dispatch, 0, 1, 3)],
        vec![0; 4],
    );
    let e = all_entries(&p);
    assert!(e.contains(&(4 | REL, (3 * 32) << 24 | 0x60 << 8 | 1)));
}

#[test]
fn l0_de_028_fetch_run_entries() {
    let p = pkg(
        vec![with_port(mg(1, 1))],
        vec![s(Op::Dispatch, 0, 300, 0)],
        vec![0; 300],
    );
    let e = all_entries(&p);
    let codes: Vec<u64> = e.iter().map(|x| x.0).collect();
    assert_eq!(codes, [0, 1, 2, 3, 0x44, 5, 0x44, 5, 32]);
    assert_eq!(e[6].1, (255 * 32) << 24 | 0x60 << 8 | 45);
}

#[test]
fn l0_de_029_mailbox_words_without_port() {
    let p = pkg(vec![mg(1, 2)], vec![s(Op::Dispatch, 0, 1, 0)], vec![]);
    let mut p = p;
    p.payloads.push(Word([1, 2, 3, 4]));
    let e = all_entries(&p);
    assert_eq!(
        &e[..6],
        [(0, 2 << 8 | 1), (1, 1), (2, 2), (3, 3), (4, 4), (5, 1)]
    );
}

#[test]
fn l0_de_030_awaits_barrier_mover_mwait() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    b.dispatch(u, &[Word::from_u64(1), Word::from_u64(2)], None)
        .unwrap();
    b.await_(u, 1);
    b.barrier();
    b.mover(&[(0, 1 << 16)], None, false);
    b.mwait(1);
    let e = all_entries(&b.build(true, false));
    let waits: Vec<_> = e.iter().filter(|x| x.0 == WAIT).copied().collect();
    assert_eq!(
        waits,
        [
            (WAIT, 1),
            (WAIT, 2),
            (WAIT, 16 << 56 | 1),
            (WAIT, 16 << 56 | 1),
            (WAIT, 2),
            (WAIT, 16 << 56 | 1)
        ]
    );
}

#[test]
fn l0_de_031_ring_entry() {
    let p = pkg(vec![], vec![s(Op::Ring, 1, 3, 2 | 1 << 16)], vec![]);
    assert_eq!(all_entries(&p), [(26, 1 | 3 << 8 | 2 << 16 | 1 << 32)]);
}

#[test]
fn l0_de_032_fetch_absolute_repeat_written_out() {
    let p = pkg(
        vec![with_port(mg(1, 1))],
        vec![s(Op::Fetch, 0, 2, 0x5000)],
        vec![],
    );
    assert!(all_entries(&p).contains(&(4, 0x5000 << 24 | 0x60 << 8 | 2)));
}

#[test]
fn l0_de_033_jump_or_patch_returned_unchanged() {
    for op in [Op::Jump, Op::Patch] {
        let p = pkg(
            vec![mg(1, 1)],
            vec![s(Op::Dispatch, 0, 1, 0), s(op, 0, 0, 0)],
            vec![1],
        );
        assert_eq!(engine::lower(&p, &f()).unwrap(), p);
    }
}

#[test]
fn l0_de_039_lower_refusals() {
    let units = (0..17).map(|x| mg(x, 0)).collect();
    let p = pkg(units, vec![], vec![]);
    assert_eq!(engine::lower(&p, &f()).unwrap_err().clause, "L0.DE.039");
    let units: Vec<_> = (0..16).map(|x| mg(x, 0)).collect();
    assert!(engine::lower(&pkg(units, vec![], vec![]), &f()).is_ok());
}

/// A package touching every path the replay reads: long fetched and
/// mailbox DISPATCHes, a REPEAT, a FETCH, an AWAIT, an MWAIT and a BARRIER.
fn every_path() -> ktpu_l0::Package {
    let repeat = 1 | 3 << 32 | 1 << 48;
    pkg(
        vec![with_port(mg(1, 1)), mg(2, 1)],
        vec![
            s(Op::Dispatch, 0, 1300, 0),
            s(Op::Dispatch, 1, 600, 1300),
            s(Op::Await, 0, 700, 0),
            s(Op::Repeat, 0, 2, repeat),
            s(Op::Fetch, 0, 300, 0x8000),
            s(Op::Mwait, 0, 1, 0),
            s(Op::Barrier, 0, 0, 0),
        ],
        (0..1902).collect(),
    )
}

fn replayed(p: &ktpu_l0::Package) -> ktpu_l0::Result<()> {
    let low = engine::lower(p, &f())?;
    replay::check(p, &low, &f())
}

#[test]
fn l0_de_040_a_lowering_passes_its_replay() {
    replayed(&every_path()).unwrap();
}

#[test]
fn l0_de_040_a_corrupted_entry_is_refused() {
    let p = every_path();
    let low = engine::lower(&p, &f()).unwrap();
    let step = *low.steps.iter().find(|s| s.op == Op::Engine).unwrap();
    // The first entry of `code`: its payload index and slot.
    let first = |code: u64| {
        (0..step.count)
            .map(|k| ((step.arg + k / 3) as usize, (k % 3) as u32))
            .find(|&(i, j)| low.payloads[i].get(8 * j, 8) == code)
            .unwrap()
    };
    // ARG3 without REL; DST untyped; a WAIT on a source past the units.
    let (i, j) = first(REL | 4);
    let (d, e) = first(0);
    let (w, v) = first(WAIT);
    for (at, bit, width, value) in [
        (i, 8 * j, 8, 4),
        (d, 64 * (e + 1) + 24, 1, 0),
        (w, 64 * (v + 1) + 56, 5, 9),
    ] {
        let mut bad = low.clone();
        bad.payloads[at].set(bit, width, value);
        assert_ne!(bad, low);
        let got = replay::check(&p, &bad, &f()).unwrap_err();
        assert_eq!(got.clause, "L0.DE.040", "{got}");
    }
}

#[cfg(feature = "mutants")]
#[test]
fn l0_de_040_refuses_each_lowering_mutant_and_off_lets_them_by() {
    for m in [
        "l0.lower/no_rel",
        "l0.lower/untyped_dst",
        "l0.lower/fetch_depth",
        "l0.lower/run_cap",
        "l0.lower/src_mover",
    ] {
        let got = ktpu_l0::mutant::with(m, || replayed(&every_path()));
        assert_eq!(got.map_err(|e| e.clause), Err("L0.DE.040"), "{m}");
    }
    let p = every_path();
    let mut bad = engine::lower(&p, &f()).unwrap();
    let step = *bad.steps.iter().find(|s| s.op == Op::Engine).unwrap();
    bad.payloads[step.arg as usize].set(0, 8, 0x3F);
    assert!(replay::check(&p, &bad, &f()).is_err());
    let off = ktpu_l0::mutant::with("l0.check/off", || replay::check(&p, &bad, &f()));
    assert!(off.is_ok());
}
