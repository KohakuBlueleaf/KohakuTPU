//! Decompose physical-memory obligations by their *proved* happens-before
//! relation. A component bounds all its starts from below and completions from
//! above. An access ordered with that envelope cannot race any member. Otherwise
//! it joins the component and is checked against its retained interval histories.
//!
//! A quiescent launch join separates these components without any assertion about
//! source syntax. Components may be finer than launches. Delayed fetches/engine
//! operations can bridge components: merge their budgets and check the access,
//! rather than assuming that visitation order or a local barrier orders them.
//! Keep old components until the conservative future-access floor retires them.

use super::{Footprint, GlobalDram, LIMIT};
use crate::model::clock::Clock;
use crate::model::finding::{Finding, Kind};

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
                // The floor includes active processes, pending engines and
                // deferred physical fetch clocks. Never substitute CPU PCs.
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
                    // Retiring some histories does NOT refund component work.
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
    ) {
        match self {
            Self::Legacy(proof) => proof.access(
                out, footprint, write, start, done, owner, what, special, byte_strobes,
            ),
            Self::Composed(proof) => proof.access(
                out, footprint, write, start, done, owner, what, special, byte_strobes,
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
    // Merging components shares their persistent histories; it never replays
    // old accesses (nor assumes append order across independent owners).
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
    ) {
        if self.refused {
            return;
        }
        let mut current = Component {
            earliest: start.clone(),
            latest: done.clone(),
            work: 0,
            shards: Vec::new(),
        };
        // These are proof summaries, not clocks joined into execution. Keep
        // ordered components for a later-visited operation with an older start.
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
            self.exhausted(out);
            return;
        }
        if current.shards.is_empty() {
            current.shards.push(Shard {
                memory: GlobalDram::default(),
                earliest: start.clone(),
                latest: done.clone(),
            });
        }
        for (index, shard) in current.shards.iter_mut().enumerate() {
            // Store the new access exactly once. Other shards supply only
            // cross-component comparisons. Ordered shard envelopes need none.
            if index != 0 && shard.ordered(start, done) {
                continue;
            }
            let before = shard.memory.work;
            shard.memory.access_inner(
                out, footprint.clone(), write, start, done, owner, what.clone(),
                special, byte_strobes, index == 0,
            );
            current.work = current.work.saturating_add(shard.memory.work - before);
            crate::composition::charge(current.work);
            if shard.memory.refused {
                self.refused = true;
                return;
            }
            if current.work > LIMIT {
                self.exhausted(out);
                return;
            }
            if index == 0 {
                shard.earliest = shard.earliest.meet(start);
                shard.latest.join(done);
            }
        }
        self.live.push(current);
    }

    fn exhausted(&mut self, out: &mut Vec<Finding>) {
        self.refused = true;
        out.push(Finding::new(
            Kind::Model,
            "shared physical memory",
            "global DRAM proof budget exhausted in an unordered component",
        ));
    }
}
