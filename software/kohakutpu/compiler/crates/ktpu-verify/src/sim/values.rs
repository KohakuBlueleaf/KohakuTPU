//! Address-bearing values with publication provenance. Unknown writes kill
//! known bytes; only a read after the defining event may resolve a value.

use std::collections::BTreeMap;
use std::sync::Arc;

use crate::check::memory::MemoryContext;
use crate::model::clock::Clock;

use super::dram::Footprint;
use super::Sim;

#[derive(Clone, Debug, PartialEq, Eq)]
struct Byte {
    value: u8,
    publication: Option<Arc<(Clock, usize)>>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct Values {
    bytes: BTreeMap<u64, Byte>,
    modified: BTreeMap<u64, u64>,
}

impl Values {
    pub(crate) fn retained(&self, stats: &mut super::stats::Retained) {
        stats.add("known_bytes", self.bytes.len());
        stats.add("modified_ranges", self.modified.len());
        for byte in self.bytes.values() {
            if let Some(event) = &byte.publication {
                if stats.object("value_publications", Arc::as_ptr(event) as usize) {
                    stats.clock(&event.0);
                }
            }
        }
    }

    pub fn invalidate(&mut self, ranges: &Footprint) {
        for &(lo, hi) in ranges {
            if lo >= hi {
                continue;
            }
            let mut begin = lo;
            let mut end = hi;
            if let Some((&at, &to)) = self.modified.range(..=lo).next_back() {
                if to >= lo {
                    begin = at;
                    end = end.max(to);
                }
            }
            let overlaps: Vec<_> = self
                .modified
                .range(begin..=hi)
                .map(|(&at, &to)| (at, to))
                .collect();
            for (at, to) in overlaps {
                end = end.max(to);
                self.modified.remove(&at);
            }
            self.modified.insert(begin, end);
            let keys: Vec<_> = self.bytes.range(lo..hi).map(|(&at, _)| at).collect();
            for at in keys {
                self.bytes.remove(&at);
            }
        }
    }

    pub fn put(&mut self, at: u64, bytes: &[u8], publication: Option<(Clock, usize)>) {
        let publication = publication.map(Arc::new);
        for (offset, &value) in bytes.iter().enumerate() {
            self.bytes.insert(
                at + offset as u64,
                Byte {
                    value,
                    publication: publication.clone(),
                },
            );
        }
    }

    pub fn modified(&self, at: u64) -> bool {
        self.modified
            .range(..=at)
            .next_back()
            .is_some_and(|(_, &hi)| at < hi)
    }

    pub fn get(&self, at: u64, n: usize, reader: &Clock) -> Option<u64> {
        let mut value = 0;
        for offset in 0..n {
            let byte = self.bytes.get(&(at + offset as u64))?;
            if let Some(event) = &byte.publication {
                if !event.0.before(reader, event.1) {
                    return None;
                }
            }
            value |= u64::from(byte.value) << (offset * 8);
        }
        Some(value)
    }

    /// After the host's all-part terminal join, visible values are initial
    /// values of the next scheduling pass. Unacknowledged bytes stay unknown.
    pub fn after(&self, terminal: &Clock) -> Self {
        let bytes = self
            .bytes
            .iter()
            .filter_map(|(&at, byte)| {
                let visible = byte
                    .publication
                    .as_ref()
                    .is_none_or(|event| event.0.before(terminal, event.1));
                visible.then_some((
                    at,
                    Byte {
                        value: byte.value,
                        publication: None,
                    },
                ))
            })
            .collect();
        Self {
            bytes,
            modified: self.modified.clone(),
        }
    }
}

impl Sim<'_> {
    pub fn physical(&self, at: u64) -> u64 {
        let a = self.t.machine.die.address.runtime();
        if at & (1u64 << a.special_bit) != 0 {
            at
        } else {
            at & !(((1u64 << a.mesh_bits) - 1) << a.mesh_shift) & !(1u64 << a.reserved_bit)
        }
    }

    pub fn initialize_memory(&mut self, context: &MemoryContext) {
        self.card.context = context.clone();
        for (&base, bytes) in &context.initial {
            let at = self.physical(base);
            self.card.values.put(at, bytes, None);
        }
    }

    pub fn stored_value(&mut self, mesh: u32, issuer: usize, address: u64, bytes: &[u8]) {
        // L0.STEP.048: a remote DRAM STORE has remote publication, not
        // the last local response of this CPU. Use the access owner's route.
        let destination = self.write_destination(mesh, None, address);
        let publication = self.card.visibility.publication(issuer, destination);
        if let Some(publication) = publication {
            let at = self.physical(address);
            self.card.values.put(at, bytes, Some(publication));
        }
    }
}
