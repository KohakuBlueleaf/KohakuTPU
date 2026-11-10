//! The node on the abstract machine: a package's steps under the firmware's
//! dispatch rules (`software/kohakuaccel/firmware/dispatch/engine.c`).
//!
//! * A word goes to a unit only while the unit holds fewer than its credit
//!   in flight (sent less completed); a unit with a fetch port takes its
//!   words in requests of at most min(room, 255), room = min(credit, fetch
//!   depth), each only once all of it fits (L0.STEP.007, L0.STEP.045).
//! * `AWAIT u n` raises u's expected count by n and waits until u's
//!   instruction retirements reach it (L0.STEP.020). DATA_RECEIVED belongs
//!   to SEND ACK, not retirement. `BARRIER` and a package's end wait until
//!   every word sent has completed.
//!
//! Consecutive DISPATCH / REPEAT steps form a segment: the words of
//! different units in it are unordered, each unit's in order. A node made
//! `free` is the dispatch scheduler (verify.md s6): it runs a segment in
//! package order, and only when the whole machine is stuck sends the
//! earliest item heading its unit's queue that can move; `packages` returns
//! the packages in the order it sent.

mod engine;
mod poll;

use crate::sim::fx::{FxHashMap as HashMap, FxHashSet as HashSet};

use crate::adapter::package::{flag, op, Package, Step, PATCH_STEPS};
use crate::adapter::u256::U256;
use crate::model::clock::Clock;
use crate::model::finding::Kind;
use crate::target::NodePollContract;

use super::package_memory::{Location, Section};
use super::symbolic::Value;
use super::{Pk, Sender, Sim, Sink, StepOutcome, Take};

/// A fetch run's most words: its 8-bit count (L0.STEP.045).
pub const FETCH_RUN: i64 = 255;
const MASK: u64 = u64::MAX;

/// A DISPATCH (or a REPEAT, written out, sent whole) of one segment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Item {
    pub unit: usize,
    pub arg: usize,
    pub count: i64,
    pub atomic: bool,
    pub step: Step,
    pub sent: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
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
    JoinSeg(usize),
    JoinOp(Step),
}

impl PartialEq for NStep {
    fn eq(&self, other: &Self) -> bool {
        match (*self, *other) {
            (Self::Seg(a) | Self::JoinSeg(a), Self::Seg(b) | Self::JoinSeg(b)) => a == b,
            (Self::Op(a) | Self::JoinOp(a), Self::Op(b) | Self::JoinOp(b)) => a == b,
            _ => false,
        }
    }
}

impl Eq for NStep {}

#[derive(Clone, PartialEq, Eq)]
pub(crate) struct Node {
    pub proc: usize,
    mover: crate::lines::Mover,
    mover_proc: usize,
    moves: Vec<Clock>,
    move_base: usize,
    engine: engine::Engine,
    pub mesh: u32,
    pub name: String,
    pub pkgs: Vec<Package>,
    raw_steps: Vec<Vec<Step>>,
    pub free: bool,
    pub units: Vec<Option<usize>>,
    credit: Vec<i64>,
    fetch: Vec<bool>,
    sent: Vec<i64>,
    expected: Vec<i64>,
    frontiers: HashMap<u32, (u16, i64)>,
    payloads: Vec<U256>,
    locations: Vec<Location>,
    step_sources: HashMap<usize, Vec<(usize, usize)>>,
    payload_bases: Vec<usize>,
    step_targets: HashMap<(usize, usize), usize>,
    pub steps: Vec<NStep>,
    pub segments: Vec<Segment>,
    pub at: usize,
    pub branches: HashMap<usize, (usize, usize)>,
    armed: bool,
    fetched: u32,
    pub why: String,
    /// Concrete register values or finite-width symbolic expressions.
    regs: HashMap<u16, Value>,
    flush: Option<poll::Flush>,
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
    pub(crate) fn retained(&self, stats: &mut super::stats::Retained) {
        self.engine.retained(stats);
        stats.add(format!("moves/{}", self.mesh), self.moves.len());
        for clock in &self.moves {
            stats.clock(clock);
        }
        for (_, clock) in &self.ends {
            stats.clock(clock);
        }
        stats.add(format!("payloads/{}", self.mesh), self.payloads.len());
        stats.add(format!("steps/{}", self.mesh), self.steps.len());
        stats.add(
            format!("dispatch_logs/{}", self.mesh),
            self.segments
                .iter()
                .map(|s| s.logs.iter().map(Vec::len).sum::<usize>())
                .sum(),
        );
        eprintln!(
            "verify node {}: pc={} step={:?}",
            self.mesh,
            self.at,
            self.steps.get(self.at)
        );
    }

    pub(crate) fn owns_process(&self, process: usize) -> bool {
        [self.proc, self.mover_proc, self.engine.proc].contains(&process)
    }

    pub(crate) fn pending_engine(&self) -> Option<usize> {
        self.engine.pending().then_some(self.engine.proc)
    }

