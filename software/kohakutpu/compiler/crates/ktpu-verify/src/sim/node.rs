//! The node on the abstract machine: a package's steps under the firmware's
//! dispatch rules (`firmware/kohakuaccel/dispatch/engine.c`).
//!
//! * A word goes to a unit only while the unit holds fewer than its credit
//!   in flight (sent less completed); a unit with a fetch port takes its
//!   words in requests of at most min(room, 255), room = min(credit, fetch
//!   depth), each only once all of it fits (L0.STEP.007, L0.STEP.045).
//! * `AWAIT u n` raises u's expected count by n and waits until u's
//!   completions (acknowledgements included) reach it; `BARRIER` and a
//!   package's end wait until every word sent has completed.
//!
//! Consecutive DISPATCH / REPEAT steps form a segment: the words of
//! different units in it are unordered, each unit's in order. A node made
//! `free` is the dispatch scheduler (verify.md s6): it runs a segment in
//! package order, and only when the whole machine is stuck sends the
//! earliest item heading its unit's queue that can move; `packages` returns
//! the packages in the order it sent.

use crate::sim::fx::{FxHashMap as HashMap, FxHashSet as HashSet};

use crate::adapter::package::{flag, op, Package, Step, PATCH_STEPS};
use crate::adapter::u256::U256;
use crate::model::clock::Clock;
use crate::model::finding::Kind;

use super::{Pk, Sender, Sim, Sink, Take};

/// A fetch run's most words: its 8-bit count (L0.STEP.045).
pub const FETCH_RUN: i64 = 255;
const MASK: u64 = u64::MAX;

/// A DISPATCH (or a REPEAT, written out, sent whole) of one segment.
#[derive(Clone, Debug)]
pub(crate) struct Item {
    pub unit: usize,
    pub arg: usize,
    pub count: i64,
    pub atomic: bool,
    pub step: Step,
    pub sent: i64,
}

