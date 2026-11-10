//! Specialize control flow by relative completion state without extra path steps.
use super::{Event, Frontiers, Graph};
use crate::error::Result;
use crate::format::Op;
use crate::Step;
use std::collections::{HashMap, HashSet};

#[derive(Hash, PartialEq, Eq)]
struct Key {
    pc: usize,
    upper: Vec<((usize, usize), i128)>,
    defined: Vec<usize>,
}
fn key(pc: usize, state: &Frontiers, live: &HashSet<usize>, units: usize) -> Key {
    let keep = |v: usize| v < 2 * units || live.contains(&v);
    let mut upper: Vec<_> = state.projected_upper(keep).collect();
    upper.sort_unstable();
    let mut defined: Vec<_> = state.defined_vars().filter(|&v| keep(v)).collect();
    defined.sort_unstable();
    Key { pc, upper, defined }
}

pub(super) fn expand(
    b: &Graph,
    events: &HashMap<usize, Event>,
    marks: &HashMap<usize, usize>,
    transfer: impl Fn(usize, &mut Frontiers) -> Result<()>,
) -> Result<Option<(Graph, HashMap<usize, Event>)>> {
    let n = b.steps.len();
    if b.steps.last().is_none_or(|s| s.op != Op::Barrier)
        || b.steps.iter().any(|s| {
            (s.op == Op::Jump && s.unit as usize > n) || (s.op == Op::Patch && s.arg >> 24 & 1 != 0)
        })
    {
        return Ok(None);
    }
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
        for &(mark, awaited) in &owners {
            if state.is_defined(mark) && state.covered(mark, awaited) {
                state.assign(mark, awaited);
            }
        }
    };
    // Only the local step graph is copied. Payloads/relocations belong to the
    // composed package and must not be recopied at each launch specialization.
    let mut out = Graph {
        steps: Vec::new(),
        ..b.empty()
    };
    let mut mapped = HashMap::new();
    let mut seen = HashMap::new();
    let mut exits = Vec::new();
    let mut todo = vec![(0usize, b.initial(), None::<usize>)];
    while let Some((mut pc, mut state, branch)) = todo.pop() {
        normalize(&mut state);
        if pc == n {
            if let Some(branch) = branch {
                exits.push(branch);
            }
            continue;
        }
        if let Some(branch) = branch {
            if let Some(&target) = seen.get(&key(pc, &state, &live[pc], b.units)) {
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
            seen.entry(key(pc, &state, &live[pc], b.units))
                .or_insert(at as u64);
            if let Some(event) = events.get(&pc) {
                mapped.insert(at, *event);
            }
            if pc + 1 == n {
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
    // The original expansion relies on package termination for the last drain.
    // An internal segment is NOT package termination: its exits must execute
    // the actual barrier before the next launch's unchanged gather/release.
    if !b.terminal {
        if out.steps.len() == u16::MAX as usize {
            return Ok(None);
        }
        out.steps.push(Step::bare(Op::Barrier));
    }
    Ok(Some((out, mapped)))
}
