//! Branch-local lower bounds for future physical accesses and publications.

use crate::model::clock::Clock;

use super::{package_memory::Location, Kinded, Pk, Sim};

impl Sim<'_> {
    pub(super) fn retire_memory(&mut self) {
        let mut floor: Option<Clock> = None;
        let mut include = |clock: &Clock| {
            floor = Some(match floor.take() {
                Some(previous) => previous.meet(clock),
                None => clock.clone(),
            });
        };
        for (p, process) in self.procs.iter().enumerate() {
            let active = match process.kind {
                Pk::Port => false,
                Pk::MgSeq(u) => self.mg(u).next_word() < self.units[u].words.len(),
                Pk::VcCu(u) => {
                    self.vc(u).running || self.vc(u).next_word() < self.units[u].words.len()
                }
                Pk::SrShell(u) => !self.sr_done(u),
                _ => !self.done(p),
            };
            if active {
                include(&process.clock);
            }
        }
        for node in &self.nodes {
            // A completed retained part is not the end of this process. Its
            // next package may access memory at this still-low causal floor.
            if self.part_proof {
                include(&self.procs[node.proc].clock);
            }
            if let Some(p) = node.pending_engine() {
                include(&self.procs[p].clock);
            }
        }
        for unit in &self.units {
            let first = match &unit.kind {
                Kinded::Mg(c) => c.next_word(),
                Kinded::Vc(v) => v.next_word(),
                // SR physical instruction fetches are not deferred.
                Kinded::Sr(_) => unit.words.len(),
            };
            for word in &unit.words[first..] {
                if matches!(word.source, Some(Location::Physical(_))) {
                    include(&word.given);
                }
            }
        }
        if let Some(floor) = floor {
            self.card.dram.retire(&floor);
            self.card.visibility.retire(&floor);
        }
    }
}