#[derive(Clone, Debug)]
pub(crate) struct Segment {
    pub pkg: usize,
    pub start: usize,
    pub end: usize,
    pub items: Vec<Item>,
    /// One per run of the segment: [(item index, first word, words)].
    pub logs: Vec<Vec<(usize, i64, i64)>>,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum NStep {
    Seg(usize),
    Op(Step),
}

pub(crate) struct Node {
    pub proc: usize,
    pub mesh: u32,
    pub name: String,
    pub pkgs: Vec<Package>,
    pub free: bool,
    pub units: Vec<Option<usize>>,
    credit: Vec<i64>,
    fetch: Vec<bool>,
    sent: Vec<i64>,
    expected: Vec<i64>,
    payloads: Vec<U256>,
    pub steps: Vec<NStep>,
    pub segments: Vec<Segment>,
    pub at: usize,
    armed: bool,
    pub why: String,
    /// Package registers: a value, or None once it holds data from memory.
    regs: HashMap<u16, Option<u64>>,
    /// JUMP step -> times taken on data
    taken_on_data: HashMap<usize, u32>,
    pub rx: usize,
    pub rx_taken: i64,
    /// Where bursts the node sends come from, as a unit's key.
    pub key: (u32, u8, u8),
    acks_taken: Vec<i64>,
    /// (this node's event count, clock) at each package's end
    pub ends: Vec<(u32, Clock)>,
    log: Option<Vec<(usize, i64, i64)>>,
    /// The run whose end every node must reach before this one goes on.
    host: usize,
}

impl Node {
    pub fn done(&self) -> bool {
        self.at >= self.steps.len()
    }
}

fn signed(v: u64) -> i64 {
    v as i64
}

fn alu(op: u32, a: u64, b: u64) -> Option<u64> {
    Some(match op {
        0 => b,
        1 => a.wrapping_add(b),
        2 => a.wrapping_sub(b),
        3 => a.wrapping_mul(b),
        4 => a & b,
        5 => a | b,
        6 => a ^ b,
        7 => a.wrapping_shl((b & 63) as u32),
        8 => a >> (b & 63),
        9 => a.min(b),
        10 => a.max(b),
        11 => a.checked_div(b).unwrap_or(MASK),
        12 => a.checked_rem(b).unwrap_or(a),
        _ => return None,
    })
}

fn cond(c: u32, a: u64, b: u64) -> bool {
    match c {
        1 => a == b,
        2 => a != b,
        3 => a < b,
        4 => a >= b,
        5 => signed(a) < signed(b),
        6 => signed(a) >= signed(b),
        _ => false,
    }
}

impl Sim<'_> {
    /// A node running `pkgs` one after another (a package, then its rerun).
    pub fn add_node(&mut self, mesh: u32, pkgs: Vec<Package>, free: bool) -> usize {
        let ni = self.nodes.len();
        let name = format!("node {mesh}");
        let proc = self.add_proc(Pk::Node(ni), name.clone());
        self.node_of.insert(mesh, ni);
        let pkg = pkgs[0].clone();
        if pkgs.iter().any(|p| p.units != pkg.units) {
            self.find(
                Kind::Model,
                name.clone(),
                "the packages name different units",
            );
        }
        let mut units = Vec::new();
        for spec in &pkg.units {
            let u = self.unit((mesh, spec.x, spec.y));
            match u {
                None => self.find(
                    Kind::Route,
                    name.clone(),
                    format!(
                        "the package names ({},{}), where no unit is",
                        spec.x, spec.y
                    ),
                ),
                Some(u) => {
                    self.feeder.insert(u, ni);
                }
            }
            units.push(u);
        }
        let n = units.len();
        let cap = self.t.facts.unbounded;
        let mut node = Node {
            proc,
            mesh,
            name: name.clone(),
            pkgs: Vec::new(),
            free,
            units,
            credit: pkg
                .units
                .iter()
                .map(|u| if u.credit == 0 { cap } else { u.credit as i64 })
                .collect(),
            fetch: pkg.units.iter().map(|u| u.fetch >> 16 != 0).collect(),
            sent: vec![0; n],
            expected: vec![0; n],
            payloads: Vec::new(),
            steps: Vec::new(),
            segments: Vec::new(),
            at: 0,
            armed: false,
            why: String::new(),
            regs: HashMap::default(),
            taken_on_data: HashMap::default(),
            rx: 0,
            rx_taken: 0,
            key: (mesh, 0, 0),
            acks_taken: vec![0; n],
            ends: Vec::new(),
            log: None,
            host: 0,
        };
        for (i, p) in pkgs.iter().enumerate() {
            node.load(p, i);
        }
        node.pkgs = pkgs;
        node.rx = self.counter(&format!("rx:{name}"));
        if let Some(&(x, y)) = self.ports(mesh).first() {
            node.key = (mesh, x, y);
        }
        self.nodes.push(node);
        ni
    }

    pub fn node_blocked(&self, i: usize) -> String {
        let nd = &self.nodes[i];
        match nd.steps[nd.at] {
            NStep::Seg(s) => {
                let seg = &nd.segments[s];
                format!(
                    "segment at package {} step {}: {}",
                    seg.pkg, seg.start, nd.why
                )
            }
            NStep::Op(s) => format!("step {} {} u{}: {}", nd.at, op::name(s.op), s.unit, nd.why),
        }
    }

    fn retired(&self, i: usize, u: usize) -> i64 {
        let unit = self.nodes[i].units[u].expect("a unit");
        self.counters[self.units[unit].done].value
    }

