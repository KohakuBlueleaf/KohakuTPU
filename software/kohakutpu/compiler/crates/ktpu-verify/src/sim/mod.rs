//! The abstract machine a package is checked on (verify.md): sequential
//! processes whose every wait is a grown counter reaching a threshold fixed
//! by the waiter's own history, so running one schedule to its end decides
//! deadlock for all (Theorem 1). The card holds the units by coordinate, the
//! nodes that feed them and the routes between them.

use std::collections::BTreeMap;

use indexmap::IndexMap;

use fx::{FxHashMap as HashMap, FxHashSet as HashSet};

use crate::adapter::u256::U256;
use crate::model::clock::Clock;
use crate::model::counter::Counter;
use crate::model::finding::{Finding, Kind};
use crate::target::{Target, UnitKind};

pub(crate) mod card;
pub(crate) mod dram;
mod feedback;
pub(crate) mod fx;
pub(crate) mod memory;
pub(crate) mod node;
pub(crate) mod package_memory;
pub(crate) mod report;
mod retire;
pub(crate) mod stats;
pub(crate) mod symbolic;
pub(crate) mod units;
pub(crate) mod values;
pub(crate) mod visibility;

use memory::Memory;
use node::Node;
use units::cluster::Cluster;
use units::row::Row;
use units::vector::Vector;

/// Events a run may take before it is cut off (a `model` finding).
pub const EVENT_LIMIT: u64 = 200_000_000;

/// What a process is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Pk {
    /// A unit's receive port, or a cluster's array: events, no actions.
    Port,
    Node(usize),
    MgSeq(usize),
    MgDq(usize),
    VcCu(usize),
    VcFe(usize),
    VcEng(usize, usize),
    SrShell(usize),
}

pub(crate) enum StepOutcome {
    Progress,
    Blocked,
    Terminal,
    Join,
    Fork {
        node: usize,
        target: usize,
        taken: symbolic::Predicate,
        fallthrough: symbolic::Predicate,
    },
}

impl From<bool> for StepOutcome {
    fn from(progress: bool) -> Self {
        if progress {
            Self::Progress
        } else {
            Self::Blocked
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
pub(crate) struct Proc {
    pub kind: Pk,
    pub name: String,
    pub clock: Clock,
    pub n: u32,
}

/// A word delivered to a unit, the clock it was sent with and its decoding.
#[derive(Clone, PartialEq, Eq)]
pub(crate) struct Word {
    pub word: U256,
    pub source: Option<package_memory::Location>,
    pub given: Clock,
    pub dec: Option<units::cluster::Decoded>,
}

#[derive(Clone, PartialEq, Eq)]
pub(crate) enum Kinded {
    Mg(Box<Cluster>),
    Vc(Box<Vector>),
    Sr(Box<Row>),
}

#[derive(Clone, PartialEq, Eq)]
pub(crate) struct Unit {
    pub key: (u32, u8, u8),
    pub name: String,
    pub port: usize,
    pub done: usize,
    pub recv: usize,
    pub acks: usize,
    /// The process whose events are this unit's completions.
    pub completer: usize,
    /// Bursts landed here: (sender process, send clock, landing clock).
    pub landings: Vec<(usize, Clock, Clock)>,
    pub words: Vec<Word>,
    pub mem: Memory,
    pub kind: Kinded,
}

/// Who sends a burst: a node or a unit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Sender {
    Node(usize),
    Unit(usize),
}

/// A CU_DATA burst's sink.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Sink {
    pub to: (u8, u8),
    pub far: Option<(u8, u8)>,
    pub mesh: u32,
    pub buf: i64,
    pub count: bool,
    pub signal: bool,
    pub ack: (u8, u8),
}

/// A shared fill's sharer group: (mesh, sharers sorted).
pub(crate) type Group = (u32, Vec<i64>);

/// A non-leading sharer's arrival: (unit, group, lead, k, own arrival,
/// own process).
#[derive(Clone, PartialEq, Eq)]
pub(crate) struct Window {
    pub unit: usize,
    pub group: Group,
    pub lead: i64,
    pub k: usize,
    pub mine: Clock,
    pub me: usize,
}

/// Per counter name, each owner process's increments over a run, by the
/// process's name; a counter no process incremented is left out.
pub type Totals = BTreeMap<String, BTreeMap<String, i64>>;

/// (`bell`, from mesh) or (`slot`, n): what a node's waits take.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(crate) enum Take {
    Bell(i64),
    Slot(i64),
}

