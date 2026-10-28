//! docs/spec/stack/l0/steps.md

use super::*;
use ktpu_l0::engine::{self, entries, WAIT};
use ktpu_l0::format::{
    patch_arg, F_ACK, F_CONSUME, F_CTRL, F_IMM, F_INDEXED, F_MEM, F_POSTED, F_REG,
};
use ktpu_l0::{Alu, BuildOpts, Cond, Op, Patch, Program, Step};

fn w(v: u64) -> Word {
    Word::from_u64(v)
}

fn ws(n: u64) -> Vec<Word> {
    (1..=n).map(w).collect()
}

fn built(p: &Program) -> Vec<Step> {
    p.build(&f(), BuildOpts::default()).unwrap().steps
}

fn s(op: Op, unit: u64, count: u64, arg: u64, flags: u8) -> Step {
    Step::new(op, unit, count, arg, flags)
}

/// All entries of a lowered package, in order.
fn lowered_entries(p: &ktpu_l0::Package) -> Vec<(u64, u64)> {
    let low = engine::lower(p, &f()).unwrap();
    low.steps
        .iter()
        .filter(|s| s.op == Op::Engine)
        .flat_map(|s| entries(&low, s))
        .collect()
}

fn wait(src: u64, want: u64) -> (u64, u64) {
    (WAIT, src << 56 | want)
}

#[test]
fn l0_step_002_end_is_a_barrier() {
    let p = pkg(
        vec![mg(1, 1)],
        vec![s(Op::Dispatch, 0, 2, 0, 0)],
        vec![1, 2],
    );
    let e = lowered_entries(&p);
    assert_eq!(e.last(), Some(&wait(0, 2)));
}

#[test]
fn l0_step_007_credit_waits_before_words_past_it() {
    let mut u = mg(1, 1);
    u.credit = 2;
    let p = pkg(vec![u], vec![s(Op::Dispatch, 0, 3, 0, 0)], vec![1, 2, 3]);
    let e = lowered_entries(&p);
    let waits: Vec<_> = e.iter().filter(|x| x.0 == WAIT).collect();
    assert_eq!(waits, [&wait(0, 1), &wait(0, 3)]);
    let third_go = e
        .iter()
        .enumerate()
        .filter(|x| x.1 .0 == 5)
        .nth(2)
        .unwrap()
        .0;
    let w1 = e.iter().position(|x| *x == wait(0, 1)).unwrap();
    assert!(w1 < third_go);
}

#[test]
fn l0_step_011_ack_reserve_keeps_the_most_asked() {
    let mut b = builder();
    b.reserve_acks(3);
    b.reserve_acks(1);
    assert_eq!(b.build(true, false).ack_reserve, 3);
}

#[test]
fn l0_step_013_end_stops_the_lowering() {
    let p = pkg(
        vec![mg(1, 1)],
        vec![s(Op::End, 0, 0, 0, 0), s(Op::Dispatch, 0, 1, 0, 0)],
        vec![1],
    );
    assert!(lowered_entries(&p).is_empty());
}

#[test]
fn l0_step_014_dispatch_step() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    b.payload(w(9), None, "");
    b.dispatch(u, &[w(7), w(8)], None).unwrap();
    b.dispatch(u, &[], None).unwrap();
    assert_eq!(b.steps, [s(Op::Dispatch, 0, 2, 1, 0)]);
}

#[test]
fn l0_step_015_pieces_of_half_credit_interleaved() {
    let mut p = Program::new();
    p.send((1, 1), ws(600), vec![])
        .send((2, 1), ws(300), vec![])
        .barrier();
    let got: Vec<(u64, u64, u64)> = built(&p)
        .iter()
        .filter(|s| s.op == Op::Dispatch)
        .map(|s| (s.unit, s.count, s.arg))
        .collect();
    assert_eq!(
        got,
        [
            (0, 256, 0),
            (1, 256, 256),
            (0, 256, 512),
            (1, 44, 768),
            (0, 88, 812)
        ]
    );
}

#[test]
fn l0_step_020_await_is_cumulative() {
    let p = pkg(
        vec![mg(1, 1)],
        vec![
            s(Op::Dispatch, 0, 3, 0, 0),
            s(Op::Await, 0, 1, 0, 0),
            s(Op::Await, 0, 2, 0, 0),
        ],
        vec![1, 2, 3],
    );
    let waits: Vec<_> = lowered_entries(&p)
        .into_iter()
        .filter(|e| e.0 == WAIT)
        .collect();
    assert_eq!(waits, [wait(0, 1), wait(0, 3), wait(0, 3)]);
}

