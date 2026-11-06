use ktpu_l0::format::{Cond, Op, F_IMM};
use ktpu_l0::program::{BuildOpts, Program};
use ktpu_l0::word::Word;
use ktpu_l0::Facts;

use super::common::card;

fn marked() -> (Program, Facts) {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let core = machine.die.units.vc[0];
    let coord = (core.x() as u32, core.y() as u32);
    let mut p = Program::new();
    p.send(coord, vec![Word::ZERO], vec![]);
    let mark = p.mark(coord);
    p.load(0, 4096, None, false);
    p.jump("join", Cond::Ne, 0, 0, true);
    p.settle(1);
    p.label("join").expect("label");
    p.send(coord, vec![Word::ZERO], vec![]);
    p.wait(coord, Some(mark)).expect("mark belongs to unit");
    (p, facts)
}

#[test]
fn marks_wait_only_for_the_saved_frontier() {
    let (p, facts) = marked();
    let b = p
        .build(&facts, BuildOpts::default())
        .expect("marked frontier agrees on both paths");
    let mut waits = b.steps.iter().filter(|s| s.op == Op::Await);
    let wait = waits.next().expect("marked wait");
    let sent = 2;
    let completed = 1;
    let target = if wait.flags & F_IMM != 0 {
        sent
    } else {
        wait.count
    };
    assert!(completed >= target, "post-mark work must not hold the wait");
    let full = waits.next().expect("full-frontier wait");
    let final_target = if full.flags & F_IMM != 0 {
        sent
    } else {
        target + full.count
    };
    assert!(
        completed < final_target,
        "post-mark work must still be awaited"
    );
    assert!(
        sent >= final_target,
        "all completions must release the final wait"
    );
    let machine = card();
    let core = machine.die.units.vc[0];
    let coord = (core.x() as u32, core.y() as u32);
    let mut p = Program::new();
    p.send(coord, vec![Word::ZERO], vec![]);
    let mark = p.mark(coord);
    p.load(0, 4096, None, false);
    p.jump("join", Cond::Ne, 0, 0, true);
    p.send(coord, vec![Word::ZERO], vec![]);
    p.label("join").expect("label");
    p.send(coord, vec![Word::ZERO], vec![]);
    p.wait(coord, None).expect("full wait");
    p.wait(coord, Some(mark)).expect("mark belongs to unit");
    let b = p
        .build(&facts, BuildOpts::default())
        .expect("full wait covers the mark on both incoming paths");
    let mut full_completed = false;
    for step in &b.steps {
        if step.op == Op::Await {
            assert!(!full_completed, "a completed mark must not poll again");
            for sent in [2, 3] {
                let target = if step.flags & F_IMM != 0 {
                    sent
                } else {
                    step.count
                };
                assert!(sent - 1 < target, "full wait must await every send");
                assert!(sent >= target, "all completions release the full wait");
            }
            full_completed = true;
        }
    }
    assert!(full_completed, "outstanding sends must be awaited");
}