    pub fn node_step(&mut self, i: usize) -> bool {
        let p = self.nodes[i].proc;
        if self.nodes[i].done() {
            return false;
        }
        let host = self.nodes[i].host;
        if host > 0 {
            // The host submits the next run once every node ended this one.
            let c = self.counter(&format!("host:{host}"));
            let need = self.node_of.len() as i64;
            if !self.counters[c].reached(need) {
                self.nodes[i].why = "the host waits for every node's package".into();
                return false;
            }
            let cl = self.clock_to(c, need, &[]);
            self.tick(p, &[cl]);
            self.nodes[i].host = 0;
        }
        let s = match self.nodes[i].steps[self.nodes[i].at] {
            NStep::Seg(s) => return self.segment(i, s),
            NStep::Op(s) => s,
        };
        match s.op {
            op::END | op::BARRIER => {
                // Every word sent has completed (interp.c: ka_engine_barrier).
                let mut rel = Vec::new();
                for u in 0..self.nodes[i].units.len() {
                    let Some(unit) = self.nodes[i].units[u] else {
                        continue;
                    };
                    let done = self.units[unit].done;
                    let sent = self.nodes[i].sent[u];
                    if !self.counters[done].reached(sent) {
                        self.nodes[i].why = format!(
                            "barrier: {} {} of {} completed",
                            self.units[unit].name, self.counters[done].value, sent
                        );
                        return false;
                    }
                    rel.push(self.clock_to(done, sent, &[]));
                }
                let clock = self.tick(p, &rel);
                let nd = &mut self.nodes[i];
                nd.at += 1;
                if s.op == op::END {
                    // The next package counts its completions from here.
                    for (e, n) in nd.expected.iter_mut().zip(&nd.sent) {
                        *e = (*e).max(*n);
                    }
                    let n = self.procs[p].n;
                    nd.ends.push((n, clock.clone()));
                    nd.regs.clear();
                    let k = nd.ends.len();
                    nd.host = k;
                    let c = self.counter(&format!("host:{k}"));
                    self.counters[c].add(1, &clock, p);
                }
                true
            }
            op::AWAIT => {
                let u = s.unit as usize;
                let Some(unit) = self.nodes[i].units.get(u).copied().flatten() else {
                    self.nodes[i].at += 1;
                    return true;
                };
                if !self.nodes[i].armed {
                    if s.flags == flag::IMM {
                        self.nodes[i].expected[u] = self.nodes[i].sent[u];
                    } else {
                        self.nodes[i].expected[u] += s.count as i64;
                    }
                    self.nodes[i].armed = true;
                }
                let recv = self.units[unit].recv;
                let want = self.nodes[i].expected[u];
                if !self.counters[recv].reached(want) {
                    self.nodes[i].why = format!(
                        "{}: {} of {} completions",
                        self.units[unit].name, self.counters[recv].value, want
                    );
                    return false;
                }
                // The unit completes at most the words this node sent it so far.
                let cap = [(self.units[unit].completer, self.nodes[i].sent[u])];
                let cl = self.clock_to(recv, want, &cap);
                self.tick(p, &[cl]);
                self.nodes[i].armed = false;
                self.nodes[i].at += 1;
                true
            }
            op::RECV => {
                let rx = self.nodes[i].rx;
                let want = self.nodes[i].rx_taken + s.count as i64;
                if !self.counters[rx].reached(want) {
                    self.nodes[i].why = format!(
                        "recv: {} of {} flits",
                        self.counters[rx].value - self.nodes[i].rx_taken,
                        s.count
                    );
                    return false;
                }
                let cl = self.clock_to(rx, want, &[]);
                self.tick(p, &[cl]);
                self.nodes[i].rx_taken = want;
                self.nodes[i].at += 1;
                true
            }
            op::RING | op::WAIT_BELL | op::WAIT_SLOT | op::FETCH | op::SEND | op::ENGINE => {
                self.node_other(i, s)
            }
            op::LOAD | op::ALU | op::JUMP => self.node_register(i, s),
            _ => {
                // MOVER, MWAIT, SETTLE, SIGNAL, CTRL, STORE, PATCH: nothing a
                // unit waits on.
                self.tick(p, &[]);
                self.nodes[i].at += 1;
                true
            }
        }
    }

    // ------------------------------------------------------------- segments
    /// Package order: the first item not fully sent, as far as credit lets.
    fn segment(&mut self, i: usize, s: usize) -> bool {
        if self.nodes[i].log.is_none() {
            for it in &mut self.nodes[i].segments[s].items {
                it.sent = 0;
            }
            self.nodes[i].log = Some(Vec::new());
        }
        let mut moved = false;
        for k in 0..self.nodes[i].segments[s].items.len() {
            let it = &self.nodes[i].segments[s].items[k];
            if it.sent < it.count {
                let n = self.push(i, s, k);
                moved = moved || n > 0;
                let it = &self.nodes[i].segments[s].items[k];
                if it.sent < it.count {
                    return moved;
                }
            }
        }
        let nd = &mut self.nodes[i];
        let log = nd.log.take().unwrap_or_default();
        nd.segments[s].logs.push(log);
        nd.at += 1;
        true
    }