/// A package-local conditional's outcome, stable under segment reordering.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Decision {
    pub mesh: u32,
    pub run: usize,
    pub branch: usize,
    pub visit: usize,
    pub taken: bool,
}

impl Decision {
    fn same_site(&self, other: &Self) -> bool {
        (self.mesh, self.run, self.branch, self.visit)
            == (other.mesh, other.run, other.branch, other.visit)
    }
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
struct Cursor {
    proc: usize,
    quantum: u16,
    moved: bool,
}

#[derive(Clone)]
pub(crate) struct Sim<'t> {
    pub t: &'t Target,
    pub card: card::Card,
    pub symbolic: symbolic::Symbolic,
    pub path: Vec<Decision>,
    pub choices: Vec<Decision>,
    pub procs: Vec<Proc>,
    /// Every process's name: the owner key of `Totals`, so one name each.
    proc_names: HashSet<String>,
    pub counters: Vec<Counter>,
    feedback: feedback::Feedback,
    names: IndexMap<String, usize>,
    totals: Option<Totals>,
    /// Cumulative counter bounds through each host-separated package.
    pub package_totals: Vec<Totals>,
    package: usize,
    pub findings: Vec<Finding>,
    /// Physical proof obligations do not stop architectural dispatch progress.
    /// Admission includes every obligation, including unresolved footprints.
    pub memory_findings: Vec<Finding>,
    /// Secondary initialization diagnostics, after routing/counter failures.
    pub sequence_findings: Vec<Finding>,
    pub events: u64,
    /// Full-state retained transitions; never enabled for ordinary checks.
    pub(crate) part_proof: bool,
    pub(crate) part_channel: Option<u16>,
    next_retirement: u64,
    pub(crate) stats_enabled: bool,
    next_stats: u64,
    /// What a finding reported once is keyed by.
    pub flagged: HashSet<i64>,
    pub units: Vec<Unit>,
    unit_at: HashMap<(u32, u8, u8), usize>,
    pub nodes: Vec<Node>,
    /// mesh -> node, in the order the meshes were first given a node.
    pub node_of: IndexMap<u32, usize>,
    /// unit -> the node whose package sends it words
    pub feeder: HashMap<usize, usize>,
    /// (mesh, what) -> doorbells / amount consumed
    pub taken: HashMap<(u32, Take), i64>,
    /// the keys of `taken` some wait reads
    pub waited: HashSet<(u32, Take)>,
    /// shared fills: group -> {sharer: [arrival clocks]}
    pub shares: HashMap<Group, IndexMap<i64, Vec<Clock>>>,
    /// (sequencer, pc) -> the arrival's index
    pub arrived: HashMap<(usize, usize), usize>,
    pub windows: Vec<Window>,
    /// DATA_RECEIVED counted by a node: (node, receiver, send clock, landing clock)
    pub acks: Vec<(usize, usize, Clock, Clock)>,
    /// Adds across nodes in send order: (sending mesh, sender, send clock)
    pub far_adds: Vec<(u32, usize, Clock)>,
    /// Vector core words decoded, by word (the cores' `xr` is the target's).
    pub core_words: HashMap<u64, Option<units::vector::CoreDecoded>>,
}

impl<'t> Sim<'t> {
    pub fn new(t: &'t Target, totals: Option<Totals>) -> Self {
        Sim {
            t,
            card: card::Card::default(),
            symbolic: symbolic::Symbolic::default(),
            path: Vec::new(),
            choices: Vec::new(),
            procs: Vec::new(),
            proc_names: HashSet::default(),
            counters: Vec::new(),
            feedback: feedback::Feedback::default(),
            names: IndexMap::new(),
            totals,
            package_totals: Vec::new(),
            package: 0,
            findings: Vec::new(),
            memory_findings: Vec::new(),
            sequence_findings: Vec::new(),
            events: 0,
            part_proof: false,
            part_channel: None,
            next_retirement: 1024,
            stats_enabled: std::env::var_os("KTPU_VERIFY_STATS").is_some(),
            next_stats: 1024,
            flagged: HashSet::default(),
            units: Vec::new(),
            unit_at: HashMap::default(),
            nodes: Vec::new(),
            node_of: IndexMap::new(),
            feeder: HashMap::default(),
            taken: HashMap::default(),
            waited: HashSet::default(),
            shares: HashMap::default(),
            arrived: HashMap::default(),
            windows: Vec::new(),
            acks: Vec::new(),
            far_adds: Vec::new(),
            core_words: HashMap::default(),
        }
    }