#[test]
fn l0_step_021_wait_upto_mark() {
    let mut p = Program::new();
    p.send((1, 1), ws(5), vec![]);
    let t = p.mark((1, 1));
    p.send((1, 1), ws(4), vec![]);
    p.wait((1, 1), Some(t)).unwrap();
    p.wait((1, 1), Some(t)).unwrap();
    let st = built(&p);
    assert_eq!(st[1].op, Op::Dispatch);
    assert_eq!(st[2], s(Op::Await, 0, 5, 0, 0));
    assert_eq!(st[3], s(Op::Await, 0, 4, 0, 0));
    assert!(p.clone().wait((2, 1), Some(t)).is_err());
}

#[test]
fn l0_step_062_wait_all_sent() {
    let mut p = Program::new();
    p.send((1, 1), ws(5), vec![]).wait((1, 1), None).unwrap();
    assert_eq!(built(&p)[1], s(Op::Await, 0, 5, 0, 0));
}

#[test]
fn l0_step_023_barrier_awaits_each_unit_never_twice() {
    let mut p = Program::new();
    p.send((2, 1), ws(2), vec![])
        .send((1, 1), ws(3), vec![])
        .barrier()
        .barrier();
    let st = built(&p);
    let tail: Vec<_> = st.iter().skip(2).copied().collect();
    assert_eq!(
        tail,
        [
            s(Op::Await, 0, 2, 0, 0),
            s(Op::Await, 1, 3, 0, 0),
            s(Op::Barrier, 0, 0, 0, 0)
        ]
    );
}

#[test]
fn l0_step_024_mover_pairs_two_a_payload() {
    let mut b = builder();
    b.mover(&[(8, 0x1234), (0x78, 5), (0x10, 9)], None, false);
    assert_eq!(b.payloads[0], Word([8, 0x1234, 0x78, 5]));
    assert_eq!(b.payloads[1], Word([0x10, 9, u64::MAX, 0]));
    assert_eq!(b.steps, [s(Op::Mover, 0, 2, 0, 0)]);
}

#[test]
fn l0_step_063_skipped_pair_pads_and_is_not_written() {
    let mut b = builder();
    b.mover(&[(8, 1)], None, false);
    let e = lowered_entries(&b.build(true, false));
    assert_eq!(e, [(9, 1)]);
}

#[test]
fn l0_step_025_go_counted() {
    let mut b = builder();
    b.mover(&[(0, 1 << 16), (0, 0), (0, 1 << 16 | 3)], None, false);
    let e = lowered_entries(&b.build(true, false));
    assert_eq!(e.last(), Some(&wait(16, 2)));
}

#[test]
fn l0_step_026_posted_does_not_wait() {
    let mut b = builder();
    b.mover(&[(0, 1 << 16)], None, true);
    assert_eq!(b.steps[0].flags, F_POSTED);
    b.mwait(1);
    let e = lowered_entries(&b.build(true, false));
    assert_eq!(e, [(8, 1 << 16), wait(16, 1), wait(16, 1)]);
}

#[test]
fn l0_step_027_mwait_absolute() {
    let p = pkg(vec![], vec![s(Op::Mwait, 0, 5, 0, 0)], vec![]);
    assert_eq!(lowered_entries(&p)[0], wait(16, 5));
}

#[test]
fn l0_step_028_move_post_wait_moves() {
    let mut p = Program::new();
    p.move_(vec![(0, 1 << 16)], vec![]);
    let t = p.post(vec![(0, 1 << 16), (0, 1 << 16)], vec![]);
    p.wait_moves(t).unwrap();
    let ops: Vec<(Op, u64, u8)> = built(&p).iter().map(|s| (s.op, s.count, s.flags)).collect();
    assert_eq!(
        ops,
        [
            (Op::Mover, 1, 0),
            (Op::Barrier, 0, 0),
            (Op::Mover, 1, F_POSTED),
            (Op::Mwait, 3, 0),
            (Op::Barrier, 0, 0)
        ]
    );
    assert!(Program::new().wait_moves(0).is_err());
}

#[test]
fn l0_step_029_bad_mover_register_refused() {
    for reg in [0x80, 0x0C] {
        let mut b = builder();
        b.mover(&[(reg, 1)], None, false);
        let err = engine::lower(&b.build(true, false), &f()).unwrap_err();
        assert_eq!(err.clause, "L0.STEP.029");
    }
}

#[test]
fn l0_step_031_ring_step_and_value() {
    let mut b = builder();
    b.ring(2, 9, 0x1234, true).unwrap();
    assert_eq!(b.steps[0], s(Op::Ring, 2, 9, 0x1_1234, 0));
    assert_eq!(
        engine::ring_value(&b.steps[0]),
        2 | 9 << 8 | 0x1234 << 16 | 1 << 32
    );
}

