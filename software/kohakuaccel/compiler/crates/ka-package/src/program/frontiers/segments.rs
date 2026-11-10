//! Quiescent decomposition derived from emitted steps, not launch annotations.
use super::{lower_graph, Event, Graph};
use crate::build::PackageBuilder;
use crate::error::Result;
use crate::format::{Op, F_IMM};
use crate::frontier_profile::entries;
use std::collections::HashMap;

/// No control or token lifetime may straddle a cut. Interval differences make
/// this O(steps + events + units), not all jumps/marks times all barriers.
fn cuts(steps: &[crate::Step], events: &HashMap<usize, Event>, units: usize) -> Vec<usize> {
    let n = steps.len();
    let whole = || vec![0, n];
    let mut forbidden = vec![0i64; n + 2];
    let mut targets = vec![false; n + 1];
    let mut span = |a: usize, b: usize| {
        if a != b {
            forbidden[a.min(b) + 1] += 1;
            forbidden[a.max(b) + 1] -= 1;
        }
    };
    for (pc, s) in steps.iter().enumerate() {
        entries(1);
        if s.op == Op::End && pc + 1 < n {
            return whole();
        }
        if s.op == Op::Frontier {
            return whole();
        }
        if s.op == Op::Jump {
            let to = s.unit as usize;
            if to > n {
                return whole();
            }
            span(pc, to);
            targets[to] = true;
        }
        if s.op == Op::Patch && s.arg >> 24 & 1 != 0 {
            // Do not partition self-modifying control flow. Program lowering's
            // address patches into FETCH's arg are harmless to this proof.
            let to = s.count as usize;
            let bit = s.arg & 0xff;
            let width = (s.arg >> 8) & 0xff;
            if to >= n || steps[to].op != Op::Fetch || bit < 64 || bit + width > 128 {
                return whole();
            }
            span(pc, to);
        }
    }
    let mut tokens = HashMap::<usize, (usize, usize, Option<usize>, bool)>::new();
    for (&pc, &e) in events {
        entries(1);
        if pc >= n {
            return whole();
        }
        let (u, token, definition) = match e {
            Event::Mark(u, t) => (u, t, true),
            Event::Wait(u, Some(t)) => (u, t, false),
            Event::Wait(_, None) => continue,
        };
        let v = tokens.entry(token).or_insert((pc, pc, Some(u), false));
        v.0 = v.0.min(pc);
        v.1 = v.1.max(pc);
        if v.2 != Some(u) {
            v.2 = None;
        }
        v.3 |= definition;
    }
    for (_, (first, last, owner, defined)) in tokens {
        entries(1);
        if owner.is_none() || !defined {
            return whole();
        }
        span(first, last);
    }
    drop(span);
    let mut active = vec![false; units];
    let mut waited = vec![usize::MAX; units];
    let (mut active_count, mut wait_count, mut epoch) = (0, 0, 0);
    let mut blocked = 0i64;
    let mut out = vec![0];
    entries(units);
    for (pc, s) in steps.iter().enumerate() {
        entries(1);
        blocked += forbidden[pc + 1];
        if matches!(s.op, Op::Dispatch | Op::Fetch | Op::Repeat) {
            let u = s.unit as usize;
            if u >= units {
                return whole();
            }
            if !active[u] {
                active[u] = true;
                active_count += 1;
            }
        }
        let full_wait = if s.op == Op::Await && s.flags == F_IMM && s.count == 0 && s.arg == 0 {
            match events.get(&pc) {
                Some(Event::Wait(u, None)) if *u == s.unit as usize => Some(*u),
                None => Some(s.unit as usize),
                _ => None,
            }
        } else {
            None
        };
        if let Some(u) = full_wait {
            if u >= units {
                return whole();
            }
            // A jump into the suffix invalidates all earlier awaits. Generations
            // avoid clearing/scanning the unit set for every ordinary step.
            if targets[pc] {
                epoch = pc + 1;
                wait_count = 0;
            }
            if active[u] && waited[u] != epoch {
                waited[u] = epoch;
                wait_count += 1;
            }
        } else {
            if s.op == Op::Barrier
                && pc + 1 < n
                && blocked == 0
                && !targets[pc]
                && wait_count == active_count
            {
                out.push(pc + 1);
            }
            epoch = pc + 1;
            wait_count = 0;
        }
    }
    out.push(n);
    out
}

pub(super) fn lower(b: &mut PackageBuilder, events: &HashMap<usize, Event>) -> Result<()> {
    let cuts = cuts(&b.steps, events, b.units.len());
    let source = std::mem::take(&mut b.steps);
    let first_slot = b.payloads.len();
    let mut scratch = 0;
    let mut out = Vec::with_capacity(source.len());
    for window in cuts.windows(2) {
        let (start, end) = (window[0], window[1]);
        let mut graph = Graph {
            steps: source[start..end].to_vec(),
            units: b.units.len(),
            first_slot,
            terminal: end == source.len(),
        };
        let mut local = HashMap::new();
        // Probe the event map once per step, not once per launch per event.
        for (offset, s) in graph.steps.iter_mut().enumerate() {
            entries(1);
            if let Some(e) = events.get(&(start + offset)) {
                local.insert(offset, *e);
            }
            if s.op == Op::Jump {
                s.unit -= start as u64;
            }
            if s.op == Op::Patch && s.arg >> 24 & 1 != 0 {
                s.count -= start as u64;
            }
        }
        scratch = scratch.max(lower_graph(&mut graph, &local, true)?);
        let base = out.len() as u64;
        for s in &mut graph.steps {
            entries(1);
            if s.op == Op::Jump {
                s.unit += base;
            }
            if s.op == Op::Patch && s.arg >> 24 & 1 != 0 {
                s.count += base;
            }
        }
        out.extend(graph.steps);
    }
    // Slot lifetimes cannot cross cuts. Every segment's original clear remains;
    // reuse the same scratch suffix, without copying or relocating payloads.
    b.payloads
        .resize(first_slot + scratch, crate::word::Word::ZERO);
    b.steps = out;
    Ok(())
}
