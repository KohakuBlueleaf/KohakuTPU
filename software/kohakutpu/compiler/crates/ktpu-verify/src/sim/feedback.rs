//! Credit invariants for feedback-gated counter contributors.
//!
//! Along every CFG prefix, `sent(C) - consumed(G) <= slack`. If only the
//! current waiter produces G, a contributor cannot send more than
//! `slack + waiter_sent(G)` before that wait. Branches include both successors;
//! a positive cycle supplies no finite invariant.

use std::collections::{BTreeMap, BTreeSet};

use crate::adapter::package::{flag, op, Step, PATCH_STEPS};
use crate::model::clock::Clock;

use super::node::{NStep, Node};
use super::Sim;

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Key {
    Slot(u32, u32),
    Bell(u32, u32),
}

impl Key {
    fn name(self) -> String {
        match self {
            Self::Slot(mesh, slot) => format!("slot:{mesh}:{slot}"),
            Self::Bell(mesh, source) => format!("bell:{mesh}:{source}"),
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
struct Credit {
    owner: usize,
    gate: usize,
    slack: i64,
}

#[derive(Clone, Default, PartialEq, Eq)]
pub(super) struct Feedback {
    /// (counter, waiting process) -> contributor bounds.
    rules: BTreeMap<(usize, usize), Vec<Credit>>,
}

fn step(s: &NStep) -> Option<Step> {
    match s {
        NStep::Op(s) | NStep::JoinOp(s) => Some(*s),
        NStep::Seg(_) | NStep::JoinSeg(_) => None,
    }
}

fn sends(mesh: u32, s: Step) -> Option<[(Key, i64); 2]> {
    (s.op == op::RING).then(|| {
        [
            (
                Key::Slot(u32::from(s.unit), s.count),
                ((s.arg & 0xffff) as i64).max(1),
            ),
            (Key::Bell(u32::from(s.unit), mesh), 1),
        ]
    })
}

fn takes(mesh: u32, s: Step) -> Option<(Key, i64)> {
    match s.op {
        op::WAIT_SLOT if s.flags & flag::CONSUME != 0 => {
            Some((Key::Slot(mesh, u32::from(s.unit)), i64::from(s.count)))
        }
        op::WAIT_BELL => Some((Key::Bell(mesh, u32::from(s.unit)), i64::from(s.count))),
        _ => None,
    }
}

/// Longest weighted prefix, or no bound when a reachable cycle is positive.
fn slack(node: &Node, counter: Key, gate: Key) -> Option<i64> {
    let len = node.steps.len();
    let mut distance = vec![None; len + 1];
    distance[0] = Some(0i64);
    let weights: Vec<i64> = node
        .steps
        .iter()
        .map(|s| {
            let Some(s) = step(s) else {
                return 0;
            };
            let sent = sends(node.mesh, s)
                .into_iter()
                .flatten()
                .filter(|(key, _)| *key == counter)
                .map(|(_, amount)| amount)
                .sum::<i64>();
            let consumed = takes(node.mesh, s)
                .filter(|(key, _)| *key == gate)
                .map_or(0, |(_, amount)| amount);
            sent - consumed
        })
        .collect();
    for _ in 0..=len {
        let mut changed = false;
        for (at, s) in node.steps.iter().enumerate() {
            let Some(before) = distance[at] else {
                continue;
            };
            let after = before.checked_add(weights[at])?;
            let jump = step(s)
                .filter(|s| s.op == op::JUMP)
                .map(|s| usize::from(s.unit));
            for next in [Some(at + 1), jump].into_iter().flatten() {
                let value = distance.get_mut(next)?;
                if value.is_none_or(|old| after > old) {
                    *value = Some(after);
                    changed = true;
                }
            }
        }
        if !changed {
            return distance.into_iter().flatten().max();
        }
    }
    None
}

impl Sim<'_> {
    /// Build once for pinned, immutable node steps. Physical package storage
    /// and step PATCH retain the counter-total proof without CFG invariants.
    pub(super) fn prepare_feedback(&mut self) {
        if self.totals.is_none() || !self.card.context.packages.is_empty() {
            return;
        }
        let mut owners: BTreeMap<Key, BTreeSet<usize>> = BTreeMap::new();
        let mut gates: BTreeMap<usize, BTreeSet<Key>> = BTreeMap::new();
        for node in &self.nodes {
            for s in node.steps.iter().filter_map(step) {
                if s.op == op::PATCH && s.arg & PATCH_STEPS != 0 {
                    return;
                }
                for (key, _) in sends(node.mesh, s).into_iter().flatten() {
                    owners.entry(key).or_default().insert(node.proc);
                }
                if let Some((gate, _)) = takes(node.mesh, s) {
                    gates.entry(node.proc).or_default().insert(gate);
                }
            }
        }
        let mut proved = Vec::new();
        for (&counter, contributors) in &owners {
            if contributors.len() < 2 {
                continue;
            }
            for node in self.nodes.iter().filter(|n| contributors.contains(&n.proc)) {
                for &gate in gates.get(&node.proc).into_iter().flatten() {
                    let Some(senders) = owners.get(&gate).filter(|p| p.len() == 1) else {
                        continue;
                    };
                    let waiter = *senders.first().expect("one gate sender");
                    if let Some(slack) = slack(node, counter, gate) {
                        proved.push((counter, waiter, node.proc, gate, slack));
                    }
                }
            }
        }
        for (counter, waiter, owner, gate, slack) in proved {
            let counter = self.counter(&counter.name());
            let gate = self.counter(&gate.name());
            self.feedback
                .rules
                .entry((counter, waiter))
                .or_default()
                .push(Credit { owner, gate, slack });
        }
    }

    pub(super) fn node_counter_clock(&self, c: usize, target: i64, waiter: usize) -> Clock {
        let Some(rules) = self.feedback.rules.get(&(c, waiter)) else {
            return self.clock_to(c, target, &[]);
        };
        let caps: Vec<_> = rules
            .iter()
            .filter_map(|rule| {
                rule.slack
                    .checked_add(self.counters[rule.gate].total(waiter))
                    .map(|cap| (rule.owner, cap))
            })
            .collect();
        self.clock_to(c, target, &caps)
    }
}