#[test]
fn l0_step_033_ring_refusals() {
    let mut b = builder();
    assert_eq!(b.ring(0, 256, 1, true).unwrap_err().clause, "L0.STEP.033");
    assert!(b.ring(0, 0, 0, true).is_err());
    assert!(b.ring(0, 0, 0x1_0000, true).is_err());
    assert!(b.ring(0, 255, 0xFFFF, false).is_ok());
}

#[test]
fn l0_step_034_wait_bell_step() {
    let mut b = builder();
    b.wait_bell(3, 2);
    assert_eq!(b.steps[0], s(Op::WaitBell, 3, 2, 0, 0));
}

#[test]
fn l0_step_035_wait_slot_step() {
    let mut b = builder();
    b.wait_slot(15, 4, true).unwrap();
    assert_eq!(b.steps[0], s(Op::WaitSlot, 15, 4, 0, F_CONSUME));
    assert_eq!(b.wait_slot(16, 1, false).unwrap_err().clause, "L0.STEP.066");
}

#[test]
fn l0_step_037_signal_and_038_settle() {
    let mut b = builder();
    b.signal(5, 6);
    b.settle(100);
    assert_eq!(
        b.steps,
        [s(Op::Signal, 0, 5, 6, 0), s(Op::Settle, 0, 100, 0, 0)]
    );
}

#[test]
fn l0_step_039_repeat_layout() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    b.repeat(
        u,
        &[w(5), w(0x60)],
        4,
        &[(1, 0, 8, 2), (0, 64, 16, 3), (1, 128, 32, 7)],
        None,
    )
    .unwrap();
    assert_eq!(b.steps[0], s(Op::Repeat, 0, 2, 4 << 32 | 3 << 48, 0));
    // Increments sorted: (0,64,16,3), (1,0,8,2) | (1,128,32,7).
    assert_eq!(
        b.payloads[2],
        Word([64 << 32 | 16 << 40, 3, 1 | 8 << 40, 2])
    );
    assert_eq!(b.payloads[3], Word([1 | 128 << 32 | 32 << 40, 7, 0, 0]));
}

#[test]
fn l0_step_041_repeat_refusals() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    let r = |b: &mut PackageBuilder, t: &[Word], n, i: &[(usize, u32, u32, u64)]| {
        b.repeat(u, t, n, i, None).unwrap_err().clause
    };
    assert_eq!(r(&mut b, &[], 1, &[]), "L0.STEP.041");
    assert_eq!(r(&mut b, &[w(1)], 0, &[]), "L0.STEP.041");
    assert_eq!(r(&mut b, &[w(1)], 0x1_0000, &[]), "L0.STEP.041");
    assert_eq!(r(&mut b, &[w(1)], 2, &[(0, 250, 8, 1)]), "L0.STEP.041");
    assert_eq!(r(&mut b, &[w(0xFE)], 3, &[(0, 0, 8, 1)]), "L0.STEP.041");
    assert!(b.repeat(u, &[w(0xFD)], 3, &[(0, 0, 8, 1)], None).is_ok());
}

#[test]
fn l0_step_042_lowering_writes_repetitions_out() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    b.repeat(u, &[w(0x10)], 3, &[(0, 0, 8, 5)], None).unwrap();
    let e = lowered_entries(&b.build(true, false));
    let arg0: Vec<u64> = e.iter().filter(|x| x.0 == 1).map(|x| x.1).collect();
    assert_eq!(arg0, [0x10, 0x15, 0x1A]);
}

#[test]
fn l0_step_043_engine_three_entries_a_payload() {
    let steps = (1..=4).map(|n| s(Op::Mwait, 0, n, 0, 0)).collect();
    let low = engine::lower(&pkg(vec![], steps, vec![]), &f()).unwrap();
    assert_eq!(low.steps, [s(Op::Engine, 0, 4, 0, 0)]);
    assert_eq!(low.payloads.len(), 2);
    assert_eq!(low.payloads[1].get(0, 24), 32 | 0xFF << 8 | 0xFF << 16);
}

#[test]
fn l0_step_044_fetch_request_words() {
    let p = pkg(
        vec![with_port(mg(3, 1))],
        vec![s(Op::Dispatch, 0, 2, 0, 0)],
        vec![1, 2],
    );
    let e = lowered_entries(&p);
    assert_eq!(
        &e[..6],
        [
            (0, 1 << 24 | 2 << 8),
            (1, 0),
            (2, 0),
            (3, (1 << 4 | 3) << 40 | 1 << 30),
            (4 | 0x40, 0x60 << 8 | 2),
            (5, 1)
        ]
    );
}

