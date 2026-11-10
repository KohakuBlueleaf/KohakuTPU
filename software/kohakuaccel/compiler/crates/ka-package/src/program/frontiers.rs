//! Unit-local completion relations at control-flow block boundaries.
use crate::build::PackageBuilder;
use crate::error::Result;
use crate::format::{Op, F_IMM};
use crate::refuse;
use state::Frontiers;
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};

mod segments;
mod slots;
mod specialize;
mod state;

#[derive(Clone, Copy)]
pub(super) enum Event {
    Mark(usize, usize),
    Wait(usize, Option<usize>),
}

/// Analysis owns only a step graph, never the accumulated package payloads.
struct Graph {
    steps: Vec<crate::Step>,
    units: usize,
    first_slot: usize,
    terminal: bool,
}
impl Graph {
    fn empty(&self) -> Self {
        Self {
            steps: Vec::new(),
            units: self.units,
            first_slot: self.first_slot,
            terminal: self.terminal,
        }
    }
    fn initial(&self) -> Frontiers {
        // Keep the base DBM, including its arithmetic, widening and absence of
        // closure. Retained scale is bounded by segmentation, not a shortcut
        // that expands the whole historical matrix when it misses.
        Frontiers::new(self.units)
    }
}

pub(super) fn lower(b: &mut PackageBuilder, events: &HashMap<usize, Event>) -> Result<()> {
    if crate::composition::active() {
        return segments::lower(b, events);
    }
    let mut graph = Graph {
        steps: std::mem::take(&mut b.steps),
        units: b.units.len(),
        first_slot: b.payloads.len(),
        terminal: true,
    };
    let slots = lower_graph(&mut graph, events, true)?;
    b.payloads
        .resize(graph.first_slot + slots, crate::word::Word::ZERO);
    b.steps = graph.steps;
    Ok(())
}

fn lower_graph(b: &mut Graph, events: &HashMap<usize, Event>, specialize: bool) -> Result<usize> {
    let units = b.units;
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
                        state.insert(t, a, 0);
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
    let mut incoming = HashMap::from([(0, b.initial())]);
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
                if !state.is_defined(t) {
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
                                let slots = lower_graph(&mut expanded, &mapped, false)?;
                                *b = expanded;
                                return Ok(slots);
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
    let first_slot = b.first_slot;
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
    b.steps = lowered;
    Ok(slot_count)
}
