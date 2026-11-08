//! Memory response clocks are independent of execution retirement.
//! CU ACK_FENCE exports local write responses with completion signals.
//! A fenced ring releases posted writes at its destination, not arbitrary
//! compute work still inside the issuing mesh (control-registers §Interlink).

use std::collections::BTreeMap;

use crate::model::clock::Clock;

use super::dram::Footprint;
use super::{Pk, Sim};

mod history;
use history::Marks;

type Key = (usize, u32);

#[derive(Clone, PartialEq, Eq)]
struct Responses {
    port: usize,
    // Issuer events are monotone. Each response clock contains every earlier
    // response on this port, so a prefix is represented by its last clock.
    marks: Marks,
}

impl Responses {
    fn through(&self, issued: u32) -> Option<&Clock> {
        self.marks.through(issued)
    }

    fn last(&self) -> Option<&Clock> {
        self.marks.last()
    }
}

#[derive(Clone, Default, PartialEq, Eq)]
pub(crate) struct Visibility {
    streams: BTreeMap<Key, Responses>,
    // Completion visits only the unit's local response streams. Release visits
    // streams of its issuing mesh; the bool distinguishes CU from CPU/mover.
    local: BTreeMap<usize, Vec<Key>>,
    meshes: BTreeMap<u32, Vec<(Key, bool)>>,
}

impl Visibility {
    pub fn retained(&self, stats: &mut super::stats::Retained) {
        for (&(issuer, destination), responses) in &self.streams {
            let count = responses.marks.retained(stats);
            stats.add(format!("responses/{issuer}/{destination}"), count);
        }
    }

    pub fn retire(&mut self, floor: &Clock) {
        for (&(issuer, _), responses) in &mut self.streams {
            responses.marks.retire(floor.at(issuer));
        }
    }

    pub fn publication(&self, issuer: usize, destination: u32) -> Option<(Clock, usize)> {
        let responses = self.streams.get(&(issuer, destination))?;
        Some((responses.last()?.clone(), responses.port))
    }
}

impl Sim<'_> {
    /// L0.STEP.048 / ISA.MV.002: CPU and mover writes route by mesh bits
    /// for both DRAM and staging (mag.v:995-998; mag_ilink.v:359-362).
    /// Their remote B is posted. Ordinary CU DRAM requests instead alias
    /// locally (memory-protocol §8). Physical identity is normalized separately.
    /// Response ordering and known-value publication must use the same route.
    pub fn write_destination(&self, mesh: u32, unit: Option<usize>, at: u64) -> u32 {
        let address = self.t.machine.die.address.runtime();
        if unit.is_some() && at & (1u64 << address.special_bit) == 0 {
            mesh
        } else {
            ((at >> address.mesh_shift) & ((1u64 << address.mesh_bits) - 1)) as u32
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn memory_write(
        &mut self,
        mesh: u32,
        unit: Option<usize>,
        footprint: Result<Footprint, String>,
        start: &Clock,
        retired: &Clock,
        issuer: usize,
        what: String,
    ) -> Clock {
        let mut acknowledged = retired.clone();
        let ranges = match footprint {
            Ok(ranges) => ranges,
            Err(why) => {
                self.dram_access(Err(why), true, start, retired, issuer, what);
                return acknowledged;
            }
        };
        let mut destinations = BTreeMap::<u32, Footprint>::new();
        for range in ranges {
            let destination = self.write_destination(mesh, unit, range.0);
            destinations.entry(destination).or_default().push(range);
        }
        for (destination, ranges) in destinations {
            // memory-protocol §7.1, mag_mem_port.v:605-634: a source's
            // completed write bursts keep their order at its fixed memory port.
            // Separate destinations retain separate response streams; sharing
            // an Xache home does not merge independent sources (§7.2).
            let key = (issuer, destination);
            let port = match self.card.visibility.streams.get(&key) {
                Some(responses) => responses.port,
                None => {
                    let port = self.add_proc(
                        Pk::Port,
                        format!(
                            "{} memory responses at {destination}",
                            self.procs[issuer].name
                        ),
                    );
                    let visibility = &mut self.card.visibility;
                    visibility.streams.insert(
                        key,
                        Responses {
                            port,
                            marks: Marks::default(),
                        },
                    );
                    visibility
                        .meshes
                        .entry(mesh)
                        .or_default()
                        .push((key, unit.is_some()));
                    if let Some(u) = unit.filter(|_| destination == mesh) {
                        visibility.local.entry(u).or_default().push(key);
                    }
                    port
                }
            };
            let committed = self.tick(port, std::slice::from_ref(retired));
            // rv64_nport emits lane strobes; mag_ilink and mag_dram_port
            // carry them through remote routing and DRAM width conversion.
            let byte_strobes = matches!(self.procs[issuer].kind, Pk::Node(_));
            self.dram_access_masked(
                Ok(ranges),
                true,
                start,
                &committed,
                port,
                what.clone(),
                byte_strobes,
            );
            if destination == mesh {
                acknowledged.join(&committed);
            }
            self.card
                .visibility
                .streams
                .get_mut(&key)
                .expect("response stream")
                .marks
                .push((retired.at(issuer), committed));
        }
        acknowledged
    }

    /// L0.STEP.012 / compute-unit-port §3.3: CU_SIGNAL follows tracked
    /// local MEM_WR_ACKs. ISA.VC.128/.154 and ISA.MG.030/.032 distinguish
    /// this signal from local retirement; neither the core nor the sequencer
    /// acquires it merely by advancing to its next instruction.
    pub fn unit_completed(&mut self, u: usize, retired: &Clock) {
        let mut observed = retired.clone();
        let visibility = &self.card.visibility;
        for &key in visibility.local.get(&u).into_iter().flatten() {
            if let Some(clock) = visibility.streams[&key].through(retired.at(key.0)) {
                observed.join(clock);
            }
        }
        let p = self.units[u].completer;
        let clock = self.tick(p, &[observed]);
        let (done, recv) = (self.units[u].done, self.units[u].recv);
        self.counters[done].add(1, &clock, p);
        self.counters[recv].add(1, &clock, p);
    }

    /// L0.STEP.031/.048: a fenced ring orders posted writes at arrival.
    /// Publication does not give the sending CPU the receiver's arrival clock,
    /// and cannot cover a CU whose retirement the sender has not acquired.
    pub fn release_clock(&self, mesh: u32, destination: u32, sent: &Clock) -> Clock {
        let mut arrival = sent.clone();
        let visibility = &self.card.visibility;
        for &(key, cu) in visibility.meshes.get(&mesh).into_iter().flatten() {
            let responses = &visibility.streams[&key];
            let clock = if cu {
                if key.1 != destination {
                    continue;
                }
                responses.through(sent.at(key.0))
            } else if key.1 == mesh || key.1 == destination {
                responses.last()
            } else {
                None
            };
            if let Some(clock) = clock {
                arrival.join(clock);
            }
        }
        arrival
    }
}