    /// The machine is stuck: send what the earliest item heading its unit's
    /// queue can, past the blocked one. False if none can move.
    pub fn node_relax(&mut self, i: usize) -> bool {
        let nd = &self.nodes[i];
        if !nd.free || nd.done() || nd.log.is_none() {
            return false;
        }
        let NStep::Seg(s) = nd.steps[nd.at] else {
            return false;
        };
        let seg = &nd.segments[s];
        if seg
            .items
            .iter()
            .any(|it| it.atomic && 0 < it.sent && it.sent < it.count)
        {
            return false;
        }
        let mut heads = HashSet::default();
        let mut first = true;
        for k in 0..self.nodes[i].segments[s].items.len() {
            let it = &self.nodes[i].segments[s].items[k];
            if it.sent >= it.count || heads.contains(&it.unit) {
                continue;
            }
            heads.insert(it.unit);
            if first {
                first = false;
                continue;
            }
            if self.push(i, s, k) > 0 {
                return true;
            }
        }
        false
    }

    /// Words of item `k` sent now under the dispatch rules.
    fn push(&mut self, i: usize, s: usize, k: usize) -> i64 {
        let p = self.nodes[i].proc;
        let u = self.nodes[i].segments[s].items[k].unit;
        let Some(unit) = self.nodes[i].units.get(u).copied().flatten() else {
            let it = &mut self.nodes[i].segments[s].items[k];
            it.sent = it.count;
            return 0;
        };
        let fetch_depth = self.t.facts.fetch_depth;
        let mut total = 0;
        loop {
            let nd = &self.nodes[i];
            let it = &nd.segments[s].items[k];
            if it.sent >= it.count {
                break;
            }
            let (limit, n) = if nd.fetch[u] {
                let limit = nd.credit[u].min(fetch_depth);
                (limit, (it.count - it.sent).min(limit.clamp(1, FETCH_RUN)))
            } else {
                (nd.credit[u], 1)
            };
            let need = nd.sent[u] + n - limit;
            let retired = self.retired(i, u);
            if need > retired {
                let why = format!(
                    "{} words to {}: {} in flight",
                    n,
                    self.units[unit].name,
                    self.nodes[i].sent[u] - retired
                );
                self.nodes[i].why = why;
                break;
            }
            let clock = if need > 0 {
                let cl = self.clock_to(self.units[unit].done, need, &[]);
                self.tick(p, &[cl])
            } else {
                self.tick(p, &[])
            };
            let (arg, sent) = {
                let it = &self.nodes[i].segments[s].items[k];
                (it.arg, it.sent)
            };
            for j in 0..n {
                let w = self.nodes[i].payloads[arg + (sent + j) as usize];
                self.deliver(unit, w, clock.clone());
            }
            let nd = &mut self.nodes[i];
            let log = nd.log.as_mut().expect("a segment's log");
            match log.last_mut() {
                Some(last) if last.0 == k => last.2 += n,
                _ => log.push((k, sent, n)),
            }
            nd.sent[u] += n;
            nd.segments[s].items[k].sent += n;
            total += n;
        }
        total
    }

    /// The packages with each segment's items in the order sent; Err when a
    /// segment run twice (a loop) was sent in two orders.
    pub fn node_packages(&self, i: usize) -> Result<Vec<Package>, String> {
        let nd = &self.nodes[i];
        let mut out = Vec::new();
        for (index, pkg) in nd.pkgs.iter().enumerate() {
            let segs: HashMap<usize, &Segment> = nd
                .segments
                .iter()
                .filter(|s| s.pkg == index)
                .map(|s| (s.start, s))
                .collect();
            let mut steps: Vec<Step> = Vec::new();
            let mut at: HashMap<usize, usize> = HashMap::default();
            let mut j = 0;
            while j < pkg.steps.len() {
                match segs.get(&j) {
                    None => {
                        at.insert(j, steps.len());
                        steps.push(pkg.steps[j]);
                        j += 1;
                    }
                    Some(seg) => {
                        for k in seg.start..seg.end {
                            at.insert(k, steps.len());
                        }
                        steps.extend(order(nd.mesh, seg)?);
                        j = seg.end;
                    }
                }
            }
            at.insert(pkg.steps.len(), steps.len());
            let to = |t: usize| at.get(&t).copied().unwrap_or(t);
            for st in &mut steps {
                if st.op == op::JUMP {
                    st.unit = to(st.unit as usize) as u16;
                } else if st.op == op::PATCH && st.arg & PATCH_STEPS != 0 {
                    st.count = to(st.count as usize) as u32;
                }
            }
            out.push(Package {
                steps,
                ..pkg.clone()
            });
        }
        Ok(out)
    }

