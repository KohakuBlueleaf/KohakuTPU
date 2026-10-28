//! What a finished run leaves (L1.VER.007) and how bursts met at the ports
//! (L1.VER.008).

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
            // A FILL of this cluster's own starts at its sequencer's previous
            // event: those starts only grow. A shared FILL's starts at its lead's.
            let own: Vec<bool> = windows
                .iter()
                .map(|(s, e)| s.at(seq) as i64 >= e.at(seq) as i64 - 1)
                .collect();
            let shared: Vec<usize> = (0..own.len()).filter(|&i| !own[i]).collect();
            for (landed, sent, what) in &c.l1_bursts {
                let at = landed.at(port);
                let first = ends.partition_point(|&e| e <= sent.at(seq));
                let mut bad = None;
                for i in first..windows.len() {
                    if at > windows[i].0.at(port) {
                        bad = Some(i);
                        break;
                    }
                    if own[i] {
                        let later = &shared[shared.partition_point(|&j| j <= i)..];
                        bad = later.iter().copied().find(|&j| at > windows[j].0.at(port));
                        break;
                    }
                }
                if let Some(b) = bad {
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