#[test]
fn path_dependent_outstanding_frontier_releases_before_later_work() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let core = machine.die.units.vc[0];
    let coord = (core.x() as u32, core.y() as u32);
    let mut p = Program::new();
    p.send(coord, vec![Word::ZERO], vec![]);
    p.load(0, 4096, None, false);
    p.jump("marked", Cond::Ne, 0, 0, true);
    p.send(coord, vec![Word::ZERO], vec![]);
    p.label("marked").expect("label");
    let mark = p.mark(coord);
    p.send(coord, vec![Word::ZERO], vec![]);
    p.wait(coord, Some(mark)).expect("marked wait");
    p.settle(1);
    let b = p
        .build(&facts, BuildOpts::default())
        .expect("both incoming outstanding frontiers are representable");

    for taken in [false, true] {
        let completed_at_mark = if taken { 1 } else { 2 };
        let mut sent = 0;
        let mut awaited = 0;
        let mut pc = 0;
        let mut released_mark = false;
        let mut waited_for_later_work = false;
        while let Some(step) = b.steps.get(pc) {
            let mut next = pc + 1;
            match step.op {
                Op::Dispatch | Op::Fetch => sent += step.count,
                Op::Await => {
                    awaited = if step.flags & F_IMM != 0 {
                        sent
                    } else {
                        awaited + step.count
                    };
                    if released_mark {
                        waited_for_later_work |= awaited > completed_at_mark;
                        assert!(
                            awaited <= sent,
                            "all completions must release the final wait"
                        );
                    } else {
                        assert!(
                            awaited <= completed_at_mark,
                            "later work must not delay the saved frontier"
                        );
                    }
                }
                Op::Settle => {
                    assert_eq!(
                        awaited, completed_at_mark,
                        "the mark must await all its work"
                    );
                    assert!(sent > awaited, "later work is still outstanding");
                    released_mark = true;
                }
                Op::Jump => {
                    if step.count & 0xff == 0 || taken {
                        next = step.unit as usize;
                    }
                }
                Op::End => break,
                Op::Load | Op::Barrier => {}
                op => panic!("unexpected frontier instruction {op:?}"),
            }
            assert!(next > pc, "this witness has acyclic control flow");
            pc = next;
        }
        assert!(released_mark, "the marked wait must reach its continuation");
        assert!(
            waited_for_later_work,
            "later work must be awaited separately"
        );
    }
}

#[test]
fn overlapping_saved_frontiers_survive_variable_loop_paths() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let core = machine.die.units.vc[0];
    let coord = (core.x() as u32, core.y() as u32);
    let mut p = Program::new();
    p.label("again").expect("label");
    p.send(coord, vec![Word::ZERO], vec![]);
    p.load(0, 4096, None, false);
    p.jump("first", Cond::Ne, 0, 0, true);
    p.send(coord, vec![Word::ZERO], vec![]);
    p.label("first").expect("label");
    let first = p.mark(coord);
    p.send(coord, vec![Word::ZERO], vec![]);
    p.load(1, 4104, None, false);
    p.jump("second", Cond::Ne, 1, 0, true);
    p.send(coord, vec![Word::ZERO], vec![]);
    p.label("second").expect("label");
    let second = p.mark(coord);
    p.send(coord, vec![Word::ZERO], vec![]);
    p.wait(coord, Some(first)).expect("first mark");
    p.settle(1);
    p.wait(coord, Some(second)).expect("second mark");
    p.settle(2);
    p.wait(coord, None).expect("whole iteration");
    p.load(2, 4112, None, false);
    p.jump("again", Cond::Ne, 2, 0, true);
    let b = p
        .build(&facts, BuildOpts::default())
        .expect("finite frontier states");
    assert!(
        b.steps.iter().all(|s| s.op != Op::Frontier),
        "fully awaited iterations need no runtime frontier storage"
    );

    let package = b.build(false, false);
    let mut cores = ktpu_l1::lower::imem::Cores::new(&machine).expect("cores");
    let resident = ktpu_l1::lower::residency::package(&package, &mut cores)
        .expect("residency preserves saved frontiers");
    for b in [package, resident] {
        let mut scratch = b.payloads.clone();
        let mut sent = 0;
        let mut awaited = 0;
        let mut pc = 0;
        let mut iteration = 0;
        let mut base = 0;
        let mut visits = [0, 0];
        let mut registers = [0u64; 3];
        while let Some(s) = b.steps.get(pc) {
            let mut next = pc + 1;
            match s.op {
                Op::Dispatch => sent += s.count,
                Op::Load => {
                    if s.unit == 0 {
                        iteration = visits[1];
                        base = awaited;
                    }
                    registers[s.unit as usize] = match s.unit {
                        0 => iteration % 2,
                        1 => (iteration + 1) % 2,
                        2 => u64::from(iteration < 2),
                        _ => panic!("unexpected register"),
                    };
                }
                Op::Jump => {
                    let ra = (s.count >> 8) & 15;
                    let take = s.count & 255 == 0 || registers[ra as usize] != 0;
                    if take {
                        next = s.unit as usize;
                    }
                }
                op if op as u8 == 23 => match s.flags {
                    0 => scratch[s.count as usize].0[0] = sent,
                    1 => awaited = awaited.max(scratch[s.count as usize].0[0]),
                    2 => {
                        for slot in &mut scratch[s.arg as usize..(s.arg + s.count) as usize] {
                            slot.0[0] = 0;
                        }
                    }
                    _ => panic!("unexpected frontier operation"),
                },
                Op::Await => {
                    awaited = if s.flags == F_IMM {
                        sent
                    } else {
                        awaited + s.count
                    };
                }
                Op::Settle => {
                    let first = base + if iteration % 2 == 0 { 2 } else { 1 };
                    let second = first + if iteration % 2 == 0 { 1 } else { 2 };
                    let expected = if s.count == 1 { first } else { second };
                    assert_eq!(
                        awaited, expected,
                        "wait must reach its own captured frontier"
                    );
                    assert!(sent > awaited, "later work remains outstanding");
                    visits[(s.count - 1) as usize] += 1;
                }
                Op::Barrier => assert_eq!(awaited, sent),
                Op::End => break,
                op => panic!("unexpected instruction {op:?}"),
            }
            pc = next;
        }
        assert_eq!(
            visits,
            [3, 3],
            "both marked continuations run every iteration"
        );
        assert_eq!(awaited, sent, "the final wait completes the later work");
    }
}