    // ------------------------------------------------------------- the rest
    fn node_other(&mut self, i: usize, s: Step) -> bool {
        let p = self.nodes[i].proc;
        let mesh = self.nodes[i].mesh;
        match s.op {
            op::RING => {
                let clock = self.tick(p, &[]);
                let slot = self.counter(&format!("slot:{}:{}", s.unit, s.count));
                self.counters[slot].add((s.arg & 0xFFFF) as i64, &clock, p);
                let bell = self.counter(&format!("bell:{}:{}", s.unit, mesh));
                self.counters[bell].add(1, &clock, p);
                self.nodes[i].at += 1;
                true
            }
            op::WAIT_BELL | op::WAIT_SLOT => {
                let (key, c) = if s.op == op::WAIT_BELL {
                    (
                        Take::Bell(s.unit as i64),
                        self.counter(&format!("bell:{mesh}:{}", s.unit)),
                    )
                } else {
                    (
                        Take::Slot(s.unit as i64),
                        self.counter(&format!("slot:{mesh}:{}", s.unit)),
                    )
                };
                self.waited.insert((mesh, key));
                let taken = self.taken.get(&(mesh, key)).copied().unwrap_or(0);
                let want = taken + s.count as i64;
                if !self.counters[c].reached(want) {
                    let (kind, which) = match key {
                        Take::Bell(w) => ("bell", w),
                        Take::Slot(w) => ("slot", w),
                    };
                    self.nodes[i].why = format!(
                        "{kind} {which}: {} of {}",
                        self.counters[c].value - taken,
                        s.count
                    );
                    return false;
                }
                let cl = self.clock_to(c, want, &[]);
                self.tick(p, &[cl]);
                if s.op == op::WAIT_BELL || s.flags & flag::CONSUME != 0 {
                    self.taken.insert((mesh, key), want);
                }
                self.nodes[i].at += 1;
                true
            }
            op::SEND => self.node_send(i, s),
            _ => {
                let at = self.nodes[i].at;
                let name = self.nodes[i].name.clone();
                self.find(
                    Kind::Model,
                    name,
                    format!("step {at}: {} is not modelled", op::name(s.op)),
                );
                self.tick(p, &[]);
                self.nodes[i].at += 1;
                true
            }
        }
    }

    /// LOAD, ALU, JUMP (interp.c load/alu/jump). A loaded value is data; a
    /// JUMP on data is taken `TRIPS` times, then not.
    fn node_register(&mut self, i: usize, s: Step) -> bool {
        let p = self.nodes[i].proc;
        self.tick(p, &[]);
        let trips = crate::target::TRIPS;
        let nd = &mut self.nodes[i];
        if s.op == op::LOAD {
            nd.regs.insert(
                s.unit,
                if s.flags == flag::IMM {
                    nd.payloads.get(s.arg as usize).map(|w| w.bits(0, 64))
                } else {
                    None
                },
            );
            nd.at += 1;
            return true;
        }
        let reg = |r: u32| nd.regs.get(&(r as u16)).copied().unwrap_or(Some(0));
        let a = reg((s.count >> 8) & 0xF);
        let b = if s.flags & flag::IMM != 0 {
            Some(s.arg)
        } else {
            reg((s.count >> 12) & 0xF)
        };
        if s.op == op::ALU {
            let v = match (a, b) {
                (Some(a), Some(b)) => alu(s.count & 0xFF, a, b),
                _ => None,
            };
            nd.regs.insert(s.unit, v);
            nd.at += 1;
            return true;
        }
        let c = s.count & 0xFF;
        let go = if c == 0 {
            true
        } else {
            match (a, b) {
                (Some(a), Some(b)) => cond(c, a, b),
                _ => {
                    let at = nd.at;
                    let n = nd.taken_on_data.get(&at).copied().unwrap_or(0);
                    let go = n < trips;
                    nd.taken_on_data.insert(at, n + u32::from(go));
                    if !go {
                        let name = nd.name.clone();
                        self.find(
                            Kind::Loop,
                            name,
                            format!("step {at}: a loop on data, checked for 0..{trips} trips"),
                        );
                    }
                    go
                }
            }
        };
        let nd = &mut self.nodes[i];
        nd.at = if go { s.unit as usize } else { nd.at + 1 };
        true
    }

