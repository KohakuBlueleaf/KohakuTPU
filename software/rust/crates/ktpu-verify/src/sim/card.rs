//! The routes between the card's units: credits, CU_DATA bursts,
//! acknowledgements and shared fills.
//!
//! A burst lands as one event of its receiver's port: it counts toward the
//! receiver's counters with that event's clock, and the DATA_RECEIVED of a
//! burst the node sent counts as one completion of the receiver in that
//! node's dispatch state (`engine.c` ka_engine_drain).

use crate::adapter::isa::Fields;
use crate::model::clock::Clock;
use crate::model::finding::Kind;

use super::memory::What;
use super::{pair, Group, Kinded, Sender, Sim, Sink, Window};

impl Sim<'_> {
    pub fn ports(&self, mesh: u32) -> Vec<(u8, u8)> {
        self.t
            .mesh(mesh)
            .map(|m| m.mem_ports.clone())
            .unwrap_or_default()
    }

    fn sender_key(&self, s: Sender) -> (u32, u8, u8) {
        match s {
            Sender::Node(i) => self.nodes[i].key,
            Sender::Unit(u) => self.units[u].key,
        }
    }

    pub fn sender_name(&self, s: Sender) -> String {
        match s {
            Sender::Node(i) => self.nodes[i].name.clone(),
            Sender::Unit(u) => self.units[u].name.clone(),
        }
    }

    /// A credit of `n` from unit `u` to `to` in its mesh.
    pub fn post(&mut self, u: usize, to: (i64, i64), n: i64, clock: &Clock, owner: usize) {
        let mesh = self.units[u].key.0;
        let dst = self.unit((mesh, to.0 as u8, to.1 as u8));
        let Some(dst) = dst else {
            let name = self.units[u].name.clone();
            self.find(
                Kind::Route,
                name,
                format!("a credit of {n} to {}, where no unit is", pair(to.0, to.1)),
            );
            return;
        };
        let cr = match &self.units[dst].kind {
            Kinded::Mg(c) => c.cr,
            Kinded::Vc(v) => v.cr,
            Kinded::Sr(r) => r.cr,
        };
        self.counters[cr].add(n, clock, owner);
    }

    /// A CU_DATA burst of `n` granules from `u` to `sink`.
    pub fn send(&mut self, u: Sender, sink: Sink, off: i64, n: i64, clock: &Clock, owner: usize) {
        let mesh = if sink.far.is_some() {
            sink.mesh
        } else {
            self.sender_key(u).0
        };
        let to = sink.far.unwrap_or(sink.to);
        if to == (0, 0) || self.ports(mesh).contains(&to) {
            // The node's RX queue: the descriptor and every word, a flit each.
            if let Some(&node) = self.node_of.get(&mesh) {
                let rx = self.nodes[node].rx;
                self.counters[rx].add(n + 1, clock, owner);
            }
            return;
        }
        let Some(dst) = self.unit((mesh, to.0, to.1)) else {
            let name = self.sender_name(u);
            self.find(
                Kind::Route,
                name,
                format!(
                    "a burst of {n} to {mesh}:{}, where no unit is",
                    pair(to.0, to.1)
                ),
            );
            return;
        };
        let mut rel = vec![clock.clone()];
        rel.extend(self.joins(dst, sink.buf));
        let port = self.units[dst].port;
        let landed = self.tick(port, &rel);
        self.units[dst]
            .landings
            .push((owner, clock.clone(), landed.clone()));
        let what = What::Burst(self.sender_name(u).into());
        let why = self.land(dst, sink.buf, off, n, sink.count, &landed, &what, clock);
        if let Some(why) = why {
            let name = self.sender_name(u);
            let dname = self.units[dst].name.clone();
            self.find(
                Kind::Bounds,
                name,
                format!("burst to {dname} rejected: {why}"),
            );
        }
        if sink.signal {
            // An ack field of 0 means the burst's sender; a nonzero one names a
            // unit coordinate, and the node's processor has none (H2): only a
            // burst the node sent itself is acknowledged to the node.
            let node = self.node_of.get(&mesh).copied();
            if let (Some(node), Sender::Node(from)) = (node, u) {
                if sink.ack == (0, 0) && from == node && self.feeder.get(&dst) == Some(&node) {
                    let (recv, acks) = (self.units[dst].recv, self.units[dst].acks);
                    self.counters[recv].add(1, &landed, port);
                    self.counters[acks].add(1, &landed, port);
                    self.acks.push((node, dst, clock.clone(), landed));
                }
            }
        }
    }

    /// What a burst into `buf` lands after: an add into a cluster's
    /// accumulator waits out a running sweep at its head (`peer_open`).
    fn joins(&self, u: usize, buf: i64) -> Vec<Clock> {
        match &self.units[u].kind {
            Kinded::Mg(c) if buf == super::units::cluster::BUF_PEER => {
                vec![self.clock_of(c.array)]
            }
            _ => Vec::new(),
        }
    }

    /// Why unit `u` rejects `n` granules at `off` of buffer `buf` (None
    /// when it takes them).
    #[allow(clippy::too_many_arguments)]
    fn land(
        &mut self,
        u: usize,
        buf: i64,
        off: i64,
        n: i64,
        count: bool,
        clock: &Clock,
        what: &What,
        sent: &Clock,
    ) -> Option<String> {
        match self.units[u].kind {
            Kinded::Mg(_) => self.mg_land(u, buf, off, n, count, clock, what, sent),
            Kinded::Vc(_) => self.vc_land(u, buf, off, n, count, clock, what),
            Kinded::Sr(_) => self.sr_land(u, buf, n, count, clock),
        }
    }

    // ------------------------------------------------------------ shared fills
    /// `(released, clock, lead arrival)` for a shared FILL: its read is
    /// issued by the lowest-indexed sharer and lands in every sharer, so each
    /// sharer's k-th fill of the group completes once every sharer reached
    /// its k-th.
    pub fn rendezvous(
        &mut self,
        u: usize,
        seq: usize,
        pc: usize,
        f: &Fields,
    ) -> Option<(Clock, Clock)> {
        let (mesh, x, y) = self.units[u].key;
        let cfg = &self.t.isa.cluster.cfg;
        let me = (y as i64) << cfg.pos_bits | x as i64;
        let peers = f.g("peers");
        let width = cfg.coord_bits();
        let mask = ktpu_isa::layout::mask(width) as i64;
        let mut members: Vec<i64> = (0..f.g("npeer"))
            .map(|i| (peers >> (width * i as u32)) & mask)
            .collect();
        members.push(me);
        members.sort_unstable();
        members.dedup();
        let group: Group = (mesh, members);
        let lead = group.1[0];
        if !self.arrived.contains_key(&(seq, pc)) {
            let t = self.tick(seq, &[]);
            let mine = self
                .shares
                .entry(group.clone())
                .or_default()
                .entry(me)
                .or_default();
            mine.push(t.clone());
            let k = mine.len();
            self.arrived.insert((seq, pc), k);
            if me != lead {
                self.windows.push(Window {
                    unit: u,
                    group: group.clone(),
                    lead,
                    k: k - 1,
                    mine: t,
                    me: seq,
                });
            }
        }
        let k = self.arrived[&(seq, pc)];
        let arrivals = self.shares.entry(group.clone()).or_default();
        if group
            .1
            .iter()
            .any(|s| arrivals.get(s).map_or(0, |v| v.len()) < k)
        {
            return None;
        }
        let mut out = Clock::new();
        for s in &group.1 {
            out.join(&arrivals[s][k - 1]);
        }
        Some((out, arrivals[&lead][k - 1].clone()))
    }

    /// The lead sharer's k-th arrival clock, if it arrived.
    pub fn lead_arrival(&self, group: &Group, lead: i64, k: usize) -> Option<Clock> {
        self.shares
            .get(group)
            .and_then(|a| a.get(&lead))
            .and_then(|v| v.get(k))
            .cloned()
    }
}
