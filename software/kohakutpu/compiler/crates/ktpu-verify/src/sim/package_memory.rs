//! Package storage is either a fresh, pinned allocator span or a placed span.
//! Placed spans use the same physical tracker as tensor and protocol accesses.

use std::collections::BTreeMap;

use crate::adapter::u256::U256;
use crate::model::clock::Clock;

use super::memory::{Memory, Space, What};
use super::{Pk, Sim};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum Section {
    Steps,
    Payloads,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Location {
    Physical(u64),
    Private {
        mesh: u32,
        run: usize,
        section: Section,
        offset: u64,
    },
}

impl Location {
    pub fn offset(&self, bytes: u64) -> Self {
        match *self {
            Self::Physical(at) => Self::Physical(at + bytes),
            Self::Private {
                mesh,
                run,
                section,
                offset,
            } => Self::Private {
                mesh,
                run,
                section,
                offset: offset + bytes,
            },
        }
    }
}

#[derive(Clone, Default, PartialEq, Eq)]
pub(crate) struct Packages {
    private: BTreeMap<(u32, usize, Section), Memory>,
}

impl Packages {
    pub(crate) fn retained(&self, stats: &mut super::stats::Retained) {
        stats.add("private_sections", self.private.len());
        for memory in self.private.values() {
            memory.retained(stats);
        }
    }
}

impl Sim<'_> {
    #[allow(clippy::too_many_arguments)]
    pub fn package_access(
        &mut self,
        location: &Location,
        bytes: u64,
        write: bool,
        start: &Clock,
        done: &Clock,
        owner: usize,
        what: String,
    ) {
        match *location {
            Location::Physical(at) => {
                if write {
                    let Pk::Node(node) = self.procs[owner].kind else {
                        self.find(
                            crate::model::finding::Kind::Model,
                            what,
                            "package mutation has no issuing CPU",
                        );
                        return;
                    };
                    let mesh = self.nodes[node].mesh;
                    let ack = self.memory_write(
                        mesh,
                        None,
                        super::dram::interval(at, bytes),
                        start,
                        done,
                        owner,
                        what,
                    );
                    self.procs[owner].clock.join(&ack);
                } else {
                    self.dram_access(
                        super::dram::interval(at, bytes),
                        false,
                        start,
                        done,
                        owner,
                        what,
                    );
                }
            }
            Location::Private {
                mesh,
                run,
                section,
                offset,
            } => {
                let memory = self
                    .card
                    .packages
                    .private
                    .entry((mesh, run, section))
                    .or_insert_with(|| {
                        Memory::new(&format!("sysnode {mesh} package {run} {section:?}"))
                    });
                // Package fetches are 16/32-byte aligned records; PATCH reads
                // and writes complete aligned 64-bit limbs (L0.STEP.053/.072).
                // Section-local limb indices preserve every possible overlap
                // without tracking eight indistinguishable byte histories.
                memory.access(
                    &mut self.findings,
                    Space::L1,
                    (offset / 8..(offset + bytes) / 8).map(|limb| limb as i64),
                    write,
                    done,
                    owner,
                    &What::Burst(what.into()),
                );
            }
        }
    }

    /// Physical stores replace initial instruction/descriptor bytes. A write
    /// with unknown data leaves the instruction unknown, never its old value.
    pub fn package_value(
        &self,
        location: &Location,
        initial: U256,
        bytes: u64,
        clock: &Clock,
    ) -> Result<U256, String> {
        let Location::Physical(at) = *location else {
            return Ok(initial);
        };
        let at = self.physical(at);
        let mut word = initial;
        for byte in 0..bytes {
            if self.card.values.modified(at + byte) {
                let value = self
                    .card
                    .values
                    .get(at + byte, 1, clock)
                    .ok_or("package bytes modified without a visible known value")?;
                word.set_bits(byte as u32 * 8, 8, value);
            }
        }
        Ok(word)
    }

    /// A fetched instruction has consumed its source by execution start.
    pub fn fetched_word(&mut self, u: usize, pc: usize, clock: &Clock, owner: usize) {
        if let Some(location) = self.units[u].words[pc].source.clone() {
            let start = self.units[u].words[pc].given.clone();
            self.package_access(
                &location,
                32,
                false,
                &start,
                clock,
                owner,
                format!("{} fetch word {pc}", self.units[u].name),
            );
        }
    }
}
