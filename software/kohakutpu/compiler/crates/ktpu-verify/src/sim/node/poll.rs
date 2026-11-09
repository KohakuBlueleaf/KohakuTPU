//! RV64 sysnode R1_DCACHE polls after a flush request. Under eventual write
//! service/response delivery, rv64_l1.v:303-362 scans LINES entries and drains
//! writebacks before busy falls (rv64_syscore.v:781). This contract is not the
//! rv_pe/kht_pe compute-PE cache contract; kht_pe.v:564 may tie busy to zero.

use super::{cond, flag, op, NStep, Node, Step};
use crate::model::clock::Clock;
use crate::sim::symbolic::{Symbolic, Value};

pub(super) const DCACHE: u64 = 0x1c8;

#[derive(Clone, PartialEq, Eq)]
pub(super) struct Flush {
    visibility: Clock,
}

pub(super) enum Exit {
    Bounded,
    Flushed(Clock),
}

/// Package STORE uses ka_st64's uncached alias. Thus no modeled dirty CPU
/// lines are outstanding at a flush request. Preserve already-acquired
/// publications in its clock, not remote CPU or other units' posted writes.
pub(super) fn request(node: &mut Node, step: Step, clock: &Clock) {
    for k in 0..step.count as usize {
        let Some(word) = node.payloads.get(step.arg as usize + k) else {
            continue;
        };
        for half in [0, 128] {
            if word.bits(half, 64) != DCACHE {
                continue;
            }
            let value = word.bits(half + 64, 64);
            let value = if step.flags & ktpu_l0::format::F_REG != 0 {
                node.regs
                    .get(&((value & 15) as u16))
                    .copied()
                    .unwrap_or(Value::Known(0))
            } else {
                Value::Known(value)
            };
            node.flush = match value {
                Value::Known(value) if value & 1 != 0 => Some(Flush {
                    visibility: clock.clone(),
                }),
                _ => None,
            };
        }
    }
}

#[derive(Clone, Copy, Default)]
struct Affine {
    count: u64,
    status: u64,
    bias: u64,
}

impl Affine {
    fn constant(bias: u64) -> Self {
        Self {
            bias,
            ..Self::default()
        }
    }

    fn add(self, other: Self) -> Self {
        Self {
            count: self.count.wrapping_add(other.count),
            status: self.status.wrapping_add(other.status),
            bias: self.bias.wrapping_add(other.bias),
        }
    }

    fn scale(self, by: u64) -> Self {
        Self {
            count: self.count.wrapping_mul(by),
            status: self.status.wrapping_mul(by),
            bias: self.bias.wrapping_mul(by),
        }
    }

    fn value(self, count: Value, status: Value, symbolic: &mut Symbolic) -> Value {
        let a = symbolic
            .alu(3, count, Value::Known(self.count))
            .expect("multiply");
        let b = symbolic
            .alu(3, status, Value::Known(self.status))
            .expect("multiply");
        let sum = symbolic.alu(1, a, b).expect("add");
        symbolic.alu(1, sum, Value::Known(self.bias)).expect("add")
    }
}

struct Poll {
    counter: u16,
    step: u64,
    outputs: [Option<Affine>; 16],
    changed: [bool; 16],
    left: Affine,
    right: Affine,
    condition: u32,
}

fn operation(step: NStep) -> Option<Step> {
    match step {
        NStep::Op(s) | NStep::JoinOp(s) => Some(s),
        _ => None,
    }
}