    // --------------------------------------------------------- the machine
    pub fn add_proc(&mut self, kind: Pk, name: String) -> usize {
        assert!(
            self.proc_names.insert(name.clone()),
            "two processes named {name:?}"
        );
        self.procs.push(Proc {
            kind,
            name,
            clock: Clock::new(),
            n: 0,
        });
        self.procs.len() - 1
    }

    /// The counter named `name`, made when first named.
    pub fn counter(&mut self, name: &str) -> usize {
        if let Some(&i) = self.names.get(name) {
            return i;
        }
        let totals = self.totals.as_ref().map(|t| {
            t.get(name)
                .map(|owners| owners.iter().map(|(o, &n)| (o.clone(), n)).collect())
                .unwrap_or_default()
        });
        self.counters.push(Counter::new(name.to_string(), totals));
        let i = self.counters.len() - 1;
        self.names.insert(name.to_string(), i);
        i
    }

    /// No node enters package `k` until every node ended package `k - 1`.
    /// Later packages cannot contribute to a counter wait in this package.
    pub fn enter_package(&mut self, k: usize) {
        if k <= self.package {
            return;
        }
        self.package = k;
        let Some(totals) = self.package_totals.get(k) else {
            return;
        };
        for counter in &mut self.counters {
            let owners = totals
                .get(&counter.name)
                .map(|owners| owners.iter().map(|(o, &n)| (o.clone(), n)).collect())
                .unwrap_or_default();
            counter.set_totals(owners);
        }
        self.totals = Some(totals.clone());
    }

    /// Every counter's increments by owner: a run's `totals`.
    pub fn tally(&self) -> Totals {
        self.counters
            .iter()
            .map(|c| {
                let owners: BTreeMap<String, i64> = c
                    .tally()
                    .into_iter()
                    .map(|(o, n)| (self.procs[o].name.clone(), n))
                    .collect();
                (c.name.clone(), owners)
            })
            .filter(|(_, owners)| !owners.is_empty())
            .collect()
    }

    /// Counter `c`'s `clock_to`, its owners named by their processes.
    pub fn clock_to(&self, c: usize, target: i64, caps: &[(usize, i64)]) -> Clock {
        self.counters[c].clock_to(target, caps, |o| self.procs[o].name.as_str())
    }

    /// Counters in the order first named.
    pub fn named(&self) -> impl Iterator<Item = (&String, &Counter)> {
        self.names.iter().map(|(n, &i)| (n, &self.counters[i]))
    }

    pub fn find(&mut self, kind: Kind, at: impl Into<String>, text: impl Into<String>) {
        self.findings.push(Finding::new(kind, at, text));
    }

    /// One event of process `p`, after everything `rel` saw.
    pub fn tick(&mut self, p: usize, rel: &[Clock]) -> Clock {
        let pr = &mut self.procs[p];
        for r in rel {
            pr.clock.join(r);
        }
        pr.n += 1;
        pr.clock.tick(p, pr.n);
        pr.clock.clone()
    }

    pub fn clock_of(&self, p: usize) -> Clock {
        self.procs[p].clock.clone()
    }

    /// Explore forward alternatives with each process's scheduling cursor intact.
    pub fn run(mut self, limit: u64) -> Vec<Self> {
        self.prepare_feedback();
        // Timed only with the stats on: wasm32 has no clock.
        let t0 = std::env::var_os("KTPU_VERIFY_STATS")
            .is_some()
            .then(std::time::Instant::now);
        let mut rounds = 0;
        let mut events = 0;
        let leaves = self.explore(limit, &mut rounds, &mut events);
        crate::work::RUNS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        crate::work::EVENTS.fetch_add(events, std::sync::atomic::Ordering::Relaxed);
        if let Some(t0) = t0 {
            let Some(sim) = leaves.first() else {
                return leaves;
            };
            eprintln!(
                "verify run: {} procs, {} events, {} rounds, {} counters, {:.3}s",
                sim.procs.len(),
                events,
                rounds,
                sim.counters.len(),
                t0.elapsed().as_secs_f64()
            );
        }
        leaves
    }