    /// Raw flits to a unit (interp.c send): a CU_DATA descriptor and its
    /// words, read from memory under MEM; with ACK the step waits for one
    /// more DATA_RECEIVED from the unit.
    fn node_send(&mut self, i: usize, s: Step) -> bool {
        let p = self.nodes[i].proc;
        let unit = self.nodes[i].units.get(s.unit as usize).copied().flatten();
        if !self.nodes[i].armed {
            let kind = (s.arg >> 32) & 0xF;
            match (kind, unit) {
                (8, Some(u)) => {
                    let d = self.nodes[i].payloads[(s.arg & 0xFFFF_FFFF) as usize];
                    let n = if s.flags & flag::MEM != 0 {
                        s.count as i64
                    } else {
                        s.count as i64 - 1
                    };
                    let ack = d.bits(208, 8) as u8;
                    let key = self.units[u].key;
                    let sink = Sink {
                        to: (key.1, key.2),
                        far: None,
                        mesh: self.nodes[i].mesh,
                        buf: d.bits(248, 8) as i64,
                        count: d.bits(216, 8) & 2 != 0,
                        signal: d.bits(216, 8) & 1 != 0,
                        ack: (ack & 0xF, ack >> 4),
                    };
                    let words = d.bits(224, 8) as i64 + 1;
                    if n != words {
                        let at = self.nodes[i].at;
                        let name = self.nodes[i].name.clone();
                        self.find(
                            Kind::Bounds,
                            name,
                            format!("step {at}: {n} words under a descriptor of {words}"),
                        );
                    }
                    let clock = self.tick(p, &[]);
                    self.send(Sender::Node(i), sink, d.bits(232, 16) as i64, n, &clock, p);
                }
                (7, Some(u)) => {
                    // CU_CTRL: the unit's control port answers each flit with one.
                    let clock = self.tick(p, &[]);
                    let rx = self.nodes[i].rx;
                    let port = self.units[u].port;
                    self.counters[rx].add(s.count as i64, &clock, port);
                }
                _ => {
                    self.tick(p, &[]);
                }
            }
            self.nodes[i].armed = true;
            if s.flags & flag::ACK != 0 && unit.is_some() {
                self.nodes[i].acks_taken[s.unit as usize] += 1;
            }
        }
        if let (true, Some(u)) = (s.flags & flag::ACK != 0, unit) {
            let want = self.nodes[i].acks_taken[s.unit as usize];
            let acks = self.units[u].acks;
            if !self.counters[acks].reached(want) {
                self.nodes[i].why = format!("send: DATA_RECEIVED from {}", self.units[u].name);
                return false;
            }
            let cl = self.clock_to(acks, want, &[]);
            self.tick(p, &[cl]);
        }
        self.nodes[i].armed = false;
        self.nodes[i].at += 1;
        true
    }

    /// What the node leaves behind: flits no RECV took.
    pub fn node_rest(&self, i: usize) -> Vec<String> {
        let nd = &self.nodes[i];
        let v = self.counters[nd.rx].value;
        if v != nd.rx_taken {
            vec![format!(
                "{} flits landed here and never received",
                v - nd.rx_taken
            )]
        } else {
            Vec::new()
        }
    }
}