    /// Forward targets and package ends are the possible joins of forked paths.
    pub fn mark_joins(&mut self) {
        let joins: Vec<usize> = self
            .steps
            .iter()
            .enumerate()
            .filter_map(|(at, step)| match step {
                NStep::Op(s) | NStep::JoinOp(s) if s.op == op::JUMP && s.unit as usize > at => {
                    Some(s.unit as usize)
                }
                NStep::Op(s) | NStep::JoinOp(s) if s.op == op::END => Some(at),
                _ => None,
            })
            .collect();
        for at in joins {
            if let Some(step) = self.steps.get_mut(at) {
                *step = match *step {
                    NStep::Seg(s) | NStep::JoinSeg(s) => NStep::JoinSeg(s),
                    NStep::Op(s) | NStep::JoinOp(s) => NStep::JoinOp(s),
                };
            }
        }
    }

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
        let mover_proc = self.add_proc(Pk::Port, format!("{name} mover"));
        let engine_proc = self.add_proc(Pk::Port, format!("{name} dispatch engine"));
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
            mover: crate::lines::Mover::default(),
            mover_proc,
            moves: Vec::new(),
            move_base: 0,
            engine: engine::Engine::new(engine_proc, n, &self.t.machine),
            mesh,
            name: name.clone(),
            pkgs: Vec::new(),
            raw_steps: Vec::new(),
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
            frontiers: HashMap::default(),
            payloads: Vec::new(),
            locations: Vec::new(),
            step_sources: HashMap::default(),
            payload_bases: Vec::new(),
            step_targets: HashMap::default(),
            steps: Vec::new(),
            segments: Vec::new(),
            at: 0,
            branches: HashMap::default(),
            armed: false,
            fetched: 0,
            why: String::new(),
            regs: HashMap::default(),
            flush: None,
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
            let storage = self.card.context.packages.get(&(mesh, i));
            let source =
                storage
                    .map(|s| Location::Physical(s.payloads))
                    .unwrap_or(Location::Private {
                        mesh,
                        run: i,
                        section: Section::Payloads,
                        offset: 0,
                    });
            let first = node.locations.len();
            for k in first..node.payloads.len() {
                node.locations.push(source.offset((k - first) as u64 * 32));
            }
        }
        node.raw_steps = pkgs.iter().map(|p| p.steps.clone()).collect();
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
            NStep::Seg(s) | NStep::JoinSeg(s) => {
                let seg = &nd.segments[s];
                format!(
                    "segment at package {} step {}: {}",
                    seg.pkg, seg.start, nd.why
                )
            }
            NStep::Op(s) | NStep::JoinOp(s) => {
                format!("step {} {} u{}: {}", nd.at, op::name(s.op), s.unit, nd.why)
            }
        }
    }

    fn retired(&self, i: usize, u: usize) -> i64 {
        let unit = self.nodes[i].units[u].expect("a unit");
        self.counters[self.units[unit].done].value
    }

    pub fn node_step(&mut self, i: usize) -> StepOutcome {
        let engine = match self.engine_dispatch(i) {
            Ok(progress) => progress,
            Err(why) => {
                self.find(Kind::Model, self.nodes[i].name.clone(), why);
                return StepOutcome::Terminal;
            }
        };
        match self.node_cpu_step(i) {
            StepOutcome::Blocked if engine => StepOutcome::Progress,
            result => result,
        }
    }

    fn node_cpu_step(&mut self, i: usize) -> StepOutcome {
        let p = self.nodes[i].proc;
        if self.nodes[i].done() {
            return StepOutcome::Blocked;
        }
        let host = self.nodes[i].host;
        if host > 0 && self.card.context.independent_packages
            && (self.card.context.host_join_every == 0
                || host % self.card.context.host_join_every != 0)
        {
            // HOSTL advances this node after its own END, without importing
            // another node's completion clock. Local END still closes register,
            // dispatch, fetch and mover state in the ordinary way below.
            self.nodes[i].host = 0;
        } else if host > 0 {
            // The host submits the next run once every node ended this one.
            let c = self.counter(&format!("host:{host}"));
            let need = self.node_of.len() as i64;
            if !self.counters[c].reached(need) {
                self.nodes[i].why = "the host waits for every node's package".into();
                return StepOutcome::Blocked;
            }
            let cl = self.clock_to(c, need, &[]);
            self.tick(p, &[cl]);
            self.nodes[i].host = 0;
            self.enter_package(host);
        }
        if let Some(step) = self.nodes[i].engine.step {
            return self.node_engine(i, step);
        }
        if let Err(why) = self.node_step_sources(i) {
            self.find(Kind::Model, self.nodes[i].name.clone(), why);
            return StepOutcome::Terminal;
        }
        let s = match self.nodes[i].steps[self.nodes[i].at] {
            NStep::Seg(s) => {
                if !self.engine_drain(i) {
                    return StepOutcome::Blocked;
                }
                return self.segment(i, s).into();
            }
            NStep::Op(s) => s,
            NStep::JoinSeg(s) => {
                let nd = &mut self.nodes[i];
                nd.steps[nd.at] = NStep::Seg(s);
                return StepOutcome::Join;
            }
            NStep::JoinOp(s) => {
                let nd = &mut self.nodes[i];
                nd.steps[nd.at] = NStep::Op(s);
                return StepOutcome::Join;
            }
        };
        if !matches!(s.op, op::ENGINE | op::ALU | op::JUMP | op::PATCH) {
            if !self.engine_drain(i) {
                return StepOutcome::Blocked;
            }
        }
        if let Err(why) = self.node_payload_sources(i, s) {
            self.find(Kind::Model, self.nodes[i].name.clone(), why);
            return StepOutcome::Terminal;
        }
        let progress = match s.op {
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
                        return StepOutcome::Blocked;
                    }
                    rel.push(self.clock_to(done, sent, &[]));
                }
                if let Some(done) = self.nodes[i].moves.last() {
                    rel.push(done.clone());
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
                    nd.flush = None;
                    nd.move_base = nd.moves.len();
                    nd.engine.end(&nd.sent);
                    let k = nd.ends.len();
                    nd.host = k;
                    let c = self.counter(&format!("host:{k}"));
                    self.counters[c].add(1, &clock, p);
                }
                true
            }
            op::FRONTIER if s.flags != 1 => {
                let nd = &mut self.nodes[i];
                let valid = match s.flags {
                    0 if s.arg == 0
                        && (s.count as usize) < nd.payloads.len()
                        && (s.unit as usize) < nd.sent.len() =>
                    {
                        nd.frontiers
                            .insert(s.count, (s.unit, nd.sent[s.unit as usize]));
                        true
                    }
                    2 if s.unit == 0
                        && s.arg <= nd.payloads.len() as u64
                        && u64::from(s.count) <= nd.payloads.len() as u64 - s.arg =>
                    {
                        let end = s.arg + u64::from(s.count);
                        nd.frontiers
                            .retain(|&k, _| !(s.arg..end).contains(&u64::from(k)));
                        true
                    }
                    _ => false,
                };
                if !valid {
                    let name = nd.name.clone();
                    self.find(Kind::Model, name, "invalid FRONTIER fields");
                    return StepOutcome::Terminal;
                }
                self.tick(p, &[]);
                self.nodes[i].at += 1;
                true
            }
            op::AWAIT | op::FRONTIER => {
                let u = s.unit as usize;
                if s.op == op::FRONTIER && u >= self.nodes[i].units.len() {
                    let name = self.nodes[i].name.clone();
                    self.find(Kind::Model, name, "FRONTIER wait names an absent unit");
                    return StepOutcome::Terminal;
                }
                let Some(unit) = self.nodes[i].units.get(u).copied().flatten() else {
                    self.nodes[i].at += 1;
                    return StepOutcome::Progress;
                };
                if !self.nodes[i].armed {
                    if s.op == op::FRONTIER {
                        let saved = self.nodes[i].frontiers.get(&s.count).copied();
                        let Some((_, target)) = saved.filter(|(unit, _)| *unit == s.unit) else {
                            let name = self.nodes[i].name.clone();
                            self.find(Kind::Model, name, "FRONTIER wait has no saved target");
                            return StepOutcome::Terminal;
                        };
                        if s.arg != 0 || (s.count as usize) >= self.nodes[i].payloads.len() {
                            let name = self.nodes[i].name.clone();
                            self.find(Kind::Model, name, "invalid FRONTIER wait fields");
                            return StepOutcome::Terminal;
                        }
                        self.nodes[i].expected[u] = self.nodes[i].expected[u].max(target);
                    } else if s.flags == flag::IMM {
                        self.nodes[i].expected[u] = self.nodes[i].sent[u];
                    } else {
                        self.nodes[i].expected[u] += s.count as i64;
                    }
                    self.nodes[i].armed = true;
                }
                let done = self.units[unit].done;
                let want = self.nodes[i].expected[u];
                if !self.counters[done].reached(want) {
                    self.nodes[i].why = format!(
                        "{}: {} of {} completions",
                        self.units[unit].name, self.counters[done].value, want
                    );
                    return StepOutcome::Blocked;
                }
                // L0.STEP.020/.021 and L0.DE.006/.009: the saved/delta
                // frontier consumes instruction-completion signals, not burst
                // ACKs. Only sent instructions can contribute to this wait.
                let cap = [(self.units[unit].completer, self.nodes[i].sent[u])];
                let cl = self.clock_to(done, want, &cap);
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
                    return StepOutcome::Blocked;
                }
                let cl = self.clock_to(rx, want, &[]);
                let clock = self.tick(p, &[cl]);
                self.node_memory(i, s.arg as usize, u64::from(s.count) * 32, true, &clock);
                self.nodes[i].rx_taken = want;
                self.nodes[i].at += 1;
                true
            }
            op::ENGINE => return self.node_engine(i, s),
            op::RING | op::WAIT_BELL | op::WAIT_SLOT | op::FETCH | op::SEND => {
                self.node_other(i, s)
            }
            op::LOAD | op::ALU | op::JUMP => return self.node_register(i, s),
            op::ASSERT_ZERO => return self.node_assert_zero(i, s),
            op::MOVER | op::MWAIT => self.node_mover(i, s),
            op::STORE => self.node_store(i, s),
            op::PATCH => self.node_patch(i, s),
            op::CTRL => {
                let clock = self.tick(p, &[]);
                if matches!(
                    self.t.facts.node_poll,
                    Some(NodePollContract::Rv64SysDcache)
                ) {
                    poll::request(&mut self.nodes[i], s, &clock);
                }
                self.nodes[i].at += 1;
                true
            }
            op::SETTLE | op::SIGNAL => {
                self.tick(p, &[]);
                self.nodes[i].at += 1;
                true
            }
            _ => self.node_other(i, s),
        };
        if progress
            && crate::composition::active()
            && matches!(s.op, op::END | op::BARRIER | op::WAIT_SLOT | op::WAIT_BELL)
        {
            // Only retire against the existing future-access lower bound. A
            // local barrier is not a global edge and cannot reset visibility.
            self.retire_memory();
        }
        progress.into()
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
                let mut w = self.nodes[i].payloads[arg + (sent + j) as usize];
                let index = arg + (sent + j) as usize;
                let location = self.nodes[i].locations[index].clone();
                match self.package_value(&location, w, 32, &clock) {
                    Ok(value) => w = value,
                    Err(why) => self.find(Kind::Model, self.nodes[i].name.clone(), why),
                }
                self.deliver(unit, w, clock.clone());
                if self.nodes[i].fetch[u] {
                    if let Some(word) = self.units[unit].words.last_mut() {
                        word.source = Some(location);
                    }
                } else {
                    self.package_access(
                        &location,
                        32,
                        false,
                        &clock,
                        &clock,
                        p,
                        format!("{} dispatch payload {index}", self.nodes[i].name),
                    );
                }
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
                let sent = self.tick(p, &[]);
                let clock = if s.arg & (1 << 16) != 0 {
                    let mut arrival = self.release_clock(mesh, u32::from(s.unit), &sent);
                    if let Some(done) = self.nodes[i].moves.last() {
                        arrival.join(done);
                    }
                    arrival
                } else {
                    sent
                };
                let slot = self.counter(&format!("slot:{}:{}", s.unit, s.count));
                self.counters[slot].add(((s.arg & 0xFFFF) as i64).max(1), &clock, p);
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
                let cl = self.node_counter_clock(c, want, p);
                self.tick(p, &[cl]);
                if s.op == op::WAIT_BELL || s.flags & flag::CONSUME != 0 {
                    self.taken.insert((mesh, key), want);
                }
                self.nodes[i].at += 1;
                true
            }
            op::SEND => self.node_send(i, s),
            op::FETCH => self.node_fetch(i, s),
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

    /// ASSERT_ZERO continues only under its successful runtime contract. It
    /// reads a register, not memory, and supplies no synchronization edge.
    fn node_assert_zero(&mut self, i: usize, s: Step) -> StepOutcome {
        let nd = &self.nodes[i];
        let value = nd.regs.get(&s.unit).copied().unwrap_or(Value::Known(0));
        let condition = self.symbolic.condition(1, value, Value::Known(0));
        if !self.symbolic.try_assume(condition) {
            self.find(
                Kind::Model,
                self.nodes[i].name.clone(),
                format!("ASSERT_ZERO contract {} cannot succeed", s.count),
            );
            return StepOutcome::Terminal;
        }
        let p = self.nodes[i].proc;
        self.tick(p, &[]);
        self.nodes[i].regs.insert(s.unit, Value::Known(0));
        self.nodes[i].at += 1;
        StepOutcome::Progress
    }

    /// Register expressions retain their identities across forward successors.
    fn node_register(&mut self, i: usize, s: Step) -> StepOutcome {
        let p = self.nodes[i].proc;
        let clock = self.tick(p, &[]);
        if s.op == op::LOAD && s.flags != flag::IMM && s.flags & ktpu_l0::format::F_CTRL == 0 {
            let footprint = self.nodes[i]
                .payloads
                .get(s.arg as usize)
                .ok_or_else(|| "LOAD names an absent payload".to_string())
                .and_then(|w| {
                    let offset = if s.flags & ktpu_l0::format::F_INDEXED != 0 {
                        match self.nodes[i].regs.get(&((s.count & 15) as u16)) {
                            Some(Value::Symbolic(_)) => return Err("unknown LOAD footprint".into()),
                            Some(Value::Known(v)) => *v,
                            None => 0,
                        }
                    } else {
                        0
                    };
                    self.cpu_interval(w.bits(0, 64).wrapping_add(offset), 8)
                });
            self.dram_access(
                footprint,
                false,
                &clock,
                &clock,
                p,
                format!("{} LOAD step {}", self.nodes[i].name, self.nodes[i].at),
            );
        }
        let loaded =
            if s.op == op::LOAD && s.flags != flag::IMM && s.flags & ktpu_l0::format::F_CTRL == 0 {
                self.nodes[i].payloads.get(s.arg as usize).and_then(|word| {
                    let offset = if s.flags & ktpu_l0::format::F_INDEXED != 0 {
                        match self.nodes[i].regs.get(&((s.count & 15) as u16)) {
                            Some(Value::Known(v)) => *v,
                            Some(Value::Symbolic(_)) => return None,
                            None => 0,
                        }
                    } else {
                        0
                    };
                    self.card.values.get(
                        self.physical(word.bits(0, 64).wrapping_add(offset)),
                        8,
                        &clock,
                    )
                })
            } else {
                None
            };
        let nd = &mut self.nodes[i];
        if s.op == op::LOAD {
            let Some(word) = nd.payloads.get(s.arg as usize) else {
                let name = nd.name.clone();
                self.find(Kind::Model, name, "LOAD names an absent payload");
                return StepOutcome::Terminal;
            };
            let base = word.bits(0, 64);
            let value = if s.flags == flag::IMM {
                Value::Known(base)
            } else if let Some(value) = loaded {
                Value::Known(value)
            } else if s.flags == ktpu_l0::format::F_CTRL
                && base == poll::DCACHE
                && matches!(
                    self.t.facts.node_poll,
                    Some(NodePollContract::Rv64SysDcache)
                )
            {
                let sample = self.symbolic.fresh();
                self.symbolic
                    .alu(4, sample, Value::Known(1))
                    .expect("status bit")
            } else {
                self.symbolic.fresh()
            };
            nd.regs.insert(s.unit, value);
            nd.at += 1;
            return StepOutcome::Progress;
        }
        let reg = |r: u32| nd.regs.get(&(r as u16)).copied().unwrap_or(Value::Known(0));
        let a = reg((s.count >> 8) & 0xF);
        let b = if s.flags & flag::IMM != 0 {
            Value::Known(s.arg)
        } else {
            reg((s.count >> 12) & 0xF)
        };
        if s.op == op::ALU {
            if self.stats_enabled && !matches!((a, b), (Value::Known(_), Value::Known(_))) {
                eprintln!(
                    "verify symbolic ALU: node={} pc={} op={} a={a:?} b={b:?} storage={:?}",
                    nd.mesh,
                    nd.at,
                    s.count & 0xFF,
                    self.symbolic.storage()
                );
            }
            let v = match (a, b) {
                (Value::Known(a), Value::Known(b)) => alu(s.count & 0xFF, a, b).map(Value::Known),
                _ => self.symbolic.alu(s.count & 0xFF, a, b),
            };
            let Some(v) = v else {
                let name = nd.name.clone();
                self.find(Kind::Model, name, "invalid ALU operation");
                return StepOutcome::Terminal;
            };
            nd.regs.insert(s.unit, v);
            nd.at += 1;
            return StepOutcome::Progress;
        }
        let c = s.count & 0xFF;
        let go = if c == 0 {
            true
        } else {
            match (a, b) {
                (Value::Known(a), Value::Known(b)) => cond(c, a, b),
                _ => {
                    let at = nd.at;
                    if s.unit as usize > at {
                        let condition = self.symbolic.condition(c, a, b);
                        let (taken, fallthrough) = self.symbolic.successors(condition);
                        return StepOutcome::Fork {
                            node: i,
                            target: s.unit as usize,
                            taken,
                            fallthrough,
                        };
                    }
                    if matches!(
                        self.t.facts.node_poll,
                        Some(NodePollContract::Rv64SysDcache)
                    ) {
                        if let Some(exit) = poll::summarize(nd, &mut self.symbolic, s) {
                            if let poll::Exit::Flushed(visibility) = exit {
                                self.procs[p].clock.join(&visibility);
                            }
                            return StepOutcome::Progress;
                        }
                    }
                    let name = nd.name.clone();
                    self.find(
                        Kind::Loop,
                        name,
                        format!(
                            "step {at}: data-dependent backward branch requires path exploration"
                        ),
                    );
                    return StepOutcome::Terminal;
                }
            }
        };
        let nd = &mut self.nodes[i];
        nd.at = if go { s.unit as usize } else { nd.at + 1 };
        StepOutcome::Progress
    }

    /// Raw flits to a unit (interp.c send): a CU_DATA descriptor and its
    /// words, read from memory under MEM; with ACK the step waits for one
    /// more DATA_RECEIVED from the unit.
    fn node_send(&mut self, i: usize, s: Step) -> bool {
        let p = self.nodes[i].proc;
        let unit = self.nodes[i].units.get(s.unit as usize).copied().flatten();
        if !self.nodes[i].armed {
            let kind = (s.arg >> 32) & 0xF;
            let clock = self.tick(p, &[]);
            if s.flags & flag::MEM != 0 {
                self.node_memory(
                    i,
                    (s.arg & 0xFFFF_FFFF) as usize + 1,
                    u64::from(s.count) * 32,
                    false,
                    &clock,
                );
            }
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
                    self.send(Sender::Node(i), sink, d.bits(232, 16) as i64, n, &clock, p);
                }
                (7, Some(u)) => {
                    // CU_CTRL: the unit's control port answers each flit with one.
                    let rx = self.nodes[i].rx;
                    let port = self.units[u].port;
                    self.counters[rx].add(s.count as i64, &clock, port);
                }
                _ => {}
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

    fn node_fetch(&mut self, i: usize, s: Step) -> bool {
        let p = self.nodes[i].proc;
        let u = s.unit as usize;
        let Some(unit) = self.nodes[i].units.get(u).copied().flatten() else {
            self.find(
                Kind::Model,
                self.nodes[i].name.clone(),
                "FETCH names an absent unit",
            );
            self.nodes[i].at += 1;
            return true;
        };
        let limit = self.nodes[i].credit[u]
            .min(self.t.facts.fetch_depth)
            .min(FETCH_RUN);
        let n = i64::from(s.count - self.nodes[i].fetched).min(limit);
        let need = self.nodes[i].sent[u] + n - limit;
        if !self.counters[self.units[unit].done].reached(need) {
            self.nodes[i].why = "FETCH waits for instruction credits".into();
            return false;
        }
        let released = self.clock_to(self.units[unit].done, need, &[]);
        let clock = self.tick(p, &[released]);
        for k in 0..n as u32 {
            let at = s.arg + u64::from(self.nodes[i].fetched + k) * 32;
            let mut word = U256([0; 4]);
            for limb in 0..4 {
                let physical = self.physical(at + limb * 8);
                let Some(value) = self.card.values.get(physical, 8, &clock) else {
                    self.find(
                        Kind::Model,
                        self.nodes[i].name.clone(),
                        "FETCH instruction has no visible known bytes",
                    );
                    self.nodes[i].at += 1;
                    self.nodes[i].fetched = 0;
                    return true;
                };
                word.set_bits(limb as u32 * 64, 64, value);
            }
            self.deliver(unit, word, clock.clone());
            if let Some(word) = self.units[unit].words.last_mut() {
                word.source = Some(Location::Physical(at));
            }
        }
        self.nodes[i].sent[u] += n;
        self.nodes[i].fetched += n as u32;
        if self.nodes[i].fetched == s.count {
            self.nodes[i].fetched = 0;
            self.nodes[i].at += 1;
        }
        true
    }

    fn node_step_sources(&mut self, i: usize) -> Result<(), String> {
        let nd = &self.nodes[i];
        let sources = nd.step_sources.get(&nd.at).cloned().unwrap_or_default();
        let p = nd.proc;
        let clock = self.tick(p, &[]);
        for (run, raw) in sources {
            let nd = &self.nodes[i];
            let s = nd.raw_steps[run][raw];
            let location = self.step_location(i, run, raw);
            let word = U256([
                u64::from(s.op)
                    | (u64::from(s.flags) << 8)
                    | (u64::from(s.unit) << 16)
                    | (u64::from(s.count) << 32),
                s.arg,
                0,
                0,
            ]);
            self.package_access(
                &location,
                16,
                false,
                &clock,
                &clock,
                p,
                format!("sysnode {} step {raw}", self.nodes[i].mesh),
            );
            let value = self.package_value(&location, word, 16, &clock)?;
            if value != word {
                let changed = Step::new(
                    value.bits(0, 8) as u8,
                    value.bits(16, 16) as u16,
                    value.bits(32, 32) as u32,
                    value.bits(64, 64),
                    value.bits(8, 8) as u8,
                );
                self.replace_step(i, run, raw, changed)?;
            }
        }
        Ok(())
    }

    fn step_location(&self, i: usize, run: usize, raw: usize) -> Location {
        let nd = &self.nodes[i];
        match self.card.context.packages.get(&(nd.mesh, run)) {
            Some(storage) => Location::Physical(storage.steps + raw as u64 * 16),
            None => Location::Private {
                mesh: nd.mesh,
                run,
                section: Section::Steps,
                offset: raw as u64 * 16,
            },
        }
    }

    fn replace_step(
        &mut self,
        i: usize,
        run: usize,
        raw: usize,
        mut s: Step,
    ) -> Result<(), String> {
        let nd = &mut self.nodes[i];
        let at = *nd
            .step_targets
            .get(&(run, raw))
            .ok_or("PATCH step target absent")?;
        if let NStep::Seg(segment) | NStep::JoinSeg(segment) = nd.steps[at] {
            if s.op != op::DISPATCH {
                return Err("patched dispatch changes its control-flow class".into());
            }
            let index = raw - nd.segments[segment].start;
            let item = nd.segments[segment]
                .items
                .get_mut(index)
                .ok_or("patched dispatch item absent")?;
            item.unit = s.unit as usize;
            item.arg = nd.payload_bases[run] + s.arg as usize;
            item.count = i64::from(s.count);
            nd.raw_steps[run][raw] = s;
            return Ok(());
        }
        nd.raw_steps[run][raw] = s;
        if s.op == op::JUMP {
            s.unit = *nd
                .step_targets
                .get(&(run, s.unit as usize))
                .ok_or("patched JUMP target absent")? as u16;
        }
        if matches!(
            s.op,
            op::LOAD | op::STORE | op::MOVER | op::SEND | op::RECV | op::ENGINE
        ) {
            s.arg += nd.payload_bases[run] as u64;
        }
        if s.op == op::PATCH && s.arg & PATCH_STEPS == 0 {
            s.count += nd.payload_bases[run] as u32;
        }
        nd.steps[at] = NStep::Op(s);
        Ok(())
    }

    fn node_payload_sources(&mut self, i: usize, s: Step) -> Result<(), String> {
        let (first, count) = match s.op {
            op::LOAD | op::RECV => (s.arg as usize, 1),
            op::STORE | op::MOVER | op::CTRL => (s.arg as usize, s.count as usize),
            op::SEND => (
                (s.arg & 0xFFFF_FFFF) as usize,
                if s.flags & flag::MEM != 0 {
                    2
                } else {
                    s.count as usize
                },
            ),
            _ => return Ok(()),
        };
        let p = self.nodes[i].proc;
        let clock = self.tick(p, &[]);
        for index in first..first + count {
            let location = self.nodes[i]
                .locations
                .get(index)
                .cloned()
                .ok_or("payload source outside package")?;
            let initial = self.nodes[i].payloads[index];
            self.package_access(
                &location,
                32,
                false,
                &clock,
                &clock,
                p,
                format!("{} payload {index}", self.nodes[i].name),
            );
            self.nodes[i].payloads[index] = self.package_value(&location, initial, 32, &clock)?;
        }
        Ok(())
    }

    fn node_mover(&mut self, i: usize, s: Step) -> bool {
        let p = self.nodes[i].proc;
        if s.op == op::MWAIT {
            let target = self.nodes[i].move_base + s.count as usize;
            if target > self.nodes[i].moves.len() {
                self.nodes[i].why = format!("mover completion {target} not issued");
                return false;
            }
            let rel = if target == 0 {
                Vec::new()
            } else {
                vec![self.nodes[i].moves[target - 1].clone()]
            };
            self.tick(p, &rel);
        } else {
            let issue = self.tick(p, &[]);
            for k in 0..s.count as usize {
                let Some(word) = self.nodes[i].payloads.get(s.arg as usize + k).copied() else {
                    self.find(
                        Kind::Model,
                        self.nodes[i].name.clone(),
                        "MOVER payload out of bounds",
                    );
                    break;
                };
                for half in [0, 128] {
                    let reg = word.bits(half, 64);
                    if reg == ktpu_l0::format::MOVER_SKIP {
                        continue;
                    }
                    let value = word.bits(half + 64, 64);
                    self.mover_write(i, reg, value, &issue);
                }
            }
            if s.flags & ktpu_l0::format::F_POSTED == 0 {
                let rel: Vec<_> = self.nodes[i].moves.last().cloned().into_iter().collect();
                self.tick(p, &rel);
            }
        }
        self.nodes[i].at += 1;
        true
    }

    fn mover_write(&mut self, i: usize, reg: u64, value: u64, issue: &Clock) {
        let address = self.t.machine.die.address.runtime();
        let special = 1u64 << address.special_bit;
        let routes = ((1u64 << address.mesh_bits) - 1) << address.mesh_shift;
        let mut seen = self.clock_of(self.nodes[i].mover_proc);
        seen.join(issue);
        let values = &self.card.values;
        let footprints = self.nodes[i].mover.command(reg, value, self.t, |at| {
            let at = if at & special == 0 {
                at & !routes & !(1u64 << address.reserved_bit)
            } else {
                at
            };
            values.get(at, 4, &seen).map(|value| value as u32)
        });
        let footprints = match footprints {
            Ok(None) => return,
            Ok(Some(footprints)) => footprints,
            Err(why) => {
                self.find(Kind::Model, self.nodes[i].name.clone(), why);
                return;
            }
        };
        let mp = self.nodes[i].mover_proc;
        let start = self.tick(mp, std::slice::from_ref(issue));
        let read = self.tick(mp, &[]);
        let done = self.tick(mp, &[]);
        let what = format!(
            "{} mover GO {}",
            self.nodes[i].name,
            self.nodes[i].moves.len()
        );
        self.dram_access(
            footprints.reads,
            false,
            &start,
            &read,
            mp,
            format!("{what} read"),
        );
        let acknowledged = self.memory_write(
            self.nodes[i].mesh,
            None,
            footprints.writes,
            &read,
            &done,
            mp,
            format!("{what} write"),
        );
        // ISA.MV.011/.080: the next GO starts after I_DRAIN
        // has consumed local responses. L0.STEP.026/.027 acquire
        // this completion only at a nonposted move or MWAIT.
        self.procs[mp].clock.join(&acknowledged);
        self.nodes[i].moves.push(acknowledged);
    }

    fn cpu_interval(&self, base: u64, bytes: u64) -> Result<super::dram::Footprint, String> {
        let address = self.t.machine.die.address.runtime();
        let base = if base & (1u64 << address.special_bit) == 0 {
            base & !(1u64 << address.reserved_bit)
        } else {
            base
        };
        super::dram::interval(base, bytes)
    }

    fn node_memory(&mut self, i: usize, payload: usize, bytes: u64, write: bool, clock: &Clock) {
        let nd = &self.nodes[i];
        let footprint = nd
            .payloads
            .get(payload)
            .ok_or_else(|| "memory operation names an absent payload".to_string())
            .and_then(|w| self.cpu_interval(w.bits(0, 64), bytes));
        let (p, mesh) = (nd.proc, nd.mesh);
        let what = format!("{} memory step {}", nd.name, nd.at);
        if write {
            let ack = self.memory_write(mesh, None, footprint, clock, clock, p, what);
            self.procs[p].clock.join(&ack);
        } else {
            self.dram_access(footprint, false, clock, clock, p, what);
        }
    }

    fn node_store(&mut self, i: usize, s: Step) -> bool {
        let p = self.nodes[i].proc;
        let clock = self.tick(p, &[]);
        let mut footprint = Ok(Vec::new());
        for k in 0..s.count as usize {
            let Some(w) = self.nodes[i].payloads.get(s.arg as usize + k) else {
                footprint = Err("STORE payload out of bounds".into());
                break;
            };
            for half in [0, 128] {
                let base = w.bits(half, 64);
                if base != ktpu_l0::format::MOVER_SKIP {
                    footprint = footprint.and_then(|mut ranges| {
                        ranges.extend(self.cpu_interval(base, 8)?);
                        Ok(ranges)
                    });
                }
            }
        }
        let ack = self.memory_write(
            self.nodes[i].mesh,
            None,
            footprint,
            &clock,
            &clock,
            p,
            format!("{} STORE step {}", self.nodes[i].name, self.nodes[i].at),
        );
        self.procs[p].clock.join(&ack);
        for k in 0..s.count as usize {
            let Some(w) = self.nodes[i].payloads.get(s.arg as usize + k).copied() else {
                break;
            };
            for half in [0, 128] {
                let address = w.bits(half, 64);
                if address == ktpu_l0::format::MOVER_SKIP {
                    continue;
                }
                let value = w.bits(half + 64, 64);
                let value = if s.flags & ktpu_l0::format::F_REG != 0 {
                    match self.nodes[i].regs.get(&((value & 15) as u16)) {
                        Some(Value::Known(value)) => Some(*value),
                        Some(Value::Symbolic(_)) => None,
                        None => Some(0),
                    }
                } else {
                    Some(value)
                };
                if let Some(value) = value {
                    self.stored_value(self.nodes[i].mesh, p, address, &value.to_le_bytes());
                }
            }
        }
        self.nodes[i].at += 1;
        true
    }

    fn node_patch(&mut self, i: usize, s: Step) -> bool {
        if let Err(why) = self.patch_value(i, s) {
            self.find(Kind::Model, self.nodes[i].name.clone(), why);
        }
        self.nodes[i].at += 1;
        true
    }

    fn patch_value(&mut self, i: usize, s: Step) -> Result<(), String> {
        let p = self.nodes[i].proc;
        let clock = self.tick(p, &[]);
        let bit = (s.arg & 255) as u32;
        let width = ((s.arg >> 8) & 255) as u32;
        let shift = ((s.arg >> 16) & 255) as u32;
        let steps = s.arg & PATCH_STEPS != 0;
        if width == 0 || width > 64 || shift >= 64 || bit + width > if steps { 128 } else { 256 } {
            return Err("invalid PATCH field".into());
        }
        let run = self.nodes[i]
            .step_sources
            .get(&self.nodes[i].at)
            .and_then(|sources| sources.first())
            .map(|&(run, _)| run)
            .ok_or("PATCH has no package origin")?;
        let (location, initial) = if steps {
            let original = self.nodes[i].raw_steps[run]
                .get(s.count as usize)
                .ok_or("PATCH step out of bounds")?;
            (
                self.step_location(i, run, s.count as usize),
                U256([
                    u64::from(original.op)
                        | (u64::from(original.flags) << 8)
                        | (u64::from(original.unit) << 16)
                        | (u64::from(original.count) << 32),
                    original.arg,
                    0,
                    0,
                ]),
            )
        } else {
            (
                self.nodes[i]
                    .locations
                    .get(s.count as usize)
                    .cloned()
                    .ok_or("PATCH payload out of bounds")?,
                *self.nodes[i]
                    .payloads
                    .get(s.count as usize)
                    .ok_or("PATCH payload out of bounds")?,
            )
        };
        let mut word =
            self.package_value(&location, initial, if steps { 16 } else { 32 }, &clock)?;
        let value = match self.nodes[i].regs.get(&s.unit) {
            Some(Value::Known(value)) => *value >> shift,
            Some(Value::Symbolic(_)) => {
                return Err("unknown patched footprint requires an ownership proof".into())
            }
            None => 0,
        };
        let value = if s.arg & (1 << 26) != 0 {
            word.bits(bit, width).wrapping_add(value)
        } else {
            value
        };
        let first = bit / 64;
        let limbs = (bit + width - 1) / 64 - first + 1;
        let touched = location.offset(u64::from(first) * 8);
        self.package_access(
            &touched,
            u64::from(limbs) * 8,
            false,
            &clock,
            &clock,
            p,
            format!("{} PATCH read", self.nodes[i].name),
        );
        word.set_bits(bit, width, value);
        self.package_access(
            &touched,
            u64::from(limbs) * 8,
            true,
            &clock,
            &clock,
            p,
            format!("{} PATCH write", self.nodes[i].name),
        );
        if let Location::Physical(base) = location {
            for limb in first..first + limbs {
                self.stored_value(
                    self.nodes[i].mesh,
                    p,
                    base + u64::from(limb) * 8,
                    &word.bits(limb * 64, 64).to_le_bytes(),
                );
            }
        }
        if steps {
            let step = Step::new(
                word.bits(0, 8) as u8,
                word.bits(16, 16) as u16,
                word.bits(32, 32) as u32,
                word.bits(64, 64),
                word.bits(8, 8) as u8,
            );
            self.replace_step(i, run, s.count as usize, step)?;
        } else {
            self.nodes[i].payloads[s.count as usize] = word;
        }
        Ok(())
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
        self.payload_bases.push(base);
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
                self.step_sources
                    .entry(self.steps.len() - 1)
                    .or_default()
                    .push((index, j));
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
            self.step_sources
                .entry(self.steps.len())
                .or_default()
                .push((index, j));
            if s.op == op::JUMP {
                self.branches.insert(self.steps.len(), (index, jumps.len()));
                jumps.push(self.steps.len());
            }
            let mut s = *s;
            if matches!(
                s.op,
                op::SEND | op::RECV | op::LOAD | op::STORE | op::MOVER | op::ENGINE
            ) {
                s.arg += base as u64;
            }
            if s.op == op::PATCH && s.arg & PATCH_STEPS == 0 {
                s.count += base as u32;
            }
            if s.op == op::FRONTIER {
                if s.flags == 2 {
                    s.arg += base as u64;
                } else {
                    s.count += base as u32;
                }
            }
            self.steps.push(NStep::Op(s));
        }
        let ends = matches!(self.steps.last(), Some(NStep::Op(s)) if s.op == op::END);
        if self.steps.len() == first || !ends {
            at.insert(pkg.steps.len(), self.steps.len());
            self.steps.push(NStep::Op(Step::new(op::END, 0, 0, 0, 0)));
        }
        for (&raw, &lowered) in &at {
            self.step_targets.insert((index, raw), lowered);
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