    fn explore(mut self, limit: u64, rounds: &mut u64, events: &mut u64) -> Vec<Self> {
        let mut pending = Vec::new();
        let mut finished = Vec::new();
        let mut joins: Vec<(Self, Cursor)> = Vec::new();
        let mut cursor = Cursor::default();
        if self.stats_enabled {
            self.retained_stats("start");
        }
        loop {
            let before = self.events;
            let outcome = self.run_rounds(limit, &mut cursor, rounds);
            *events += self.events - before;
            if self.stats_enabled {
                eprintln!(
                    "verify branches: pending={} joins={} finished={}",
                    pending.len(),
                    joins.len(),
                    finished.len()
                );
                self.retained_stats("boundary");
            }
            match outcome {
                StepOutcome::Fork {
                    node,
                    target,
                    taken,
                    fallthrough,
                } => {
                    let nd = &self.nodes[node];
                    let (run, branch) = nd.branches[&nd.at];
                    let visit = self
                        .path
                        .iter()
                        .filter(|d| (d.mesh, d.run, d.branch) == (nd.mesh, run, branch))
                        .count();
                    let decision = Decision {
                        mesh: nd.mesh,
                        run,
                        branch,
                        visit,
                        taken: false,
                    };
                    let chosen = self.choices.iter().find(|d| d.same_site(&decision));
                    if let Some(chosen) = chosen {
                        let go = chosen.taken;
                        let predicate = if go { taken } else { fallthrough };
                        if predicate.is_false() {
                            let Some((next, next_cursor)) = pending.pop() else {
                                return finished;
                            };
                            self = next;
                            cursor = next_cursor;
                        } else {
                            self.choose(node, target, decision, go, predicate);
                        }
                    } else if taken.is_false() {
                        self.choose(node, target, decision, false, fallthrough);
                    } else if fallthrough.is_false() {
                        self.choose(node, target, decision, true, taken);
                    } else {
                        for node in &mut self.nodes {
                            node.mark_joins();
                        }
                        let mut other = self.clone();
                        other.choose(node, target, decision.clone(), true, taken);
                        self.choose(node, target, decision, false, fallthrough);
                        if !pending
                            .iter()
                            .any(|(s, c): &(Self, Cursor)| *c == cursor && s.same_state(&other))
                        {
                            pending.push((other, cursor));
                        }
                    }
                }
                StepOutcome::Join => {
                    if pending.is_empty() {
                        joins.clear();
                        continue;
                    }
                    if !joins
                        .iter()
                        .any(|(s, c)| *c == cursor && s.same_state(&self))
                    {
                        joins.push((self.clone(), cursor));
                        continue;
                    }
                    let Some((next, next_cursor)) = pending.pop() else {
                        return finished;
                    };
                    self = next;
                    cursor = next_cursor;
                }
                StepOutcome::Terminal => {
                    if !finished.iter().any(|s: &Self| s.same_state(&self)) {
                        finished.push(self);
                    }
                    let Some((next, next_cursor)) = pending.pop() else {
                        return finished;
                    };
                    self = next;
                    cursor = next_cursor;
                }
                StepOutcome::Progress | StepOutcome::Blocked => unreachable!("run boundary"),
            }
        }
    }

    fn choose(
        &mut self,
        node: usize,
        target: usize,
        mut decision: Decision,
        taken: bool,
        predicate: symbolic::Predicate,
    ) {
        self.symbolic.predicate = predicate;
        decision.taken = taken;
        self.path.push(decision);
        let nd = &mut self.nodes[node];
        nd.at = if taken { target } else { nd.at + 1 };
    }

    /// Findings retain one witness path; it is not part of machine identity.
    pub fn witness(&self, findings: &mut [Finding]) {
        if self.path.is_empty() {
            return;
        }
        let path = self
            .path
            .iter()
            .map(|d| {
                format!(
                    "node {} run {} branch {} visit {} {}",
                    d.mesh,
                    d.run,
                    d.branch,
                    d.visit,
                    if d.taken { "taken" } else { "fallthrough" }
                )
            })
            .collect::<Vec<_>>()
            .join("; ");
        for finding in findings {
            finding.text = format!("{} [path: {path}]", finding.text);
        }
    }

