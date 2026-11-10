//! Decompose physical-memory obligations by their proved happens-before relation.
//! Ordered envelopes cannot race. Unordered ones merge their histories and spent
//! work; only the conservative future-access floor may retire them.

use super::{Footprint, GlobalDram, LIMIT};
use crate::model::clock::Clock;
use crate::model::finding::{Finding, Kind};

/// Constructed only on refusal and returned to the caller, never stored in the
/// simulator's semantic state. Historical access counts/launch IDs are untracked.
pub(crate) struct Failure {
    pub work: usize,
    pub owner: usize,
    pub what: String,
    pub start: Clock,
    pub done: Clock,
    pub latest: Clock,
    /// An existing shard envelope, before the triggering access updates it.
    /// This is not the former heaviest-component diagnostic or a source launch.
    pub previous: Option<(Clock, Clock)>,
}

#[derive(Clone, PartialEq, Eq)]
pub(crate) enum Proof {
    Legacy(GlobalDram),
    Composed(Components),
}

impl Default for Proof {
    fn default() -> Self {
        if crate::composition::active() {
            Self::Composed(Components::default())
        } else {
            Self::Legacy(GlobalDram::default())
        }
    }
}

impl Proof {
    pub fn retained(&self, stats: &mut crate::sim::stats::Retained) {
        match self {
            Self::Legacy(proof) => proof.retained(stats),
            Self::Composed(proof) => {
                stats.add("dram_components", proof.live.len());
                for component in &proof.live {
                    for shard in &component.shards {
                        shard.memory.retained(stats);
                    }
                }
            }
        }
    }

    pub fn retire(&mut self, floor: &Clock) {
        match self {
            Self::Legacy(proof) => proof.retire(floor),
            Self::Composed(proof) => {
                // Includes pending engines and deferred physical fetch origins.
                proof.live.retain_mut(|component| {
                    if component.latest.covered_by(floor) {
                        return false;
                    }
                    component.shards.retain_mut(|shard| {
                        if shard.latest.covered_by(floor) {
                            return false;
                        }
                        shard.memory.retire(floor);
                        true
                    });
                    // History retirement never refunds already charged work.
                    true
                });
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn access(
        &mut self,
        out: &mut Vec<Finding>,
        footprint: Result<Footprint, String>,
        write: bool,
        start: &Clock,
        done: &Clock,
        owner: usize,
        what: String,
        special: u64,
        byte_strobes: bool,
    ) -> Option<Failure> {
        match self {
            Self::Legacy(proof) => {
                proof.access(
                    out,
                    footprint,
                    write,
                    start,
                    done,
                    owner,
                    what,
                    special,
                    byte_strobes,
                );
                None
            }
            Self::Composed(proof) => proof.access(
                out,
                footprint,
                write,
                start,
                done,
                owner,
                what,
                special,
                byte_strobes,
            ),
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
struct Shard {
    memory: GlobalDram,
    earliest: Clock,
    latest: Clock,
}

impl Shard {
    fn ordered(&self, start: &Clock, done: &Clock) -> bool {
        self.latest.covered_by(start) || done.covered_by(&self.earliest)
    }
}

#[derive(Clone, PartialEq, Eq)]
struct Component {
    earliest: Clock,
    latest: Clock,
    work: usize,
    // Merge persistent histories without replaying accesses or assuming an
    // append order across independent owners.
    shards: Vec<Shard>,
}

impl Component {
    fn ordered(&self, start: &Clock, done: &Clock) -> bool {
        self.latest.covered_by(start) || done.covered_by(&self.earliest)
    }
}

#[derive(Clone, Default, PartialEq, Eq)]
pub(crate) struct Components {
    live: Vec<Component>,
    refused: bool,
}

impl Components {
    #[allow(clippy::too_many_arguments)]
    fn access(
        &mut self,
        out: &mut Vec<Finding>,
        footprint: Result<Footprint, String>,
        write: bool,
        start: &Clock,
        done: &Clock,
        owner: usize,
        what: String,
        special: u64,
        byte_strobes: bool,
    ) -> Option<Failure> {
        if self.refused {
            return None;
        }
        let mut current = Component {
            earliest: start.clone(),
            latest: done.clone(),
            work: 0,
            shards: Vec::new(),
        };
        self.live.retain_mut(|previous| {
            if previous.ordered(start, done) {
                return true;
            }
            current.earliest = current.earliest.meet(&previous.earliest);
            current.latest.join(&previous.latest);
            current.work = current.work.saturating_add(previous.work);
            current.shards.append(&mut previous.shards);
            false
        });
        crate::composition::charge(current.work);
        if current.work > LIMIT {
            return Some(self.exhausted(out, &current, owner, &what, start, done, None));
        }
        if current.shards.is_empty() {
            current.shards.push(Shard {
                memory: GlobalDram::default(),
                earliest: start.clone(),
                latest: done.clone(),
            });
        }
        for index in 0..current.shards.len() {
            let shard = &mut current.shards[index];
            if index != 0 && shard.ordered(start, done) {
                continue;
            }
            let before = shard.memory.work;
            shard.memory.access_inner(
                out,
                footprint.clone(),
                write,
                start,
                done,
                owner,
                what.clone(),
                special,
                byte_strobes,
                index == 0,
            );
            current.work = current.work.saturating_add(shard.memory.work - before);
            crate::composition::charge(current.work);
            if shard.memory.refused {
                // Failure-only lookup. Preserve simultaneous race/line findings.
                if let Some(at) = out.iter().rposition(|f| {
                    f.kind == Kind::Model
                        && f.at == "shared physical memory"
                        && f.text == "global DRAM proof budget exhausted"
                }) {
                    out.remove(at);
                }
                return Some(self.exhausted(out, &current, owner, &what, start, done, Some(index)));
            }
            if current.work > LIMIT {
                return Some(self.exhausted(out, &current, owner, &what, start, done, Some(index)));
            }
            if index == 0 {
                shard.earliest = shard.earliest.meet(start);
                shard.latest.join(done);
            }
        }
        self.live.push(current);
        None
    }

    #[allow(clippy::too_many_arguments)]
    fn exhausted(
        &mut self,
        out: &mut Vec<Finding>,
        current: &Component,
        owner: usize,
        what: &str,
        start: &Clock,
        done: &Clock,
        triggering_shard: Option<usize>,
    ) -> Failure {
        self.refused = true;
        // On an inner-shard refusal that shard's envelope is still unchanged.
        // On merge exhaustion all envelopes are unchanged; choose the first
        // heaviest unordered shard. No diagnostic selection happens on success.
        let previous = match triggering_shard {
            Some(index) => current.shards.get(index),
            None => current
                .shards
                .iter()
                .filter(|s| !s.ordered(start, done))
                .fold(None::<&Shard>, |best, s| {
                    if best.is_none_or(|old| s.memory.work > old.memory.work) {
                        Some(s)
                    } else {
                        best
                    }
                }),
        }
        .filter(|s| s.earliest != *start || s.latest != *done)
        .map(|s| (s.earliest.clone(), s.latest.clone()));
        out.push(Finding::new(
            Kind::Model,
            "shared physical memory",
            "global DRAM proof budget exhausted in an unordered component",
        ));
        Failure {
            work: current.work,
            owner,
            what: what.into(),
            start: start.clone(),
            done: done.clone(),
            latest: current.latest.clone(),
            previous,
        }
    }
}