#[test]
fn l0_step_045_fetch_runs_capped_at_255() {
    let p = pkg(
        vec![with_port(mg(1, 1))],
        vec![s(Op::Dispatch, 0, 600, 0, 0)],
        vec![0; 600],
    );
    let runs: Vec<u64> = lowered_entries(&p)
        .iter()
        .filter(|x| x.0 == 0x44)
        .map(|x| x.1)
        .collect();
    assert_eq!(
        runs,
        [
            0x60 << 8 | 255,
            (255 * 32) << 24 | 0x60 << 8 | 255,
            (510 * 32) << 24 | 0x60 << 8 | 90
        ]
    );
    let waits: Vec<_> = lowered_entries(&p)
        .into_iter()
        .filter(|x| x.0 == WAIT)
        .collect();
    assert_eq!(waits, [wait(0, 510 + 90 - 512), wait(0, 600)]);
}

#[test]
fn l0_step_046_fetch_step_and_portless_refusal() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    assert_eq!(
        b.fetch_from(u, 0x9000, 4).unwrap_err().clause,
        "L0.STEP.046"
    );
    let mut b = builder();
    b.fetch = Some((0, 2));
    let u = b.unit((1, 1), None, None).unwrap();
    b.fetch_from(u, 0x9000, 4).unwrap();
    b.fetch_from(u, 0x9000, 0).unwrap();
    assert_eq!(b.steps, [s(Op::Fetch, 0, 4, 0x9000, 0)]);
    let e = lowered_entries(&b.build(true, false));
    assert!(e.contains(&(4, 0x9000 << 24 | 0x60 << 8 | 4)));
}

#[test]
fn l0_step_047_store_pairs() {
    let mut b = builder();
    b.store(&[(0x100, 1), (0x108, 2), (0x110, 3)], true, false);
    assert_eq!(b.payloads[0], Word([0x100, 1, 0x108, 2]));
    assert_eq!(b.payloads[1], Word([0x110, 3, u64::MAX, 0]));
    assert_eq!(b.steps[0], s(Op::Store, 0, 2, 0, F_REG));
}

#[test]
fn l0_step_049_load_step() {
    let mut b = builder();
    b.load(3, 0x38, None, true, false);
    b.load(4, 0x6000, Some(2), false, false);
    assert_eq!(b.steps[0], s(Op::Load, 3, 0, 0, F_CTRL));
    assert_eq!(b.steps[1], s(Op::Load, 4, 2, 1, F_INDEXED));
    assert_eq!(b.payloads[1], w(0x6000));
}

#[test]
fn l0_step_050_ctrl_pairs() {
    let mut b = builder();
    b.ctrl(&[(0x1D0, 3)], false);
    assert_eq!(b.payloads[0], Word([0x1D0, 3, u64::MAX, 0]));
    assert_eq!(b.steps[0], s(Op::Ctrl, 0, 1, 0, 0));
}

#[test]
fn l0_step_051_alu_encoding() {
    let mut b = builder();
    b.alu(2, Alu::Mul, 1, 7, true).unwrap();
    b.alu(2, Alu::Add, 2, 3, false).unwrap();
    b.alu(1, Alu::Mov, 0, (-1i64) as u64, true).unwrap();
    assert_eq!(b.steps[0], s(Op::Alu, 2, 3 | 1 << 8, 7, F_IMM));
    assert_eq!(b.steps[1], s(Op::Alu, 2, 1 | 2 << 8 | 3 << 12, 0, 0));
    assert_eq!(b.steps[2].arg, u64::MAX);
    assert_eq!(
        b.alu(16, Alu::Add, 0, 0, true).unwrap_err().clause,
        "L0.STEP.070"
    );
    assert!(b.alu(0, Alu::Add, 0, 16, false).is_err());
}

#[test]
fn l0_step_052_jump_encoding_and_labels() {
    let mut b = builder();
    let at = b.jump(0, Cond::Ltu, 2, 1, false);
    b.retarget(at, 5);
    assert_eq!(b.steps[0], s(Op::Jump, 5, 3 | 2 << 8 | 1 << 12, 0, 0));
    let mut p = Program::new();
    p.alu(1, Alu::Mov, 0, 3, true);
    p.label("top").unwrap();
    p.alu(1, Alu::Sub, 1, 1, true)
        .jump("top", Cond::Ne, 1, 0, true);
    let st = built(&p);
    assert_eq!(st[2], s(Op::Jump, 1, 2 | 1 << 8, 0, F_IMM));
    assert!(p.label("top").is_err());
    let mut q = Program::new();
    q.jump("nowhere", Cond::Always, 0, 0, true);
    assert_eq!(
        q.build(&f(), BuildOpts::default()).unwrap_err().clause,
        "L0.STEP.052"
    );
}

