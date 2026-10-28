//! A count that only grows, with the clock of every increment, kept per
//! incrementing process (its owner).
//!
//! `clock_to(t)` is what reaching `t` must follow under every schedule. With
//! one owner that is its first increments up to `t`. With several, owner s
//! contributes at least `t - (total - total_s)` of its own increments
//! whatever the order (verify.md lemma 3), `total_s` its increments over the
//! whole run; `totals` holds them from a first run (absent there, the first
//! run joins its own order, which only that run's totals use).

use super::clock::Clock;

#[derive(Debug, Default)]
pub struct Counter {
    pub name: String,
    pub value: i64,
    /// owner -> [(owner's running total after the increment, its clock)]
    owners: Vec<(usize, Vec<(i64, Clock)>)>,
    totals: Option<Vec<(usize, i64)>>,
}

impl Counter {
    pub fn new(name: String, totals: Option<Vec<(usize, i64)>>) -> Self {
        Counter {
            name,
            value: 0,
            owners: Vec::new(),
            totals,
        }
    }

    pub fn add(&mut self, n: i64, clock: &Clock, owner: usize) {
        if n <= 0 {
            return;
        }
        self.value += n;
        let at = match self.owners.iter().position(|(o, _)| *o == owner) {
            Some(i) => i,
            None => {
                self.owners.push((owner, Vec::new()));
                self.owners.len() - 1
            }
        };
        let marks = &mut self.owners[at].1;
        let last = marks.last().map(|m| m.0).unwrap_or(0);
        marks.push((last + n, clock.clone()));
    }

    pub fn reached(&self, target: i64) -> bool {
        self.value >= target
    }

    pub fn total(&self, owner: usize) -> i64 {
        self.owners
            .iter()
            .find(|(o, _)| *o == owner)
            .and_then(|(_, m)| m.last())
            .map(|m| m.0)
            .unwrap_or(0)
    }

    /// Every owner's increments: the next run's `totals`.
    pub fn tally(&self) -> Vec<(usize, i64)> {
        self.owners
            .iter()
            .map(|(o, _)| (*o, self.total(*o)))
            .collect()
    }

    /// The join of the increments reaching `target` must follow. `caps`
    /// bounds an owner's increments that can precede the wait.
    pub fn clock_to(&self, target: i64, caps: &[(usize, i64)]) -> Clock {
        let mut out = Clock::new();
        if target <= 0 {
            return out;
        }
        let Some(totals) = &self.totals else {
            for (_, marks) in &self.owners {
                join_upto(&mut out, marks, target);
            }
            return out;
        };
        let mut avail: Vec<(usize, i64)> = totals.clone();
        for &(owner, cap) in caps {
            match avail.iter_mut().find(|(o, _)| *o == owner) {
                Some(e) => e.1 = e.1.min(cap),
                None => avail.push((owner, 0i64.min(cap))),
            }
        }
        let whole: i64 = avail.iter().map(|(_, v)| v).sum();
        for (owner, marks) in &self.owners {
            let mine = avail
                .iter()
                .find(|(o, _)| o == owner)
                .map(|(_, v)| *v)
                .unwrap_or(0);
            let k = target - (whole - mine);
            if k > 0 {
                join_upto(&mut out, marks, k);
            }
        }
        out
    }
}

/// Join the clock of the first mark reaching `k`: one owner's clocks only
/// grow, so it holds every earlier one.
fn join_upto(out: &mut Clock, marks: &[(i64, Clock)], k: i64) {
    if marks.is_empty() {
        return;
    }
    let i = marks.partition_point(|m| m.0 < k);
    out.join(&marks[i.min(marks.len() - 1)].1);
}