fn prove(node: &Node, latch: Step) -> Option<Poll> {
    let start = usize::from(latch.unit);
    let end = node.at;
    if start >= end {
        return None;
    }
    // Entry must execute the observation and every recurrence before the latch.
    for (at, instruction) in node.steps.iter().enumerate() {
        if !(start..end).contains(&at) {
            if let Some(s) = operation(*instruction) {
                if s.op == op::JUMP && (start + 1..=end).contains(&usize::from(s.unit)) {
                    return None;
                }
            }
        }
    }
    let body: Vec<_> = node.steps[start..end]
        .iter()
        .copied()
        .map(operation)
        .collect::<Option<_>>()?;
    let mut changed = [false; 16];
    let mut counter = None;
    let mut observation = None;
    for s in &body {
        *changed.get_mut(usize::from(s.unit))? = true;
        match s.op {
            op::LOAD
                if s.flags == ktpu_l0::format::F_CTRL
                    && node.payloads.get(s.arg as usize)?.bits(0, 64) == DCACHE =>
            {
                if observation.replace(s.unit).is_some() {
                    return None;
                }
            }
            op::ALU => {
                if s.unit == ((s.count >> 8) & 15) as u16
                    && s.count & 255 == 1
                    && s.flags & flag::IMM != 0
                    && s.arg != 0
                {
                    if counter.replace((s.unit, s.arg)).is_some() {
                        return None;
                    }
                }
            }
            _ => return None,
        }
    }
    let (counter, step) = counter?;
    let observation = observation?;
    if counter == observation {
        return None;
    }
    let mut values: [Option<Affine>; 16] = std::array::from_fn(|r| {
        if r == usize::from(counter) {
            Some(Affine {
                count: 1,
                ..Affine::default()
            })
        } else if changed[r] {
            None
        } else {
            match node
                .regs
                .get(&(r as u16))
                .copied()
                .unwrap_or(Value::Known(0))
            {
                Value::Known(v) => Some(Affine::constant(v)),
                Value::Symbolic(_) => None,
            }
        }
    });
    let mut counter_written = false;
    for s in body {
        let dst = usize::from(s.unit);
        if s.unit == counter {
            if counter_written {
                return None;
            }
            counter_written = true;
        }
        let value = if s.op == op::LOAD {
            Affine {
                status: 1,
                ..Affine::default()
            }
        } else {
            let op = s.count & 255;
            let a = if op == 0 {
                Affine::default()
            } else {
                values[((s.count >> 8) & 15) as usize]?
            };
            let b = if s.flags & flag::IMM != 0 {
                Affine::constant(s.arg)
            } else {
                values[((s.count >> 12) & 15) as usize]?
            };
            match op {
                0 => b,
                1 => a.add(b),
                2 => a.add(b.scale(u64::MAX)),
                3 if b.count == 0 && b.status == 0 => a.scale(b.bias),
                _ => return None,
            }
        };
        values[dst] = Some(value);
    }
    let left = values[((latch.count >> 8) & 15) as usize]?;
    let right = if latch.flags & flag::IMM != 0 {
        Affine::constant(latch.arg)
    } else {
        values[((latch.count >> 12) & 15) as usize]?
    };
    Some(Poll {
        counter,
        step,
        outputs: values,
        changed,
        left,
        right,
        condition: latch.count & 255,
    })
}

pub(super) fn summarize(node: &mut Node, symbolic: &mut Symbolic, latch: Step) -> Option<Exit> {
    let poll = prove(node, latch)?;
    let current = node
        .regs
        .get(&poll.counter)
        .copied()
        .unwrap_or(Value::Known(0));
    let (left, right) = (poll.left, poll.right);
    let (count, status, exit) = if left.count == 0
        && right.count == 0
        && !cond(poll.condition, left.bias, right.bias)
        && cond(
            poll.condition,
            left.bias.wrapping_add(left.status),
            right.bias.wrapping_add(right.status),
        ) {
        let flush = node.flush.as_ref()?;
        // A finite positive number of iterations has any u64 residue. All
        // affine outputs share that residue, including counter wraparound.
        let extra = symbolic.fresh();
        let delta = symbolic
            .alu(3, extra, Value::Known(poll.step))
            .expect("multiply");
        (
            symbolic.alu(1, current, delta).expect("add"),
            Value::Known(0),
            Exit::Flushed(flush.visibility.clone()),
        )
    } else if poll.condition == 3
        && left.count == 1
        && left.bias == poll.step
        && right.count == 0
        && right.status == 0
        && left.status >= right.bias
    {
        let Value::Known(first) = current else {
            return None;
        };
        let trips = right.bias.saturating_sub(first).div_ceil(poll.step);
        let Some(last) = trips
            .checked_mul(poll.step)
            .and_then(|d| first.checked_add(d))
        else {
            return None;
        };
        if last.checked_add(left.status).is_none() {
            return None;
        }
        let extra = symbolic.fresh();
        let in_range = symbolic.condition(4, Value::Known(trips), extra);
        symbolic.assume(in_range);
        let delta = symbolic
            .alu(3, extra, Value::Known(poll.step))
            .expect("multiply");
        let early = symbolic.alu(1, current, delta).expect("add");
        let sample = symbolic.fresh();
        let status = symbolic.alu(4, sample, Value::Known(1)).expect("and");
        // Idle exits at the bound; busy may exit at any counted iteration.
        // Construct the relation instead of accumulating status/count clauses.
        let distance = symbolic
            .alu(2, Value::Known(last), early)
            .expect("subtract");
        let selected = symbolic.alu(3, status, distance).expect("multiply");
        let count = symbolic
            .alu(2, Value::Known(last), selected)
            .expect("subtract");
        (count, status, Exit::Bounded)
    } else {
        return None;
    };
    let before = symbolic
        .alu(2, count, Value::Known(poll.step))
        .expect("subtract");
    for (r, expression) in poll.outputs.into_iter().enumerate() {
        if poll.changed[r] {
            let Some(expression) = expression else {
                return None;
            };
            node.regs
                .insert(r as u16, expression.value(before, status, symbolic));
        }
    }
    node.at += 1;
    Some(exit)
}
