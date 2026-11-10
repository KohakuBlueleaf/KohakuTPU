//! Exact state transfer for a certified retained-package boundary.
use std::collections::BTreeMap;
use super::*;
use crate::sim::Totals;

/// Allocated only by install_part. Ordinary L1/L0 checks must not carry and
/// clone these maps at every branch/join snapshot.
#[derive(Clone, Default, PartialEq, Eq)]
pub(super) struct PartState {
    pub(super) identity: usize,
    pub(super) join_entries: HashSet<usize>,
    pub(super) tail: usize,
    units: BTreeMap<usize, (i64, i64, i64)>,
}

impl Sim<'_> {
    /// Replace executable storage only. Unit state, counters and their clocks,
    /// posted writes, private-package mutations and symbolic memory stay alive.
    pub(crate) fn install_part(
        &mut self, inputs: &BTreeMap<u32, Package>, identity: usize,
        channel: Option<u16>, checkpoints: &BTreeMap<u32, (Vec<usize>, usize)>,
    ) -> Result<(), String> {
        if self.nodes.is_empty() {
            for (&mesh, package) in inputs {
                self.add_node(mesh, vec![package.clone()], false);
            }
        } else {
            // windows records HISTORICAL non-leading shared-fill arrivals, not
            // pending execution. Keep it for port_findings; done checks the live
            // sequencers, including an unreleased shared-fill rendezvous.
            if (0..self.procs.len()).any(|p| !self.done(p)) {
                return Err("part transition has pending execution".into());
            }
            for (&mesh, package) in inputs {
                let i = *self.node_of.get(&mesh).ok_or("part lost a participant")?;
                let units: Vec<_> = package.units.iter().map(|s| self.unit((mesh, s.x, s.y))).collect();
                for &unit in &units {
                    let u = unit.ok_or("part names an absent physical unit")?;
                    if self.feeder.insert(u, i).is_some_and(|owner| owner != i) {
                        return Err("retained part changed physical unit dispatcher".into());
                    }
                }
                let nd = &mut self.nodes[i];
                let part = nd.part.as_mut().ok_or("missing retained node state")?;
                // Save accounting by physical identity, not local header index.
                for (slot, unit) in nd.units.iter().enumerate() {
                    if let Some(u) = unit {
                        part.units.insert(*u, (nd.sent[slot], nd.expected[slot], nd.acks_taken[slot]));
                    }
                }
                nd.units = units;
                let n = nd.units.len();
                nd.sent = vec![0; n];
                nd.expected = vec![0; n];
                nd.acks_taken = vec![0; n];
                for (slot, unit) in nd.units.iter().enumerate() {
                    if let Some(u) = unit {
                        let (sent, expected, acks) = part.units.get(u).copied().unwrap_or_default();
                        nd.sent[slot] = sent;
                        nd.expected[slot] = expected;
                        nd.acks_taken[slot] = acks;
                    }
                }
                nd.credit = package.units.iter().map(|s| if s.credit == 0 {
                    self.t.facts.unbounded
                } else { i64::from(s.credit) }).collect();
                nd.fetch = package.units.iter().map(|s| s.fetch >> 16 != 0).collect();
                nd.engine.rebind(&nd.sent);
                // END already clears regs/flush and drains execution. Keep
                // mover descriptors, publication clocks and all unit state.
                if nd.fetched != 0 || nd.armed {
                    return Err("part END left a partial fetch/wait".into());
                }
                nd.host = 0;
                nd.ends.clear();
                nd.at = 0;
                nd.branches.clear();
                nd.log = None;
                nd.frontiers.clear();
                nd.pkgs = vec![package.clone()];
                nd.raw_steps = vec![package.steps.clone()];
                nd.steps.clear();
                nd.segments.clear();
                nd.step_targets.clear();
                nd.step_sources.clear();
                nd.payload_bases.clear();
                nd.payloads.clear();
                nd.locations.clear();
                nd.load(package, 0);
                for k in 0..nd.payloads.len() {
                    nd.locations.push(Location::Private {
                        mesh, run: identity, section: Section::Payloads, offset: k as u64 * 32,
                    });
                }
            }
        }
        // The next discovery/proof forks share immutable incoming counter
        // prefixes. Freeze before cloning, not Arc::make_mut after cloning a
        // monolithic history (which would still copy the entire old prefix).
        for counter in &mut self.counters { counter.share_history(); }
        self.part_channel = channel;
        for node in &mut self.nodes {
            let (entries, tail) = &checkpoints[&node.mesh];
            let part = node.part.get_or_insert_with(Default::default);
            part.join_entries = entries.iter().map(|raw| node.step_targets[&(0, *raw)]).collect();
            part.tail = node.step_targets.get(&(0, *tail)).copied().unwrap_or(node.steps.len());
            part.identity = identity;
            for location in &mut node.locations {
                if let Location::Private { run, .. } = location { *run = identity; }
            }
        }
        self.path.clear();
        self.choices.clear();
        self.events = 0;
        self.next_retirement = 1024;
        self.next_stats = 1024;
        self.feedback = Default::default();
        Ok(())
    }

    pub(super) fn part_join_drain(&self, i: usize) -> Result<(), String> {
        let nd = &self.nodes[i];
        let acquired = &self.procs[nd.proc].clock;
        let includes = |required: &Clock| (0..self.procs.len()).all(|p| required.at(p) <= acquired.at(p));
        for (slot, unit) in nd.units.iter().enumerate() {
            if let Some(u) = unit {
                let unit = &self.units[*u];
                let sent = nd.sent[slot];
                if !self.counters[unit.done].reached(sent)
                    || !includes(&self.clock_to(unit.done, sent, &[(unit.completer, sent)])) {
                    return Err("retained join entered without acquiring its local unit drain".into());
                }
            }
        }
        if nd.moves.last().is_some_and(|done| !includes(done)) {
            return Err("retained join entered without acquiring its local mover drain".into());
        }
        Ok(())
    }

    pub(crate) fn part_totals(&mut self, totals: Option<Totals>) {
        for c in &mut self.counters {
            let owners = totals.as_ref().map(|all| {
                all.get(&c.name).into_iter().flat_map(|owners| owners.iter())
                    .map(|(name, n)| (name.clone(), *n)).collect()
            });
            c.replace_totals(owners);
        }
        self.totals = totals;
        self.feedback = Default::default();
        if self.totals.is_some() { self.prepare_feedback(); }
    }

    pub(crate) fn part_host_join(&mut self) {
        // Exactly the original host collection policy, only between epochs.
        let mut clock = Clock::new();
        for node in &self.nodes { clock.join(&self.procs[node.proc].clock); }
        let processes: Vec<_> = self.nodes.iter().map(|n| n.proc).collect();
        for p in processes { self.tick(p, &[clock.clone()]); }
    }

    pub(crate) fn part_package(&self, mesh: u32, input: &Package) -> Package {
        let node = &self.nodes[self.node_of[&mesh]];
        let mut package = input.clone();
        package.steps = node.raw_steps[0].clone();
        package.payloads = node.payloads.clone();
        package
    }

    pub(crate) fn part_findings(&self) -> Vec<crate::Finding> {
        let mut out = self.findings.clone();
        out.extend(self.memory_findings.clone());
        if !out.iter().any(|f| f.kind == Kind::Deadlock) {
            out.extend(self.rest());
            out.extend(self.port_findings());
            if !out.iter().any(|f| f.kind.refuses()) {
                out.extend(self.sequence_findings.clone());
            }
        }
        out
    }

    pub(crate) fn part_diagnostics_clear(&mut self) {
        // Do not clear DRAM components, values, visibility or counter prefixes.
        self.findings.clear();
        self.memory_findings.clear();
        self.sequence_findings.clear();
    }
}
