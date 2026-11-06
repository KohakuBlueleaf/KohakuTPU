//! Symbolic cross-node events and receiver-scoped slot leases.

use std::collections::{BTreeMap, BTreeSet};
use std::ops::Range;

use indexmap::IndexMap;

use super::sync::{Access, Stage};
use crate::ir::{Error, Result};

/// Backend ABI facts, independent of the hardware slot count.
#[derive(Clone, Debug)]
pub struct BackendSyncFacts {
    pub revision: &'static str,
    pub reserved_slots: BTreeSet<i64>,
    pub available_slots: BTreeSet<i64>,
}

impl BackendSyncFacts {
    pub fn firmware(slots: i64) -> Self {
        Self {
            revision: "firmware-slots-v1",
            reserved_slots: BTreeSet::new(),
            available_slots: (0..slots).collect(),
        }
    }

    /// `kos/abi.h`: messages 0..3, credits 4..7, barrier 8.
    pub fn os(slots: i64) -> Self {
        Self {
            revision: "kos-slots-v1",
            reserved_slots: (0..9.min(slots)).collect(),
            available_slots: (9..slots).collect(),
        }
    }

    pub fn common(slots: i64) -> Self {
        let firmware = Self::firmware(slots);
        let os = Self::os(slots);
        Self {
            revision: "common-slots-v1",
            reserved_slots: firmware
                .reserved_slots
                .union(&os.reserved_slots)
                .copied()
                .collect(),
            available_slots: firmware
                .available_slots
                .intersection(&os.available_slots)
                .copied()
                .collect(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct EventId(pub String);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind {
    MemoryRelease,
    AccumulatorReady,
    PeerComplete,
    BankCredit,
}

/// Lifetime endpoints are proven happens-before epochs, not source positions.
/// The end includes consumption and sender quiescence.
#[derive(Clone, Debug)]
pub struct EventSpec {
    pub id: EventId,
    pub producers: Vec<i64>,
    pub consumers: Vec<i64>,
    pub released_regions: Vec<Access>,
    pub acquire_regions: Vec<Access>,
    pub kind: EventKind,
    pub count: i64,
    pub lifetime: Range<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SlotLease {
    pub event: EventId,
    pub receiver: i64,
    pub slot: i64,
    pub lifetime: Range<u64>,
}

/// Interval colouring independently for each receiving sysnode.
pub fn allocate_slots(events: &[EventSpec], facts: &BackendSyncFacts) -> Result<Vec<SlotLease>> {
    let mut pending = Vec::new();
    let mut ids = BTreeSet::new();
    for event in events {
        if !ids.insert(event.id.clone())
            || event.lifetime.is_empty()
            || event.count <= 0
            || event.producers.is_empty()
            || event.producers.iter().any(|&node| node < 0)
            || event.consumers.is_empty()
        {
            return Err(Error::new(
                "L2.ORDER.009",
                "invalid event identity, count or lifetime",
            ));
        }
        let mut receivers = BTreeSet::new();
        for &receiver in &event.consumers {
            if receiver < 0 || !receivers.insert(receiver) {
                return Err(Error::new("L2.ORDER.009", "invalid event receiver"));
            }
            pending.push((receiver, event));
        }
    }
    pending
        .sort_by(|(a, x), (b, y)| (a, x.lifetime.start, &x.id).cmp(&(b, y.lifetime.start, &y.id)));
    let mut ends: BTreeMap<(i64, i64), u64> = BTreeMap::new();
    let mut leases = Vec::new();
    for (receiver, event) in pending {
        let slot = facts.available_slots.iter().copied().find(|slot| {
            *slot >= 0
                && !facts.reserved_slots.contains(slot)
                && ends
                    .get(&(receiver, *slot))
                    .is_none_or(|end| *end <= event.lifetime.start)
        });
        let Some(slot) = slot else {
            return Err(Error::new(
                "L2.ORDER.009",
                format!(
                    "event {:?}: receiver {receiver} has no available slot lease; phased joins required",
                    event.id,
                ),
            ));
        };
        ends.insert((receiver, slot), event.lifetime.end);
        leases.push(SlotLease {
            event: event.id.clone(),
            receiver,
            slot,
            lifetime: event.lifetime.clone(),
        });
    }
    Ok(leases)
}

/// All accesses covered by a published frontier, including reads.
pub fn release_union(stages: &[Vec<Access>], frontier: usize) -> Vec<Access> {
    stages[..=frontier].iter().flatten().cloned().collect()
}

pub(super) fn symbol(kind: &str, producer: i64, receiver: i64) -> String {
    format!("@{kind}:{producer}:{receiver}@")
}

/// Resolve stream events together, after all node stages have been composed.
/// A stream retains its lease for the entire invocation; replay requires an
/// externally quiescent epoch. Tokens within a stream retain FIFO counting.
pub(super) fn lower_events(
    built: &mut IndexMap<i64, Vec<Stage>>,
    facts: &BackendSyncFacts,
) -> Result<Vec<SlotLease>> {
    let mut specs = BTreeMap::new();
    for stages in built.values() {
        for stage in stages {
            for line in &stage.lines {
                for part in line.split('@').skip(1).step_by(2) {
                    if !part.starts_with("order:")
                        && !part.starts_with("ready:")
                        && !part.starts_with("complete:")
                    {
                        continue;
                    }
                    let fields: Vec<_> = part.split(':').collect();
                    if fields.len() != 3 {
                        return Err(Error::new("L2.ORDER.009", "invalid symbolic event"));
                    }
                    let parse = |s: &str| {
                        s.parse::<i64>()
                            .map_err(|_| Error::new("L2.ORDER.009", "invalid symbolic event node"))
                    };
                    let producer = parse(fields[1])?;
                    let receiver = parse(fields[2])?;
                    let id = EventId(format!("@{part}@"));
                    specs.entry(id.clone()).or_insert(EventSpec {
                        id,
                        producers: vec![producer],
                        consumers: vec![receiver],
                        released_regions: vec![],
                        acquire_regions: vec![],
                        kind: if fields[0] == "order" {
                            EventKind::MemoryRelease
                        } else if fields[0] == "complete" {
                            EventKind::PeerComplete
                        } else {
                            EventKind::AccumulatorReady
                        },
                        count: 1,
                        lifetime: 0..1,
                    });
                }
            }
        }
    }
    let leases = allocate_slots(&specs.into_values().collect::<Vec<_>>(), facts)?;
    for stages in built.values_mut() {
        for stage in stages {
            for line in &mut stage.lines {
                for lease in &leases {
                    *line = line.replace(&lease.event.0, &lease.slot.to_string());
                }
            }
        }
    }
    Ok(leases)
}
