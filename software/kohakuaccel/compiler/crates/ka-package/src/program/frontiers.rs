//! Unit-local completion relations at control-flow block boundaries.

use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};

use crate::build::PackageBuilder;
use crate::error::Result;
use crate::format::{Op, F_IMM};
use crate::refuse;

mod slots;
mod specialize;

#[derive(Clone, Copy)]
pub(super) enum Event {
    Mark(usize, usize),
    Wait(usize, Option<usize>),
}

#[derive(Clone)]
struct Frontiers {
    /// Finite upper bounds on x - y. Missing pairs have no known bound.
    upper: HashMap<(usize, usize), i128>,
    defined: HashSet<usize>,
}

impl Frontiers {
    fn new(units: usize) -> Self {
        let mut upper = HashMap::new();
        let mut defined = HashSet::new();
        for u in 0..units {
            upper.insert((2 * u, 2 * u + 1), 0);
            upper.insert((2 * u + 1, 2 * u), 0);
            defined.extend([2 * u, 2 * u + 1]);
        }
        Self { upper, defined }
    }

    fn bound(&self, x: usize, y: usize) -> Option<i128> {
        if x == y {
            Some(0)
        } else {
            self.upper.get(&(x, y)).copied()
        }
    }

    fn difference(&self, x: usize, y: usize) -> Option<i128> {
        let hi = self.bound(x, y)?;
        (self.bound(y, x)? == -hi).then_some(hi)
    }

    fn covered(&self, x: usize, y: usize) -> bool {
        self.bound(x, y).is_some_and(|d| d <= 0)
    }

    fn assign(&mut self, to: usize, from: usize) {
        if to == from {
            return;
        }
        self.upper.retain(|&(x, y), _| x != to && y != to);
        let mut copied = Vec::new();
        for (&(x, y), &bound) in &self.upper {
            if x == from {
                copied.push(((to, y), bound));
            }
            if y == from {
                copied.push(((x, to), bound));
            }
        }
        self.upper.extend(copied);
        self.upper.insert((to, from), 0);
        self.upper.insert((from, to), 0);
        if self.defined.contains(&from) {
            self.defined.insert(to);
        } else {
            self.defined.remove(&to);
        }
    }

    fn add(&mut self, to: usize, n: i128) {
        for (&(x, y), bound) in &mut self.upper {
            if x == to {
                *bound += n;
            }
            if y == to {
                *bound -= n;
            }
        }
    }

    fn join(&mut self, other: &Self) -> bool {
        let mut changed = false;
        self.upper.retain(|pair, bound| {
            let Some(&incoming) = other.upper.get(pair) else {
                changed = true;
                return false;
            };
            if incoming > *bound {
                changed = true;
                if incoming <= 0 {
                    *bound = 0;
                } else {
                    return false;
                }
            }
            true
        });
        self.defined.retain(|v| {
            let keep = other.defined.contains(v);
            changed |= !keep;
            keep
        });
        changed
    }
}

pub(super) fn lower(b: &mut PackageBuilder, events: &HashMap<usize, Event>) -> Result<()> {
    lower_graph(b, events, true)
}

