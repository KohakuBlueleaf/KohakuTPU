//! Completion suffixes and captured positions through residency rewriting.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

type Suffix = Option<VecDeque<bool>>;

#[derive(Clone, PartialEq, Eq)]
struct Saved {
    suffix: Suffix,
    /// The expected frontier has not passed this capture.
    ahead: bool,
    /// Captures no later than this one, by payload slot.
    older: BTreeSet<u64>,
}

#[derive(Clone, PartialEq, Eq, Default)]
pub(super) struct Unit {
    pending: Suffix,
    saved: BTreeMap<u64, Saved>,
}

fn meet(left: &mut Suffix, right: &Suffix) {
    if left != right {
        *left = None;
    }
}

impl Unit {
    pub(super) fn empty() -> Self {
        Self {
            pending: Some(VecDeque::new()),
            saved: BTreeMap::new(),
        }
    }

    pub(super) fn send(&mut self, flags: impl IntoIterator<Item = bool>) {
        for flag in flags {
            if let Some(q) = &mut self.pending {
                q.push_back(flag);
            }
            for saved in self.saved.values_mut() {
                if let Some(q) = &mut saved.suffix {
                    q.push_back(flag);
                }
            }
        }
    }

    pub(super) fn capture(&mut self, slot: u64) {
        for saved in self.saved.values_mut() {
            saved.older.remove(&slot);
        }
        let older = self.saved.keys().copied().chain([slot]).collect();
        self.saved.insert(
            slot,
            Saved {
                suffix: Some(VecDeque::new()),
                ahead: true,
                older,
            },
        );
    }

    pub(super) fn full(&mut self) {
        self.pending = Some(VecDeque::new());
        for saved in self.saved.values_mut() {
            saved.ahead = saved.suffix.as_ref().is_some_and(|q| q.is_empty());
        }
    }

    pub(super) fn wait_saved(&mut self, slot: u64) {
        let Some(saved) = self.saved.get(&slot) else {
            self.pending = None;
            return;
        };
        self.pending = if saved.ahead {
            saved.suffix.clone()
        } else {
            match (&self.pending, &saved.suffix) {
                (Some(p), Some(s)) => Some(if p.len() <= s.len() { p } else { s }.clone()),
                _ => None,
            }
        };
        for saved in self.saved.values_mut() {
            saved.ahead &= saved.older.contains(&slot);
        }
    }

    pub(super) fn wait_delta(&mut self, count: u64) -> Option<u64> {
        let q = self.pending.as_mut()?;
        let mut kept = 0;
        for _ in 0..count {
            kept += u64::from(q.pop_front()?);
        }
        for saved in self.saved.values_mut() {
            saved.ahead &= saved.suffix.as_ref().is_some_and(|s| s.len() <= q.len());
        }
        Some(kept)
    }

    pub(super) fn join(&mut self, other: &Self) {
        meet(&mut self.pending, &other.pending);
        self.saved.retain(|slot, saved| {
            let Some(incoming) = other.saved.get(slot) else {
                return false;
            };
            meet(&mut saved.suffix, &incoming.suffix);
            saved.ahead &= incoming.ahead;
            saved.older.retain(|slot| incoming.older.contains(slot));
            true
        });
    }
}

#[derive(Clone, PartialEq, Eq, Default)]
pub(super) struct State(pub(super) BTreeMap<u64, Unit>);

impl State {
    pub(super) fn clear(&mut self, first: u64, count: u64) {
        for unit in self.0.values_mut() {
            unit.saved
                .retain(|slot, _| !(first..first + count).contains(slot));
        }
    }

    pub(super) fn unit(&mut self, unit: u64) -> &mut Unit {
        self.0.entry(unit).or_insert_with(Unit::empty)
    }

    pub(super) fn join(&mut self, other: &Self) -> bool {
        let old = self.clone();
        let units: BTreeSet<_> = self.0.keys().chain(other.0.keys()).copied().collect();
        for unit in units {
            self.unit(unit)
                .join(other.0.get(&unit).unwrap_or(&Unit::empty()));
        }
        *self != old
    }
}