#[test]
fn outstanding_mark_across_unbounded_backedge_keeps_its_runtime_target() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let core = machine.die.units.vc[0];
    let coord = (core.x() as u32, core.y() as u32);
    let mut p = Program::new();
    p.label("before").expect("label");
    p.send(coord, vec![Word::ZERO], vec![]);
    p.load(0, 4096, None, false);
    p.jump("before", Cond::Ne, 0, 0, true);
    let mark = p.mark(coord);
    p.label("after").expect("label");
    p.send(coord, vec![Word::ZERO], vec![]);
    p.load(1, 4104, None, false);
    p.jump("after", Cond::Ne, 1, 0, true);
    p.wait(coord, Some(mark)).expect("marked wait");
    p.settle(1);
    let b = p.build(&facts, BuildOpts::default()).expect("live mark");
    assert!(
        b.steps.iter().any(|s| s.op == Op::Frontier),
        "independently unbounded prefixes and suffixes need a saved target"
    );
    for (before, after) in [(1, 1), (3, 2), (2, 4)] {
        let mut scratch = b.payloads.clone();
        let mut registers = [0u64; 2];
        let mut loads = [0; 2];
        let mut sent = 0;
        let mut awaited = 0;
        let mut pc = 0;
        let mut released = false;
        let mut steps = 0;
        while let Some(s) = b.steps.get(pc) {
            steps += 1;
            assert!(steps < 1000, "the selected finite execution must terminate");
            let mut next = pc + 1;
            match s.op {
                Op::Dispatch | Op::Fetch => sent += s.count,
                Op::Load => {
                    let r = s.unit as usize;
                    loads[r] += 1;
                    registers[r] = u64::from(loads[r] < [before, after][r]);
                }
                Op::Jump => {
                    let r = (s.count >> 8) & 15;
                    if s.count & 255 == 0 || registers[r as usize] != 0 {
                        next = s.unit as usize;
                    }
                }
                Op::Frontier => match s.flags {
                    0 => scratch[s.count as usize].0[0] = sent,
                    1 => awaited = awaited.max(scratch[s.count as usize].0[0]),
                    2 => {
                        for slot in &mut scratch[s.arg as usize..(s.arg + s.count) as usize] {
                            slot.0[0] = 0;
                        }
                    }
                    _ => panic!("unexpected frontier operation"),
                },
                Op::Await => {
                    awaited = if s.flags & F_IMM != 0 {
                        sent
                    } else {
                        awaited + s.count
                    };
                }
                Op::Settle => {
                    assert_eq!(awaited, before, "only pre-mark work holds the wait");
                    assert_eq!(sent - awaited, after, "later work remains outstanding");
                    released = true;
                }
                Op::Barrier => assert_eq!(awaited, sent),
                Op::End => break,
                op => panic!("unexpected instruction {op:?}"),
            }
            pc = next;
        }
        assert!(released, "the saved wait reaches its continuation");
        assert_eq!(awaited, sent, "the final wait drains the suffix separately");
    }
}