fn lower_graph(
    b: &mut PackageBuilder,
    events: &HashMap<usize, Event>,
    specialize: bool,
) -> Result<()> {
    let units = b.units.len();
    let mut marks = HashMap::new();
    for event in events.values() {
        if let Event::Mark(_, token) = event {
            let next = 2 * units + marks.len();
            marks.entry(*token).or_insert(next);
        }
    }
    let target = |u: usize, token: Option<usize>| -> Result<usize> {
        match token {
            None => Ok(2 * u),
            Some(t) => marks
                .get(&t)
                .copied()
                .ok_or_else(|| crate::Refusal::new("L0.STEP.021", format!("no mark {t}"))),
        }
    };
    let event = |pc: usize| {
        events.get(&pc).copied().or_else(|| {
            let s = &b.steps[pc];
            (s.op == Op::Await).then_some(Event::Wait(s.unit as usize, None))
        })
    };
    let transfer = |pc: usize, state: &mut Frontiers| -> Result<()> {
        match event(pc) {
            Some(Event::Mark(u, token)) => state.assign(marks[&token], 2 * u),
            Some(Event::Wait(u, token)) => {
                let t = target(u, token)?;
                let a = 2 * u + 1;
                if !state.covered(t, a) {
                    if state.covered(a, t) || token.is_none() {
                        state.assign(a, t);
                    } else {
                        let mut advanced = state.clone();
                        advanced.assign(a, t);
                        state.join(&advanced);
                        state.upper.insert((t, a), 0);
                    }
                }
            }
            None => {
                let step = &b.steps[pc];
                let n = match step.op {
                    Op::Dispatch | Op::Fetch => step.count,
                    Op::Repeat => step.count * ((step.arg >> 32) & 0xFFFF),
                    _ => 0,
                };
                if n != 0 {
                    state.add(2 * step.unit as usize, i128::from(n));
                }
            }
        }
        Ok(())
    };
    let mut heads = BTreeSet::from([0, b.steps.len()]);
    let mut targets = HashSet::new();
    for (pc, step) in b.steps.iter().enumerate() {
        if step.op == Op::Jump {
            let to = step.unit as usize;
            if to > b.steps.len() {
                return refuse!("L0.STEP.052", "jump target outside the program");
            }
            heads.extend([to, pc + 1]);
            targets.insert(to);
        } else if step.op == Op::End {
            heads.insert(pc + 1);
        }
    }
    let heads: Vec<_> = heads.into_iter().collect();
    let ends: HashMap<_, _> = heads.windows(2).map(|w| (w[0], w[1])).collect();
    let mut incoming = HashMap::from([(0, Frontiers::new(units))]);
    let mut todo = VecDeque::from([0]);
    while let Some(start) = todo.pop_front() {
        let Some(&end) = ends.get(&start) else {
            continue;
        };
        let mut state = incoming[&start].clone();
        for pc in start..end {
            transfer(pc, &mut state)?;
        }
        let last = &b.steps[end - 1];
        let mut next = if last.op == Op::End {
            Vec::new()
        } else {
            vec![end]
        };
        if last.op == Op::Jump {
            if last.count & 0xFF == 0 {
                next.clear();
            }
            next.push(last.unit as usize);
        }
        for next in next {
            let changed = match incoming.get_mut(&next) {
                Some(old) => old.join(&state),
                None => {
                    incoming.insert(next, state.clone());
                    true
                }
            };
            if changed {
                todo.push_back(next);
            }
        }
    }
    let mut waits = HashMap::new();
    let mut needed = HashSet::new();
    let mut snapshots = HashMap::new();
    for (&start, state) in &incoming {
        let Some(&end) = ends.get(&start) else {
            continue;
        };
        let mut state = state.clone();
        for pc in start..end {
            if let Some(Event::Wait(u, token)) = event(pc) {
                let t = target(u, token)?;
                let a = 2 * u + 1;
                if !state.defined.contains(&t) {
                    return refuse!("L0.STEP.021", "mark is not defined on every incoming path");
                }
                if !state.covered(t, a) {
                    let encoding = if let Some(d) = state.difference(t, a) {
                        let count = u64::try_from(d).map_err(|_| {
                            crate::Refusal::new("L0.STEP.021", "completion delta exceeds u64")
                        })?;
                        (0, count)
                    } else if token.is_none() || state.difference(t, 2 * u) == Some(0) {
                        (F_IMM, 0)
                    } else {
                        if specialize {
                            if let Some((mut expanded, mapped)) =
                                specialize::expand(b, events, &marks, &transfer)?
                            {
                                lower_graph(&mut expanded, &mapped, false)?;
                                *b = expanded;
                                return Ok(());
                            }
                        }
                        if let Some(token) = token {
                            needed.insert(token);
                            snapshots.insert(pc, token);
                        }
                        (0, 0)
                    };
                    waits.insert(pc, encoding);
                }
            }
            transfer(pc, &mut state)?;
        }
    }
    let slots = slots::allocate(b, events, &needed, &ends);
    let slot_count = slots.values().max().map_or(0, |n| n + 1);
    let first_slot = b.payloads.len();
    let clear = crate::Step::new(Op::Frontier, 0, slot_count as u64, first_slot as u64, 2);
    let mut entered = false;
    let mut lowered: Vec<crate::Step> = Vec::new();
    let mut offsets = vec![0; b.steps.len() + 1];
    for (pc, step) in b.steps.iter().enumerate() {
        offsets[pc] = lowered.len() as u64;
        entered |= targets.contains(&pc);
        let mut step = *step;
        match event(pc) {
            Some(Event::Mark(u, token)) => {
                let Some(&slot) = slots.get(&token) else {
                    continue;
                };
                step = crate::Step::new(Op::Frontier, u as u64, (first_slot + slot) as u64, 0, 0);
            }
            Some(Event::Wait(..)) => {
                let Some(&(flags, count)) = waits.get(&pc) else {
                    continue;
                };
                if let Some(token) = snapshots.get(&pc) {
                    step.op = Op::Frontier;
                    step.flags = 1;
                    step.count = (first_slot + slots[token]) as u64;
                } else {
                    step.flags = flags;
                    step.count = count;
                }
            }
            None => {}
        }
        if step.op == Op::Barrier
            && !entered
            && lowered.last().is_none_or(|last| last.op == Op::Barrier)
            && !crate::mutant::on("l0.build/double_barrier")
        {
            continue;
        }
        if step.op == Op::End && slot_count != 0 {
            lowered.push(clear);
        }
        lowered.push(step);
        entered = false;
    }
    offsets[b.steps.len()] = lowered.len() as u64;
    if slot_count != 0 {
        lowered.push(clear);
    }
    for step in &mut lowered {
        if step.op == Op::Jump {
            step.unit = offsets[step.unit as usize];
        } else if step.op == Op::Patch && step.arg >> 24 & 1 != 0 {
            step.count = offsets[step.count as usize];
        }
    }
    b.payloads
        .resize(first_slot + slot_count, crate::word::Word::ZERO);
    b.steps = lowered;
    Ok(())
}
