//! Reusable storage for live runtime completion frontiers.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use crate::build::PackageBuilder;
use crate::format::Op;

use super::Event;

pub(super) fn allocate(
    b: &PackageBuilder,
    events: &HashMap<usize, Event>,
    needed: &HashSet<usize>,
    ends: &HashMap<usize, usize>,
) -> BTreeMap<usize, usize> {
    let mut starts: Vec<_> = ends.keys().copied().collect();
    starts.sort_unstable();
    let successors = |end: usize| {
        let s = &b.steps[end - 1];
        let mut next = Vec::new();
        if s.op != Op::End && !(s.op == Op::Jump && s.count & 0xff == 0) {
            next.push(end);
        }
        if s.op == Op::Jump {
            next.push(s.unit as usize);
        }
        next
    };
    let transfer = |pc: usize, live: &mut HashSet<usize>| match events.get(&pc) {
        Some(Event::Mark(_, t)) => {
            live.remove(t);
        }
        Some(Event::Wait(_, Some(t))) if needed.contains(t) => {
            live.insert(*t);
        }
        _ => {}
    };
    let mut incoming: HashMap<usize, HashSet<usize>> = HashMap::new();
    loop {
        let mut changed = false;
        for &start in starts.iter().rev() {
            let end = ends[&start];
            let mut live = HashSet::new();
            for next in successors(end) {
                if let Some(set) = incoming.get(&next) {
                    live.extend(set.iter().copied());
                }
            }
            for pc in (start..end).rev() {
                transfer(pc, &mut live);
            }
            if incoming.get(&start) != Some(&live) {
                incoming.insert(start, live);
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }
    let mut edges: BTreeMap<usize, BTreeSet<usize>> =
        needed.iter().map(|&t| (t, BTreeSet::new())).collect();
    for &start in &starts {
        let end = ends[&start];
        let mut live = HashSet::new();
        for next in successors(end) {
            if let Some(set) = incoming.get(&next) {
                live.extend(set.iter().copied());
            }
        }
        for pc in (start..end).rev() {
            if let Some(Event::Mark(_, t)) = events.get(&pc) {
                if needed.contains(t) {
                    for &other in &live {
                        if other != *t {
                            edges.entry(*t).or_default().insert(other);
                            edges.entry(other).or_default().insert(*t);
                        }
                    }
                }
            }
            transfer(pc, &mut live);
        }
    }
    let mut slots = BTreeMap::new();
    for (&token, neighbours) in &edges {
        let occupied: BTreeSet<_> = neighbours
            .iter()
            .filter_map(|t| slots.get(t).copied())
            .collect();
        let mut slot = 0;
        while occupied.contains(&slot) {
            slot += 1;
        }
        slots.insert(token, slot);
    }
    slots
}
