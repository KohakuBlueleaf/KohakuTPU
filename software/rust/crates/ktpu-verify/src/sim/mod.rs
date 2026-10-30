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
pub(crate) mod fx;
pub(crate) mod memory;
pub(crate) mod node;
pub(crate) mod report;
pub(crate) mod units;

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

pub(crate) struct Proc {
    pub kind: Pk,
    pub name: String,
    pub clock: Clock,
    pub n: u32,
}

/// A word delivered to a unit, the clock it was sent with and its decoding.
pub(crate) struct Word {
    pub word: U256,
    pub given: Clock,
    pub dec: Option<units::cluster::Decoded>,
}

pub(crate) enum Kinded {
    Mg(Box<Cluster>),
    Vc(Box<Vector>),
    Sr(Box<Row>),
}

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

pub(crate) struct Sim<'t> {
    pub t: &'t Target,
    pub procs: Vec<Proc>,
    /// Every process's name: the owner key of `Totals`, so one name each.
    proc_names: HashSet<String>,
    pub counters: Vec<Counter>,
    names: IndexMap<String, usize>,
    totals: Option<Totals>,
    pub findings: Vec<Finding>,
    /// Secondary initialization diagnostics, after routing/counter failures.
    pub sequence_findings: Vec<Finding>,
    pub events: u64,
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
    /// Vector core words decoded, by word (the cores' `xr` is the target's).
    pub core_words: HashMap<u64, Option<units::vector::CoreDecoded>>,
}

impl<'t> Sim<'t> {
    pub fn new(t: &'t Target, totals: Option<Totals>) -> Self {
        Sim {
            t,
            procs: Vec::new(),
            proc_names: HashSet::default(),
            counters: Vec::new(),
            names: IndexMap::new(),
            totals,
            findings: Vec::new(),
            sequence_findings: Vec::new(),
            events: 0,
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

    /// Every process as far as it goes, round after round, until none can
    /// take a step; then every process not at its end is in a deadlock (the
    /// same one under every schedule).
    pub fn run(&mut self, limit: u64) {
        // Timed only with the stats on: wasm32 has no clock.
        let t0 = std::env::var_os("KTPU_VERIFY_STATS")
            .is_some()
            .then(std::time::Instant::now);
        let mut rounds = 0u64;
        self.run_rounds(limit, &mut rounds);
        if let Some(t0) = t0 {
            eprintln!(
                "verify run: {} procs, {} events, {} rounds, {} counters, {:.3}s",
                self.procs.len(),
                self.events,
                rounds,
                self.counters.len(),
                t0.elapsed().as_secs_f64()
            );
        }
    }

    fn run_rounds(&mut self, limit: u64, rounds: &mut u64) {
        loop {
            *rounds += 1;
            let mut moved = false;
            let mut p = 0;
            while p < self.procs.len() {
                while self.step(p) {
                    moved = true;
                    self.events += 1;
                    if self.events > limit {
                        self.find(Kind::Model, "machine", format!("over {limit} events"));
                        return;
                    }
                }
                p += 1;
            }
            if moved {
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
    }

    fn step(&mut self, p: usize) -> bool {
        match self.procs[p].kind {
            Pk::Port => false,
            Pk::Node(i) => self.node_step(i),
            Pk::MgSeq(u) => self.mg_seq_step(u),
            Pk::MgDq(u) => self.mg_dq_step(u),
            Pk::VcCu(u) => self.vc_cu_step(u),
            Pk::VcFe(u) => self.vc_fe_step(u),
            Pk::VcEng(u, k) => self.vc_eng_step(u, k),
            Pk::SrShell(u) => self.sr_step(u),
        }
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
        self.units[u].words.push(Word { word, given, dec });
    }
}

/// `(a, b)` as Python prints a pair.
pub(crate) fn pair(a: impl std::fmt::Display, b: impl std::fmt::Display) -> String {
    format!("({a}, {b})")
}

#[cfg(test)]
mod tests {
    use super::*;
    use ktpu_machine::Machine;

    /// Every unit of a multi-mesh machine made at once: no two processes
    /// share a name.
    #[test]
    fn every_unit_names_its_processes_apart() {
        let m = Machine::named("multimesh_v9t6").unwrap();
        let t = Target::from_machine(&m, None).unwrap();
        assert!(t.meshes.len() > 1);
        let mut s = Sim::new(&t, None);
        for mesh in &t.meshes {
            for &(x, y) in mesh.mg.iter().chain(&mesh.vc).chain(&mesh.sr) {
                s.unit((mesh.index, x, y)).unwrap();
            }
        }
        assert!(s.procs.len() > 2 * t.meshes.len());
    }

    #[test]
    #[should_panic(expected = "two processes named")]
    fn a_second_process_of_one_name_is_refused() {
        let m = Machine::named("multimesh_v9t6").unwrap();
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let mut s = Sim::new(&t, None);
        s.add_proc(Pk::Port, "port".into());
        s.add_proc(Pk::Port, "port".into());
    }
}