    fn same_state(&self, other: &Self) -> bool {
        self.card == other.card
            && self.procs == other.procs
            && self.proc_names == other.proc_names
            && self.counters == other.counters
            && self.names == other.names
            && self.totals == other.totals
            && self.findings == other.findings
            && self.memory_findings == other.memory_findings
            && self.sequence_findings == other.sequence_findings
            && self.events == other.events
            && self.next_retirement == other.next_retirement
            && self.flagged == other.flagged
            && self.units == other.units
            && self.unit_at == other.unit_at
            && self.nodes == other.nodes
            && self.node_of == other.node_of
            && self.feeder == other.feeder
            && self.taken == other.taken
            && self.waited == other.waited
            && self.shares == other.shares
            && self.arrived == other.arrived
            && self.windows == other.windows
            && self.acks == other.acks
            && self.far_adds == other.far_adds
            && self.choices == other.choices
            && self.symbolic == other.symbolic
    }

    fn run_rounds(&mut self, limit: u64, cursor: &mut Cursor, rounds: &mut u64) -> StepOutcome {
        loop {
            *rounds += 1;
            while cursor.proc < self.procs.len() {
                loop {
                    if cursor.quantum == 256 {
                        cursor.quantum = 0;
                        break;
                    }
                    match self.step(cursor.proc) {
                        StepOutcome::Progress => {
                            cursor.moved = true;
                            self.events += 1;
                            if self.events > limit {
                                self.find(Kind::Model, "machine", format!("over {limit} events"));
                                return StepOutcome::Terminal;
                            }
                            cursor.quantum += 1;
                        }
                        fork @ StepOutcome::Fork { .. } => {
                            cursor.quantum += 1;
                            cursor.moved = true;
                            self.events += 1;
                            if self.events > limit {
                                self.find(Kind::Model, "machine", format!("over {limit} events"));
                                return StepOutcome::Terminal;
                            }
                            return fork;
                        }
                        StepOutcome::Join => return StepOutcome::Join,
                        StepOutcome::Blocked => {
                            cursor.quantum = 0;
                            break;
                        }
                        StepOutcome::Terminal => return StepOutcome::Terminal,
                    }
                }
                cursor.proc += 1;
            }
            cursor.proc = 0;
            if self.stats_enabled && self.events >= self.next_stats {
                self.retained_stats("sweep");
                self.next_stats = self.events.saturating_mul(2);
            }
            if self.events >= self.next_retirement {
                self.retire_memory();
                self.next_retirement = self.events + 1024;
            }
            if std::mem::take(&mut cursor.moved) {
                continue;
            }
            // Stuck: a scheduling node may send past its blocked item.
            let mut relaxed = false;
            let mut p = 0;
            while p < self.procs.len() {
                if self.relax(p) {
                    relaxed = true;
                    break;
                }
                p += 1;
            }
            if !relaxed {
                break;
            }
        }
        for p in 0..self.procs.len() {
            if !self.done(p) {
                let text = self.blocked(p);
                let name = self.procs[p].name.clone();
                self.find(Kind::Deadlock, name, text);
            }
        }
        StepOutcome::Terminal
    }

    fn step(&mut self, p: usize) -> StepOutcome {
        let progress = match self.procs[p].kind {
            Pk::Port => false,
            Pk::Node(i) => return self.node_step(i),
            Pk::MgSeq(u) => self.mg_seq_step(u),
            Pk::MgDq(u) => self.mg_dq_step(u),
            Pk::VcCu(u) => self.vc_cu_step(u),
            Pk::VcFe(u) => self.vc_fe_step(u),
            Pk::VcEng(u, k) => self.vc_eng_step(u, k),
            Pk::SrShell(u) => self.sr_step(u),
        };
        progress.into()
    }

    fn relax(&mut self, p: usize) -> bool {
        match self.procs[p].kind {
            Pk::Node(i) => self.node_relax(i),
            _ => false,
        }
    }

    pub fn done(&self, p: usize) -> bool {
        match self.procs[p].kind {
            Pk::Port => true,
            Pk::Node(i) => self.nodes[i].done(),
            Pk::MgSeq(u) => self.mg_seq_done(u),
            Pk::MgDq(u) => self.mg_dq_done(u),
            Pk::VcCu(u) => self.vc_cu_done(u),
            Pk::VcFe(u) => self.vc(u).fe.halted,
            Pk::VcEng(u, k) => {
                let e = &self.vc(u).eng[k];
                e.head >= e.items.len()
            }
            Pk::SrShell(u) => self.sr_done(u),
        }
    }