fn order(mesh: u32, seg: &Segment) -> Result<Vec<Step>, String> {
    let reverse = crate::mutant::on("l1.verify/reverse-segment");
    let Some(first) = seg.logs.first() else {
        let mut out: Vec<Step> = seg.items.iter().map(|it| it.step).collect();
        if reverse {
            out.reverse();
        }
        return Ok(out);
    };
    if seg.logs.iter().any(|l| l != first) {
        return Err(format!(
            "node {mesh}: a segment in a loop was sent in two orders"
        ));
    }
    let mut out: Vec<Step> = first
        .iter()
        .map(|&(k, from, n)| {
            let it = &seg.items[k];
            if it.atomic {
                it.step
            } else {
                Step::new(
                    op::DISPATCH,
                    it.step.unit,
                    n as u32,
                    it.step.arg + from as u64,
                    0,
                )
            }
        })
        .collect();
    if reverse {
        out.reverse();
    }
    Ok(out)
}

impl Node {
    /// The package's steps with each segment one step, and every payload and
    /// step index moved past the packages before.
    fn load(&mut self, pkg: &Package, index: usize) {
        let base = self.payloads.len();
        self.payloads.extend(pkg.payloads.iter().copied());
        let first = self.steps.len();
        let mut at: HashMap<usize, usize> = HashMap::default();
        let mut seg: Option<usize> = None;
        let mut jumps = Vec::new();
        for (j, s) in pkg.steps.iter().enumerate() {
            if s.op == op::DISPATCH || s.op == op::REPEAT {
                let si = match seg {
                    Some(si) => si,
                    None => {
                        self.segments.push(Segment {
                            pkg: index,
                            start: j,
                            end: j,
                            items: Vec::new(),
                            logs: Vec::new(),
                        });
                        let si = self.segments.len() - 1;
                        self.steps.push(NStep::Seg(si));
                        seg = Some(si);
                        si
                    }
                };
                at.insert(j, self.steps.len() - 1);
                self.segments[si].end = j + 1;
                let item = if s.op == op::DISPATCH {
                    Item {
                        unit: s.unit as usize,
                        arg: base + s.arg as usize,
                        count: s.count as i64,
                        atomic: false,
                        step: *s,
                        sent: 0,
                    }
                } else {
                    let (from, n) = self.repeat(s, base);
                    Item {
                        unit: s.unit as usize,
                        arg: from,
                        count: n,
                        atomic: true,
                        step: *s,
                        sent: 0,
                    }
                };
                self.segments[si].items.push(item);
                continue;
            }
            seg = None;
            at.insert(j, self.steps.len());
            if s.op == op::JUMP {
                jumps.push(self.steps.len());
            }
            let mut s = *s;
            if matches!(s.op, op::SEND | op::RECV | op::LOAD) {
                s.arg += base as u64;
            }
            self.steps.push(NStep::Op(s));
        }
        let ends = matches!(self.steps.last(), Some(NStep::Op(s)) if s.op == op::END);
        if self.steps.len() == first || !ends {
            at.insert(pkg.steps.len(), self.steps.len());
            self.steps.push(NStep::Op(Step::new(op::END, 0, 0, 0, 0)));
        }
        for k in jumps {
            if let NStep::Op(s) = &mut self.steps[k] {
                let t = s.unit as usize;
                s.unit = at.get(&t).copied().unwrap_or(t) as u16;
            }
        }
    }

    /// A REPEAT's words written out: (first payload, words).
    fn repeat(&mut self, s: &Step, base: usize) -> (usize, i64) {
        let first = base + (s.arg & 0xFFFF_FFFF) as usize;
        let repeats = (s.arg >> 32) & 0xFFFF;
        let nincs = s.arg >> 48;
        let count = s.count as usize;
        let mut incs = Vec::new();
        for k in 0..nincs as usize {
            let w = self.payloads[first + count + k / 2];
            let half = 128 * (k % 2) as u32;
            let head = w.bits(half, 64);
            let delta = w.bits(half + 64, 64);
            incs.push((
                (head & 0xFFFF_FFFF) as usize,
                ((head >> 32) & 0xFF) as u32,
                ((head >> 40) & 0xFF) as u32,
                delta,
            ));
        }
        let at = self.payloads.len();
        for r in 0..repeats {
            for j in 0..count {
                let mut w = self.payloads[first + j];
                for &(index, bit, width, delta) in &incs {
                    if index == j {
                        let old = w.bits(bit, width);
                        w.set_bits(bit, width, old.wrapping_add(r.wrapping_mul(delta)));
                    }
                }
                self.payloads.push(w);
            }
        }
        (at, (repeats as usize * count) as i64)
    }
}