#[test]
fn l0_step_053_patch_arg() {
    assert_eq!(
        patch_arg(8, 16, 2, false, false).unwrap(),
        8 | 16 << 8 | 2 << 16
    );
    assert_eq!(
        patch_arg(64, 40, 0, true, true).unwrap(),
        64 | 40 << 8 | 1 << 24 | 1 << 26
    );
    let mut b = builder();
    b.payload(w(0), None, "");
    b.patch(3, 0, 8, 16, 2, false, false).unwrap();
    assert_eq!(b.steps[0], s(Op::Patch, 3, 0, 8 | 16 << 8 | 2 << 16, 0));
}

#[test]
fn l0_step_072_patch_refusals() {
    for (bit, width, shift, steps) in [
        (0, 0, 0, false),
        (0, 65, 0, false),
        (0, 8, 64, false),
        (250, 8, 0, false),
        (120, 9, 0, true),
    ] {
        assert_eq!(
            patch_arg(bit, width, shift, steps, false)
                .unwrap_err()
                .clause,
            "L0.STEP.072"
        );
    }
}

#[test]
fn l0_step_054_patch_through_r15_and_loop_await() {
    let mut p = Program::new();
    p.send((1, 1), ws(2), vec![]);
    p.label("top").unwrap();
    let patch = Patch {
        word: 1,
        segments: vec![(0, 16, 0)],
        reg: 2,
        konst: 64,
    };
    p.send((1, 1), ws(3), vec![patch]);
    let st = built(&p);
    assert_eq!(st[0], s(Op::Dispatch, 0, 2, 0, 0));
    assert_eq!(st[1], s(Op::Await, 0, 0, 0, F_IMM));
    assert_eq!(st[2], s(Op::Alu, 15, 1 | 2 << 8, 64, F_IMM));
    assert_eq!(st[3], s(Op::Patch, 15, 3, 16 << 8, 0));
    assert_eq!(st[4], s(Op::Dispatch, 0, 3, 2, 0));
}

#[test]
fn l0_step_055_send_steps() {
    let mut b = builder();
    let u = b.unit((1, 1), None, None).unwrap();
    b.send(u, 7, &[w(1), w(2)], true);
    b.push(u, w(0xD), 0x3000, 4, false, false);
    assert_eq!(b.steps[0], s(Op::Send, 0, 2, 7 << 32, F_ACK));
    assert_eq!(b.steps[1], s(Op::Send, 0, 4, 2 | 8 << 32, F_MEM));
    assert_eq!(&b.payloads[2..], [w(0xD), w(0x3000)]);
}

#[test]
fn l0_step_056_recv_step() {
    let mut b = builder();
    b.recv(0x7800, 2, 0xFFFF, false);
    assert_eq!(b.steps[0], s(Op::Recv, 0xFFFF, 2, 0, 0));
    assert_eq!(b.payloads[0], w(0x7800));
}

#[test]
fn l0_step_058_kept_steps_close_the_engine_step() {
    let p = pkg(
        vec![],
        vec![
            s(Op::Mwait, 0, 1, 0, 0),
            s(Op::Signal, 0, 5, 6, 0),
            s(Op::Mwait, 0, 2, 0, 0),
        ],
        vec![],
    );
    let low = engine::lower(&p, &f()).unwrap();
    let ops: Vec<Op> = low.steps.iter().map(|s| s.op).collect();
    assert_eq!(ops, [Op::Engine, Op::Signal, Op::Engine]);
    assert_eq!(low.steps[1], p.steps[1]);
}

#[test]
fn l0_step_064_wait_moves_past_label_refused() {
    let mut p = Program::new();
    let t = p.post(vec![(0, 1 << 16)], vec![]);
    p.label("l").unwrap();
    p.wait_moves(t).unwrap();
    assert_eq!(
        p.build(&f(), BuildOpts::default()).unwrap_err().clause,
        "L0.STEP.064"
    );
}

#[test]
fn l0_step_075_lower_refuses_an_engine_step() {
    let p = pkg(vec![], vec![s(Op::Engine, 0, 0, 0, 0)], vec![]);
    assert_eq!(engine::lower(&p, &f()).unwrap_err().clause, "L0.DE.039");
}