    fn blocked(&self, p: usize) -> String {
        match self.procs[p].kind {
            Pk::Port => "not at its end".into(),
            Pk::Node(i) => self.node_blocked(i),
            Pk::MgSeq(u) => self.mg_seq_blocked(u),
            Pk::MgDq(u) => self.mg_dq_blocked(u),
            Pk::VcCu(u) => {
                if self.vc(u).running {
                    "its RUN has not halted".into()
                } else {
                    "waiting for words from the node".into()
                }
            }
            Pk::VcFe(u) => format!("pc {}: {}", self.vc(u).fe.pc, self.vc(u).fe.why),
            Pk::VcEng(u, k) => {
                let v = self.vc(u);
                let e = &v.eng[k];
                format!("{} waits", v.entries[e.items[e.head]].op)
            }
            Pk::SrShell(u) => self.sr_blocked(u),
        }
    }

    // ------------------------------------------------------------ the units
    /// The unit at (mesh, x, y), made when first named; None if the machine
    /// has nothing there.
    pub fn unit(&mut self, key: (u32, u8, u8)) -> Option<usize> {
        if let Some(&u) = self.unit_at.get(&key) {
            return Some(u);
        }
        let (mesh, x, y) = key;
        let kind = self.t.mesh(mesh)?.kind_at((x, y))?;
        let ui = self.units.len();
        let name = format!("{}({}:{},{})", kind.tag(), mesh, x, y);
        let unit = match kind {
            UnitKind::Mg => self.new_cluster(ui, key, name),
            UnitKind::Vc => self.new_vector(ui, key, name),
            UnitKind::Sr => self.new_row(ui, key, name),
        };
        self.units.push(unit);
        self.unit_at.insert(key, ui);
        Some(ui)
    }

    pub fn mg(&self, u: usize) -> &Cluster {
        match &self.units[u].kind {
            Kinded::Mg(c) => c,
            _ => unreachable!("unit {u} is no cluster"),
        }
    }

    pub fn mg_mut(&mut self, u: usize) -> &mut Cluster {
        match &mut self.units[u].kind {
            Kinded::Mg(c) => c,
            _ => unreachable!("unit {u} is no cluster"),
        }
    }

    pub fn vc(&self, u: usize) -> &Vector {
        match &self.units[u].kind {
            Kinded::Vc(c) => c,
            _ => unreachable!("unit {u} is no vector core"),
        }
    }

    pub fn vc_mut(&mut self, u: usize) -> &mut Vector {
        match &mut self.units[u].kind {
            Kinded::Vc(c) => c,
            _ => unreachable!("unit {u} is no vector core"),
        }
    }

    pub fn sr(&self, u: usize) -> &Row {
        match &self.units[u].kind {
            Kinded::Sr(c) => c,
            _ => unreachable!("unit {u} is no row unit"),
        }
    }

    pub fn sr_mut(&mut self, u: usize) -> &mut Row {
        match &mut self.units[u].kind {
            Kinded::Sr(c) => c,
            _ => unreachable!("unit {u} is no row unit"),
        }
    }

    pub fn node_done_for(&self, u: usize) -> bool {
        match self.feeder.get(&u) {
            None => true,
            Some(&n) => self.nodes[n].done(),
        }
    }

    pub fn deliver(&mut self, u: usize, word: U256, given: Clock) {
        if matches!(self.units[u].kind, Kinded::Mg(_)) && !crate::mutant::on("verify.cluster/off") {
            if let Err(text) =
                crate::cluster::word(self.t, self.units[u].key, ktpu_isa::W256(word.0))
            {
                let pc = self.units[u].words.len();
                self.find(
                    Kind::Cluster,
                    self.units[u].name.clone(),
                    format!("word {pc}: {text}"),
                );
            }
        }
        let dec = match &self.units[u].kind {
            Kinded::Mg(_) => units::cluster::decode(&self.t.isa, &word),
            _ => None,
        };
        self.units[u].words.push(Word {
            word,
            given,
            dec,
            source: None,
        });
    }
}

/// `(a, b)` as Python prints a pair.
pub(crate) fn pair(a: impl std::fmt::Display, b: impl std::fmt::Display) -> String {
    format!("({a}, {b})")
}
