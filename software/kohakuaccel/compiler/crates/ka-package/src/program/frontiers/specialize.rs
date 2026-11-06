//! Specialize control flow by relative completion state without extra path steps.

use std::collections::{HashMap, HashSet};

use crate::build::PackageBuilder;
use crate::error::Result;
use crate::format::Op;
use crate::Step;

use super::{Event, Frontiers};

#[derive(Hash, PartialEq, Eq)]
struct Key {
    pc: usize,
    upper: Vec<((usize, usize), i128)>,
    defined: Vec<usize>,
}

fn key(pc: usize, state: &Frontiers, live: &HashSet<usize>, units: usize) -> Key {
    let keep = |v: usize| v < 2 * units || live.contains(&v);
    let mut upper: Vec<_> = state
        .upper
        .iter()
        .filter(|&(&(x, y), _)| keep(x) && keep(y))
        .map(|(&p, &v)| (p, v))
        .collect();
    upper.sort_unstable();
    let mut defined: Vec<_> = state.defined.iter().copied().filter(|&v| keep(v)).collect();
    defined.sort_unstable();
    Key { pc, upper, defined }
}

/// Equivalent relative states share jump targets, including backedges. Layout
/// duplicates fallthrough tails rather than inserting an executed join step.
/// The wire step limit bounds construction for unbounded relative states.
pub(super) fn expand(
    b: &PackageBuilder,
    events: &HashMap<usize, Event>,
    marks: &HashMap<usize, usize>,
    transfer: impl Fn(usize, &mut Frontiers) -> Result<()>,
) -> Result<Option<(PackageBuilder, HashMap<usize, Event>)>> {
    let n = b.steps.len();
    if b.steps.last().is_none_or(|s| s.op != Op::Barrier)
        || b.steps.iter().any(|s| {
            (s.op == Op::Jump && s.unit as usize > n) || (s.op == Op::Patch && s.arg >> 24 & 1 != 0)
        })
    {
        return Ok(None);
    }
    // Only marks used before their next definition distinguish continuations.
    let mut live = vec![HashSet::new(); n + 1];
    loop {
        let mut changed = false;
        for pc in (0..n).rev() {
            let s = &b.steps[pc];
            let mut next = if s.op == Op::End || (s.op == Op::Jump && s.count & 0xff == 0) {
                HashSet::new()
            } else {
                live[pc + 1].clone()
            };
            if s.op == Op::Jump {
                next.extend(live[s.unit as usize].iter().copied());
            }
            match events.get(&pc) {
                Some(Event::Mark(_, t)) => {
                    next.remove(&marks[t]);
                }
                Some(Event::Wait(_, Some(t))) => {
                    next.insert(marks[t]);
                }
                _ => {}
            }
            if next != live[pc] {
                live[pc] = next;
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }
    let mut owners: Vec<_> = events
        .values()
        .filter_map(|e| match e {
            Event::Mark(u, t) => Some((marks[t], 2 * u + 1)),
            _ => None,
        })
        .collect();
    owners.sort_unstable();
    owners.dedup();
    let normalize = |state: &mut Frontiers| {
        // A completed mark remains dominated until its next definition. Its
        // distance behind the awaited frontier cannot affect a future wait.
        for &(mark, awaited) in &owners {
            if state.defined.contains(&mark) && state.covered(mark, awaited) {
                state.assign(mark, awaited);
            }
        }
    };
    let mut out = b.clone();
    out.steps.clear();
    let mut mapped = HashMap::new();
    let mut seen = HashMap::new();
    let mut exits = Vec::new();
    let mut todo = vec![(0usize, Frontiers::new(b.units.len()), None::<usize>)];
    while let Some((mut pc, mut state, branch)) = todo.pop() {
        normalize(&mut state);
        if pc == n {
            if let Some(branch) = branch {
                exits.push(branch);
            }
            continue;
        }
        if let Some(branch) = branch {
            if let Some(&target) = seen.get(&key(pc, &state, &live[pc], b.units.len())) {
                out.steps[branch].unit = target;
                continue;
            }
            out.steps[branch].unit = out.steps.len() as u64;
        }
        loop {
            if out.steps.len() == u16::MAX as usize {
                return Ok(None);
            }
            normalize(&mut state);
            let mut s = b.steps[pc];
            let at = out.steps.len();
            seen.entry(key(pc, &state, &live[pc], b.units.len()))
                .or_insert(at as u64);
            if let Some(event) = events.get(&pc) {
                mapped.insert(at, *event);
            }
            if pc + 1 == n {
                // Package termination performs the final barrier.
                out.steps.push(Step::new(Op::Jump, 0, 0, 0, 0));
                exits.push(at);
                break;
            }
            transfer(pc, &mut state)?;
            normalize(&mut state);
            if s.op == Op::Jump {
                let target = s.unit as usize;
                if s.count & 0xff == 0 {
                    out.steps.push(s);
                    todo.push((target, state, Some(at)));
                    break;
                }
                todo.push((target, state.clone(), Some(at)));
                // The taken edge is relocated when its state is laid out.
                s.unit = 0;
            }
            out.steps.push(s);
            if s.op == Op::End {
                break;
            }
            pc += 1;
        }
    }
    let end = out.steps.len() as u64;
    for at in exits {
        out.steps[at].unit = end;
    }
    Ok(Some((out, mapped)))
}
