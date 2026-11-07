//! Opt-in retained-state census, independent of proof state and budgets.

use std::collections::BTreeMap;

use crate::model::clock::Clock;

use super::fx::{FxHashMap, FxHashSet};
use super::Sim;

#[derive(Default)]
pub(crate) struct Retained {
    records: BTreeMap<String, usize>,
    objects: FxHashSet<(&'static str, usize)>,
    bases: FxHashMap<usize, usize>,
    clocks: usize,
}

impl Retained {
    pub fn add(&mut self, kind: impl Into<String>, count: usize) {
        *self.records.entry(kind.into()).or_default() += count;
    }

    pub fn object(&mut self, kind: &'static str, address: usize) -> bool {
        if !self.objects.insert((kind, address)) {
            return false;
        }
        self.add(kind, 1);
        true
    }

    pub fn clock(&mut self, clock: &Clock) {
        self.clocks += 1;
        let (address, capacity) = clock.storage();
        self.bases.entry(address).or_insert(capacity);
    }
}

impl Sim<'_> {
    pub(crate) fn retained_stats(&self, context: &str) {
        let mut stats = Retained::default();
        for proc in &self.procs {
            stats.clock(&proc.clock);
        }
        for counter in &self.counters {
            for (owner, marks) in counter.histories() {
                stats.add(format!("counter/{}/{owner}", counter.name), marks.len());
                for (_, clock) in marks {
                    stats.clock(clock);
                }
            }
        }
        self.card.dram.retained(&mut stats);
        self.card.visibility.retained(&mut stats);
        self.card.packages.retained(&mut stats);
        self.card.values.retained(&mut stats);
        stats.add(
            "initial_bytes",
            self.card.context.initial.values().map(Vec::len).sum(),
        );
        for unit in &self.units {
            unit.mem.retained(&mut stats);
            match &unit.kind {
                super::Kinded::Mg(cluster) => cluster.retained(&mut stats),
                super::Kinded::Vc(vector) => vector.retained(&mut stats),
                super::Kinded::Sr(_) => {}
            }
            stats.add(format!("instructions/{}", unit.name), unit.words.len());
            for word in &unit.words {
                stats.clock(&word.given);
            }
            stats.add(format!("landings/{}", unit.name), unit.landings.len());
            for (_, sent, landed) in &unit.landings {
                stats.clock(sent);
                stats.clock(landed);
            }
        }
        for node in &self.nodes {
            node.retained(&mut stats);
        }
        for arrivals in self.shares.values().flat_map(|group| group.values()) {
            stats.add("shared_fill_arrivals", arrivals.len());
            for clock in arrivals {
                stats.clock(clock);
            }
        }
        for window in &self.windows {
            stats.clock(&window.mine);
        }
        stats.add("fill_windows", self.windows.len());
        stats.add("acks", self.acks.len());
        for (_, _, sent, landed) in &self.acks {
            stats.clock(sent);
            stats.clock(landed);
        }
        stats.add("far_adds", self.far_adds.len());
        for (_, _, sent) in &self.far_adds {
            stats.clock(sent);
        }
        stats.add("decoded_words", self.core_words.len());
        let (nodes, unique, cache, words) = self.symbolic.storage();
        stats.add("symbolic_nodes", nodes);
        stats.add("symbolic_unique", unique);
        stats.add("symbolic_cache", cache);
        stats.add("symbolic_words", words);
        eprintln!(
            "verify retained {context}: events={} procs={} clock_handles={} clock_bases={} clock_capacity_bytes={} records={:?}",
            self.events,
            self.procs.len(),
            stats.clocks,
            stats.bases.len(),
            stats.bases.values().sum::<usize>() * std::mem::size_of::<u32>(),
            stats.records
        );
    }
}
