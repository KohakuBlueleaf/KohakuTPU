//! What a finished run leaves (L1.VER.007) and how bursts met at the ports
//! (L1.VER.008).

use std::collections::HashMap;

use crate::model::finding::{Finding, Kind};

use super::{Kinded, Sim};

impl Sim<'_> {
    /// Counters left above what their waiters took: a unit's, a node's, a
    /// doorbell or slot some wait reads, or a DATA_RECEIVED that may arrive
    /// after its package ends.
    pub fn rest(&self) -> Vec<Finding> {
        let mut out = Vec::new();
        if crate::mutant::on(super::units::cluster::CHECKER) {
            return out;
        }
        for (u, unit) in self.units.iter().enumerate() {
            let texts = match unit.kind {
                Kinded::Mg(_) => self.mg_rest(u),
                Kinded::Vc(_) => self.vc_rest(u),
                Kinded::Sr(_) => self.sr_rest(u),
            };
            out.extend(
                texts
                    .into_iter()
                    .map(|t| Finding::new(Kind::Rest, unit.name.clone(), t)),
            );
        }
        for &i in self.node_of.values() {
            let name = self.nodes[i].name.clone();
            out.extend(
                self.node_rest(i)
                    .into_iter()
                    .map(|t| Finding::new(Kind::Rest, name.clone(), t)),
            );
        }
        for (name, c) in self.named() {
            let mut parts = name.splitn(3, ':');
            let (Some(kind), Some(mesh), Some(which)) = (parts.next(), parts.next(), parts.next())
            else {
                continue;
            };
            let take = match (kind, which.parse::<i64>()) {
                ("bell", Ok(w)) => super::Take::Bell(w),
                ("slot", Ok(w)) => super::Take::Slot(w),
                _ => continue,
            };
            let Ok(mesh) = mesh.parse::<u32>() else {
                continue;
            };
            let key = (mesh, take);
            let taken = self.taken.get(&key).copied().unwrap_or(0);
            // A count no wait of the program reads only grows: the
            // program's reruns cannot tell.
            if self.waited.contains(&key) && c.value != taken {
                out.push(Finding::new(
                    Kind::Rest,
                    format!("node {mesh}"),
                    format!("{kind} {which}: {} never taken", c.value - taken),
                ));
            }
        }
        // A DATA_RECEIVED belongs to the package whose step sent the burst it
        // answers; the next package counts completions from zero.
        for (node, dst, sent, landed) in &self.acks {
            let nd = &self.nodes[*node];
            let at = sent.at(nd.proc);
            let end = nd.ends.iter().find(|(n, _)| *n >= at).map(|(_, c)| c);
            let port = self.units[*dst].port;
            if end.is_none_or(|e| e.at(port) < landed.at(port)) {
                out.push(Finding::new(
                    Kind::Rest,
                    nd.name.clone(),
                    format!(
                        "a DATA_RECEIVED from {} may arrive after its package ends",
                        self.units[*dst].name
                    ),
                ));
            }
        }
        out
    }

    /// P1: bursts from different senders into one unit land one after the
    /// other (each landing happens before the next sender's send). P2: a
    /// burst into a sharer of a shared fill lands before the lead's arrival
    /// or is sent after the sharer's own. P3: `collisions`.
    pub fn port_findings(&self) -> Vec<Finding> {
        let mut out = Vec::new();
        for unit in &self.units {
            let port = unit.port;
            let mut prev: Option<&(
                usize,
                crate::model::clock::Clock,
                crate::model::clock::Clock,
            )> = None;
            for l in &unit.landings {
                let (owner, sent, _) = l;
                if let Some((po, _, plan)) = prev {
                    if po != owner && plan.at(port) > sent.at(port) {
                        out.push(Finding::new(
                            Kind::Port,
                            unit.name.clone(),
                            format!(
                                "a burst from {} is not ordered after the one from {}: their flits can interleave",
                                self.procs[*owner].name, self.procs[*po].name
                            ),
                        ));
                    }
                }
                prev = Some(l);
            }
        }
        out.extend(self.collisions());
        out.extend(self.far_add_findings());
        for w in &self.windows {
            let Some(lead_at) = self.lead_arrival(&w.group, w.lead, w.k) else {
                continue;
            };
            let unit = &self.units[w.unit];
            let port = unit.port;
            for (owner, sent, landed) in &unit.landings {
                let before = landed.at(port) <= lead_at.at(port);
                let after = w.mine.at(w.me) <= sent.at(w.me);
                if !(before || after) {
                    out.push(Finding::new(
                        Kind::Port,
                        unit.name.clone(),
                        format!(
                            "a burst from {} may queue behind the responses of shared fill {} before this unit reaches it",
                            self.procs[*owner].name, w.k
                        ),
                    ));
                }
            }
        }
        out
    }

    /// P4: a node's adds across nodes go one at a time, whatever their
    /// destinations: each send follows the previous sender's send.
    fn far_add_findings(&self) -> Vec<Finding> {
        let mut out = Vec::new();
        let mut last: HashMap<u32, (usize, &crate::model::clock::Clock)> = HashMap::new();
        for (mesh, owner, sent) in &self.far_adds {
            if let Some((prev, at)) = last.insert(*mesh, (*owner, sent)) {
                if prev != *owner && at.at(prev) > sent.at(prev) {
                    out.push(Finding::new(
                        Kind::Port,
                        self.procs[*owner].name.clone(),
                        format!(
                            "an add across nodes is not ordered after the one from {}",
                            self.procs[prev].name
                        ),
                    ));
                }
            }
        }
        out
    }

    /// P3: an L1 burst into a cluster lands before a memory FILL's
    /// responses can start, or is sent after the FILL completed. A
    /// cluster's FILLs are in its sequencer's order, so the FILLs completed
    /// before a burst's sending are a prefix; the rest must all start after
    /// the burst landed.
    fn collisions(&self) -> Vec<Finding> {
        let mut out = Vec::new();
        if crate::mutant::on(super::units::cluster::CHECKER) {
            return out;
        }
        for unit in &self.units {
            let Kinded::Mg(c) = &unit.kind else {
                continue;
            };
            let windows = &c.fill_windows;
            if windows.is_empty() {
                continue;
            }
            let (seq, port) = (c.seq, unit.port);
            let ends: Vec<u32> = windows.iter().map(|(_, e)| e.at(seq)).collect();
            let starts = Minima::new(windows.iter().map(|(start, _)| start.at(port)));
            for (landed, sent, what) in &c.l1_bursts {
                let at = landed.at(port);
                let first = ends.partition_point(|&e| e <= sent.at(seq));
                if let Some(b) = starts.first_below(first, at) {
                    out.push(Finding::new(
                        Kind::Port,
                        unit.name.clone(),
                        format!("{what} may land while FILL {b}'s responses arrive"),
                    ));
                }
            }
        }
        out
    }
}

/// Range minima preserve nonmonotone shared-FILL start clocks.
struct Minima {
    tree: Vec<u32>,
    leaves: usize,
}

impl Minima {
    fn new(values: impl ExactSizeIterator<Item = u32>) -> Self {
        let leaves = values.len().max(1).next_power_of_two();
        let mut tree = vec![u32::MAX; 2 * leaves];
        for (i, value) in values.enumerate() {
            tree[leaves + i] = value;
        }
        for i in (1..leaves).rev() {
            tree[i] = tree[2 * i].min(tree[2 * i + 1]);
        }
        Self { tree, leaves }
    }

    fn first_below(&self, start: usize, ceiling: u32) -> Option<usize> {
        self.find(1, 0, self.leaves, start, ceiling)
    }

    fn find(&self, node: usize, lo: usize, hi: usize, start: usize, ceiling: u32) -> Option<usize> {
        if hi <= start || self.tree[node] >= ceiling {
            return None;
        }
        if hi - lo == 1 {
            return Some(lo);
        }
        let mid = lo + (hi - lo) / 2;
        self.find(2 * node, lo, mid, start, ceiling)
            .or_else(|| self.find(2 * node + 1, mid, hi, start, ceiling))
    }
}
