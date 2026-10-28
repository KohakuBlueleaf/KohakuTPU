//! A matmul cluster (`mx_cluster_cu.v`).
//!
//! Two processes: the sequencer runs the unit's words in order; the drain
//! queue sends what sweeps emitted and drains sent. Counters: entries landed
//! by COUNT bursts per L1 side (taken by `fill recv`), credits received
//! (spent by bursts).
//!
//! The write port closes a burst at `mg.wburst` sub-tiles or whenever the
//! queue runs dry, so where bursts split depends on timing; a `credit` burst
//! leaves only once the credits hold its length. A burst holding sub-tile j
//! of an item of g starts at or after the item's start and ends before
//! min(j + burst, g), so the model lets sub-tile j leave once credits reach
//! min(j + burst, g) past what the item's predecessors spent: no split asks
//! for more (verify.md lemma 5).

use std::rc::Rc;

use ktpu_isa::unit::cluster::{DFLAG_COUNT, DFLAG_SIGNAL};
use ktpu_isa::{Isa, W256};

use crate::adapter::isa::Fields;
use crate::adapter::u256::U256;
use crate::model::clock::Clock;
use crate::model::finding::Kind;
use crate::sim::memory::{Memory, Space, What};
use crate::sim::{Kinded, Pk, Sender, Sim, Sink, Unit};

/// CU_DATA buffers: L1 A, L1 B, the accumulator (a peer's add).
pub const BUF_L1A: i64 = 0;
pub const BUF_L1B: i64 = 1;
pub const BUF_PEER: i64 = 2;

/// The mutant under which the checker takes every burst a cluster gets and
/// reports no leftovers and no FILL overlap.
pub(crate) const CHECKER: &str = "isa.cluster/checker";

pub(crate) struct Cluster {
    /// Entries landed by COUNT bursts, per L1 side, and taken by fills.
    pub rx: [usize; 2],
    pub taken: [i64; 2],
    pub cr: usize,
    pub spent: i64,
    /// The array: a sweep reads L1 from its issue until the array is idle,
    /// which the sequencer learns only at a POST or a DRAIN.
    pub array: usize,
    pub seq: usize,
    pub dq: usize,
    // the sequencer
    pc: usize,
    /// Sub-tiles pushed into the drain queue so far, and through the last
    /// emitting sweep (what a fused drain waits for).
    pushed: i64,
    emitted: i64,
    /// A drain to a unit at the head: pushed, and where its last sub-tile is.
    queued: bool,
    until: i64,
    why: String,
    /// The array's clock at the last emitting sweep.
    swept: Clock,
    /// Emitting GEMM awaiting its matching fused DRAIN.
    collect: Option<Decoded>,
    /// Complete operand granules and LOAD-opened accumulator sub-tiles.
    initialized: [Vec<bool>; 3],
    // the drain queue: (sub-tiles, fields, clock pushed)
    items: Vec<(i64, Decoded, Clock)>,
    head: usize,
    into: i64,
    left: i64,
    marks: Vec<(i64, Clock)>,
    /// L1 bursts: (landing clock, send clock, what); memory FILLs' response
    /// windows: (start clock, end clock). FILL responses and CU_DATA words
    /// share one entry-assembly register, so the two must never overlap.
    pub l1_bursts: Vec<(Clock, Clock, What)>,
    pub fill_windows: Vec<(Clock, Clock)>,
}

/// A cluster word decoded: its format name and fields.
pub(crate) type Decoded = Rc<(&'static str, Fields)>;

pub(crate) fn decode(isa: &Isa, word: &U256) -> Option<Decoded> {
    isa.cluster
        .set
        .decode(W256(word.0))
        .ok()
        .map(|(n, f)| Rc::new((n, Fields::from(f))))
}

fn side(s: i64) -> &'static str {
    if s == 0 {
        "A"
    } else {
        "B"
    }
}

impl Cluster {
    fn clock_left(&self, upto: i64) -> Clock {
        if upto <= 0 || self.marks.is_empty() {
            return Clock::new();
        }
        let i = self.marks.partition_point(|m| m.0 < upto);
        self.marks[i.min(self.marks.len() - 1)].1.clone()
    }
}

impl Sim<'_> {
    pub(crate) fn new_cluster(&mut self, ui: usize, key: (u32, u8, u8), name: String) -> Unit {
        let rx = [
            self.counter(&format!("rx:{name}:A")),
            self.counter(&format!("rx:{name}:B")),
        ];
        let cr = self.counter(&format!("cr:{name}"));
        let done = self.counter(&format!("done:{name}"));
        let recv = self.counter(&format!("recv:{name}"));
        let acks = self.counter(&format!("acks:{name}"));
        let port = self.add_proc(Pk::Port, format!("{name} port"));
        let array = self.add_proc(Pk::Port, format!("{name} array port"));
        let seq = self.add_proc(Pk::MgSeq(ui), format!("{name} sequencer"));
        let dq = self.add_proc(Pk::MgDq(ui), format!("{name} drain queue"));
        Unit {
            key,
            mem: Memory::new(&name),
            name,
            port,
            done,
            recv,
            acks,
            completer: seq,
            landings: Vec::new(),
            words: Vec::new(),
            kind: Kinded::Mg(Box::new(Cluster {
                rx,
                taken: [0, 0],
                cr,
                spent: 0,
                array,
                seq,
                dq,
                pc: 0,
                pushed: 0,
                emitted: 0,
                queued: false,
                until: 0,
                why: String::new(),
                swept: Clock::new(),
                collect: None,
                initialized: [
                    vec![
                        false;
                        (self.t.facts.mg_l1_a_entries * self.t.facts.mg_entry_granules) as usize
                    ],
                    vec![
                        false;
                        (self.t.facts.mg_l1_b_entries * self.t.facts.mg_entry_granules) as usize
                    ],
                    vec![false; self.t.facts.mg_tiles as usize],
                ],
                items: Vec::new(),
                head: 0,
                into: 0,
                left: 0,
                marks: Vec::new(),
                l1_bursts: Vec::new(),
                fill_windows: Vec::new(),
            })),
        }
    }

    /// Granules each CU_DATA buffer takes.
    fn mg_limit(&self, buf: i64) -> Option<i64> {
        let f = &self.t.facts;
        match buf {
            BUF_L1A => Some(f.mg_l1_a_entries * f.mg_entry_granules),
            BUF_L1B => Some(f.mg_l1_b_entries * f.mg_entry_granules),
            BUF_PEER => Some(f.mg_tiles * f.mg_subtile_granules),
            _ => None,
        }
    }

    /// `n` granules at granule `off` of buffer `buf`, sent at `sent`; a
    /// COUNT burst counts each entry whose last word lands.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn mg_land(
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
        let (buf, off, n) = if crate::mutant::on(CHECKER) {
            (buf % 2, (off % 1024) & !1, n.min(8))
        } else {
            (buf, off, n)
        };
        let Some(limit) = self.mg_limit(buf) else {
            return Some(format!("buffer {buf} is none of the cluster's"));
        };
        let sub = self.t.facts.mg_subtile_granules;
        if off < 0 || n < 0 || off + n > limit || (buf == BUF_PEER && off % sub != 0) {
            return Some(format!("granules {off}..{} past buffer {buf}", off + n - 1));
        }
        let eg = self.t.facts.mg_entry_granules;
        if !crate::mutant::on("verify.cluster/off") {
            if buf == BUF_PEER {
                self.mg_initialized(
                    u,
                    Space::Acc,
                    off.div_euclid(sub)..(off + n + sub - 1).div_euclid(sub),
                    "ADD_PEER",
                    self.mg(u).pc,
                );
            } else {
                self.mg_mut(u).initialized[buf as usize][off as usize..(off + n) as usize]
                    .fill(true);
            }
        }
        let port = self.units[u].port;
        let unit = &mut self.units[u];
        if buf == BUF_L1A || buf == BUF_L1B {
            let space = if buf == BUF_L1A { Space::A } else { Space::B };
            let entries = off.div_euclid(eg)..(off + n - 1).div_euclid(eg) + 1;
            unit.mem
                .access(&mut self.findings, space, entries, true, clock, port, what);
            let Kinded::Mg(c) = &mut unit.kind else {
                unreachable!()
            };
            c.l1_bursts
                .push((clock.clone(), sent.clone(), what.clone()));
            if count {
                let rx = c.rx[buf as usize];
                let k = (off + n).div_euclid(eg) - off.div_euclid(eg);
                self.counters[rx].add(k, clock, port);
            }
        } else {
            // An add into the accumulator, a sub-tile `sub` granules.
            let tiles = off.div_euclid(sub)..(off + n).div_euclid(sub);
            unit.mem.access(
                &mut self.findings,
                Space::Acc,
                tiles,
                true,
                clock,
                port,
                what,
            );
        }
        None
    }

    /// L1 entries {bank, (off + i) mod bank}, i < n: an offset in the bank
    /// and a bank bit (`mx_cluster_cu.v` pl_ent).
    fn mg_entries(&self, bank: i64, off: i64, n: i64) -> Vec<i64> {
        let b = self.t.facts.mg_bank_entries;
        (0..n).map(|i| bank * b + (off + i).rem_euclid(b)).collect()
    }

    pub(crate) fn mg_rest(&self, u: usize) -> Vec<String> {
        let c = self.mg(u);
        let mut out = Vec::new();
        for s in 0..2 {
            let v = self.counters[c.rx[s]].value;
            if v != c.taken[s] {
                out.push(format!(
                    "{} entries landed on side {} and never taken",
                    v - c.taken[s],
                    side(s as i64)
                ));
            }
        }
        if c.collect.is_some() && !crate::mutant::on("verify.cluster/off") {
            out.push("an emitting GEMM has no collecting fused DRAIN".into());
        }
        let v = self.counters[c.cr].value;
        if v != c.spent {
            out.push(format!("{} credits received and never spent", v - c.spent));
        }
        out
    }

    // ------------------------------------------------------------ sequencer
    pub(crate) fn mg_seq_done(&self, u: usize) -> bool {
        self.mg(u).pc >= self.units[u].words.len() && self.node_done_for(u)
    }

    pub(crate) fn mg_seq_blocked(&self, u: usize) -> String {
        let c = self.mg(u);
        if c.pc >= self.units[u].words.len() {
            return "waiting for words from the node".into();
        }
        let why = if c.why.is_empty() { "blocked" } else { &c.why };
        format!("word {}: {}", c.pc, why)
    }

    fn mg_block(&mut self, u: usize, why: String) -> bool {
        self.mg_mut(u).why = why;
        false
    }

    pub(crate) fn mg_seq_step(&mut self, u: usize) -> bool {
        let c = self.mg(u);
        let (seq, pc, array) = (c.seq, c.pc, c.array);
        if pc >= self.units[u].words.len() {
            return false;
        }
        let given = self.units[u].words[pc].given.clone();
        let Some(dec) = self.units[u].words[pc].dec.clone() else {
            let name = self.procs[seq].name.clone();
            self.find(
                Kind::Model,
                name,
                format!("word {pc} decodes as no cluster instruction"),
            );
            let n = self.units[u].words.len();
            self.mg_mut(u).pc = n;
            return false;
        };
        let (name, f) = (dec.0, &dec.1);
        let room = self.t.facts.mg_dq_room;
        let mut rel = vec![given.clone()];
        // When a memory FILL's responses may start arriving.
        let mut start = self.clock_of(seq);
        start.join(&given);
        match name {
            "FILL" if f.g("recv") != 0 => {
                let s = f.g("sel") as usize & 1;
                let c = self.mg(u);
                let want = c.taken[s] + f.g("n");
                let rx = c.rx[s];
                if !self.counters[rx].reached(want) {
                    let why = format!(
                        "fill {} recv n={}: {} entries landed",
                        side(s as i64),
                        f.g("n"),
                        self.counters[rx].value - c.taken[s]
                    );
                    return self.mg_block(u, why);
                }
                rel.push(self.counters[rx].clock_to(want, &[]));
                self.mg_mut(u).taken[s] = want;
            }
            "FILL" if f.g("npeer") != 0 => {
                let Some((clock, lead)) = self.rendezvous(u, seq, pc, f) else {
                    return self.mg_block(
                        u,
                        "shared fill: waiting for its sharers to reach theirs".into(),
                    );
                };
                rel.push(clock);
                // The lead's read: its responses come from then on.
                start = lead;
            }
            "GEMM" if f.g("emit") != 0 => {
                // An emitting sweep starts once everything queued before has
                // left (S_GEMM waits !drain_busy), then retires on issue.
                let c = self.mg(u);
                if c.left < c.pushed {
                    let why = format!(
                        "emitting sweep: {} earlier sub-tiles still to leave",
                        c.pushed - c.left
                    );
                    return self.mg_block(u, why);
                }
                rel.push(c.clock_left(c.pushed));
                rel.push(c.swept.clone());
            }
            "DRAIN" if f.g("fuse") != 0 => {
                let c = self.mg(u);
                if c.left < c.emitted {
                    let why = format!(
                        "fused drain: {} sub-tiles still to leave",
                        c.emitted - c.left
                    );
                    return self.mg_block(u, why);
                }
                rel.push(c.clock_left(c.emitted));
            }
            "DRAIN" => {
                // Retires once the write port has sent its last sub-tile.
                if !self.mg(u).queued {
                    self.mg_push_drain(u, f, &given);
                }
                let c = self.mg(u);
                if c.left < c.until {
                    let why = format!("drain: {} sub-tiles still to leave", c.until - c.left);
                    return self.mg_block(u, why);
                }
                rel.push(c.clock_left(c.until));
                self.mg_mut(u).queued = false;
            }
            _ => {}
        }
        let plain_drain = name == "DRAIN" && f.g("fuse") == 0;
        if name == "POST" || name == "GEMM" || plain_drain {
            // Each waits for the array to be idle, its sweeps' reads done; an
            // emitting sweep holding more than the drain queue's room stays
            // busy until enough of its sub-tiles left.
            let c = self.mg(u);
            let need = c.pushed - room;
            if c.left < need {
                let why = format!(
                    "the array waits for the drain queue: {} sub-tiles",
                    need - c.left
                );
                return self.mg_block(u, why);
            }
            rel.push(self.clock_of(array));
            rel.push(c.clock_left(need));
        } else if name == "DRAIN" {
            rel.push(self.mg(u).swept.clone());
        }
        let clock = self.tick(seq, &rel);
        if name == "FILL" && f.g("recv") == 0 {
            let s = f.g("sel") & 1;
            let sp = if s == 0 { Space::A } else { Space::B };
            let ents = self.mg_entries(f.g("fbank"), f.g("eoff"), f.g("n"));
            let what = What::Word(if s == 0 { "FILL A" } else { "FILL B" }, pc as i64);
            self.mg_mark(u, sp, ents.iter().copied());
            let unit = &mut self.units[u];
            unit.mem
                .access(&mut self.findings, sp, ents, true, &clock, seq, &what);
            self.mg_mut(u).fill_windows.push((start, clock.clone()));
        } else if name == "GEMM" {
            let at = self.tick(array, std::slice::from_ref(&clock));
            let what = What::Word("GEMM", pc as i64);
            let (gm, gn, nk) = (f.g("gm"), f.g("gn"), f.g("nk"));
            let a = self.mg_entries(f.g("abank"), f.g("aoff"), gm * nk);
            let b = self.mg_entries(f.g("bbank"), f.g("boff"), gn * nk);
            if !crate::mutant::on("verify.cluster/off") {
                for (space, locs) in [(Space::A, a.clone()), (Space::B, b.clone())] {
                    self.mg_initialized(u, space, locs, "GEMM", pc);
                }
                if f.g("acc") != 0 {
                    self.mg_initialized(u, Space::Acc, 0..gm * gn, "GEMM acc", pc);
                }
            }
            self.mg_mark(u, Space::Acc, 0..gm * gn);
            let unit = &mut self.units[u];
            let out = &mut self.findings;
            unit.mem.access(out, Space::A, a, false, &at, array, &what);
            unit.mem.access(out, Space::B, b, false, &at, array, &what);
            unit.mem
                .access(out, Space::Acc, 0..gm * gn, true, &at, array, &what);
            if f.g("emit") != 0 {
                // Sub-tiles leave from the issue on; only the last implies
                // the sweep read everything.
                let g = gm * gn;
                if self.mg(u).collect.is_some() && !crate::mutant::on("verify.cluster/off") {
                    self.find(
                        Kind::Cluster,
                        self.units[u].name.clone(),
                        format!("word {pc}: emitting GEMM replaces an uncollected emission"),
                    );
                }
                let c = self.mg_mut(u);
                c.swept = at;
                c.collect = Some(dec.clone());
                c.items.push((g, dec.clone(), clock.clone()));
                c.pushed += g;
                c.emitted = c.pushed;
            }
        }
        if name == "DRAIN" && f.g("fuse") != 0 && !crate::mutant::on("verify.cluster/off") {
            let matches = self.mg(u).collect.as_ref().is_some_and(|gemm| {
                let g = &gemm.1;
                f.g("n") == g.g("gm") * g.g("gn")
                    && [
                        "addr", "addr_hi", "dnode", "dst_x", "dst_y", "dbuf", "dflags", "dack_x",
                        "dack_y", "dmesh", "dfin", "credit",
                    ]
                    .iter()
                    .all(|n| f.g(n) == g.g(n))
            });
            if !matches {
                self.find(
                    Kind::Cluster,
                    self.units[u].name.clone(),
                    format!("word {pc}: fused DRAIN has no matching emitting GEMM/count/sink"),
                );
            }
            self.mg_mut(u).collect = None;
        }
        if name == "POST" {
            self.post(u, (f.g("dst_x"), f.g("dst_y")), f.g("n"), &clock, seq);
        }
        let c = self.mg_mut(u);
        c.pc += 1;
        c.why.clear();
        let (done, recv) = (self.units[u].done, self.units[u].recv);
        self.counters[done].add(1, &clock, seq);
        self.counters[recv].add(1, &clock, seq);
        true
    }

    /// A plain DRAIN of `n` sub-tiles, once the array is idle (S_DRAIN waits
    /// !gemm_busy); into another cluster's accumulator a sub-tile is
    /// `sub` granules (`mx_cluster_node.v` OP_SEND).
    fn mg_push_drain(&mut self, u: usize, f: &Fields, given: &Clock) {
        let c = self.mg(u);
        let (seq, array, pc) = (c.seq, c.array, c.pc);
        let rel = [given.clone(), self.clock_of(array)];
        let clock = self.tick(seq, &rel);
        let n = f.g("n");
        let what = What::Word("DRAIN", pc as i64);
        // Plain drains also supply padding and credit markers; only arithmetic
        // reads require initialized accumulator contents.
        let unit = &mut self.units[u];
        unit.mem.access(
            &mut self.findings,
            Space::Acc,
            0..n,
            false,
            &clock,
            seq,
            &what,
        );
        let g = if f.g("dnode") != 0 && f.g("dbuf") == BUF_PEER {
            self.t.facts.mg_subtile_granules * n
        } else {
            n
        };
        let dec = self.units[u].words[pc]
            .dec
            .clone()
            .expect("a decoded DRAIN");
        let c = self.mg_mut(u);
        c.items.push((g, dec, clock));
        c.pushed += g;
        c.queued = true;
        c.until = c.pushed;
    }

    fn mg_initialized(
        &mut self,
        u: usize,
        space: Space,
        locs: impl IntoIterator<Item = i64>,
        op: &str,
        pc: usize,
    ) {
        let granules = if space == Space::Acc {
            1
        } else {
            self.t.facts.mg_entry_granules
        };
        let initialized = &self.mg(u).initialized[space as usize];
        if let Some(at) = locs.into_iter().find(|&at| {
            (at * granules..(at + 1) * granules).any(|g| initialized.get(g as usize) != Some(&true))
        }) {
            let finding = crate::Finding::new(
                Kind::Cluster,
                self.units[u].name.clone(),
                format!("word {pc}: {op} reads uninitialized {space} entry {at}"),
            );
            if !self.sequence_findings.contains(&finding) {
                self.sequence_findings.push(finding);
            }
        }
    }

    fn mg_mark(&mut self, u: usize, space: Space, locs: impl IntoIterator<Item = i64>) {
        if crate::mutant::on("verify.cluster/off") {
            return;
        }
        let granules = if space == Space::Acc {
            1
        } else {
            self.t.facts.mg_entry_granules
        };
        let initialized = &mut self.mg_mut(u).initialized[space as usize];
        for at in locs {
            for g in at * granules..(at + 1) * granules {
                if let Some(v) = initialized.get_mut(g as usize) {
                    *v = true;
                }
            }
        }
    }

    // ---------------------------------------------------------- drain queue
    pub(crate) fn mg_dq_done(&self, u: usize) -> bool {
        let c = self.mg(u);
        c.head >= c.items.len()
    }

    pub(crate) fn mg_dq_blocked(&self, u: usize) -> String {
        let c = self.mg(u);
        let g = c.items[c.head].0;
        let need = c.spent + (c.into + self.t.facts.mg_burst).min(g);
        format!(
            "sub-tile {} of {} waits for credits: {} of {}",
            c.into, g, self.counters[c.cr].value, need
        )
    }

    pub(crate) fn mg_dq_step(&mut self, u: usize) -> bool {
        let burst = self.t.facts.mg_burst;
        let c = self.mg(u);
        if c.head >= c.items.len() {
            return false;
        }
        let dq = c.dq;
        let (g, dec, pushed) = c.items[c.head].clone();
        let f = &dec.1;
        let credit = f.g("dnode") != 0 && f.g("credit") != 0;
        let mut rel = vec![pushed];
        let mut upto = g;
        if credit {
            let have = self.counters[c.cr].value - c.spent;
            upto = if have >= g { g } else { have - (burst - 1) };
            if upto <= c.into {
                return false;
            }
            rel.push(self.counters[c.cr].clock_to(c.spent + (upto + burst - 1).min(g), &[]));
        }
        let into = c.into;
        let clock = self.tick(dq, &rel);
        let n = upto - into;
        if f.g("dnode") != 0 {
            let sink = burst_sink(f, self.t.isa.cluster.cfg.pos_bits);
            let off = (f.g("addr") / self.t.facts.word_bytes + into) & 0xFFFF;
            self.send(Sender::Unit(u), sink, off, n, &clock, dq);
        }
        let c = self.mg_mut(u);
        c.into = upto;
        c.left += n;
        c.marks.push((c.left, clock));
        if c.into >= g {
            if credit {
                c.spent += g;
            }
            c.head += 1;
            c.into = 0;
        }
        true
    }
}

/// A drain's sink, as the card routes it.
fn burst_sink(f: &Fields, pos_bits: u32) -> Sink {
    let fin = f.g("dfin");
    Sink {
        to: (f.g("dst_x") as u8, f.g("dst_y") as u8),
        far: if fin != 0 {
            Some((
                (fin & ktpu_isa::layout::mask(pos_bits) as i64) as u8,
                (fin >> pos_bits) as u8,
            ))
        } else {
            None
        },
        mesh: f.g("dmesh") as u32,
        buf: f.g("dbuf"),
        count: f.g("dflags") & DFLAG_COUNT as i64 != 0,
        signal: f.g("dflags") & DFLAG_SIGNAL as i64 != 0,
        ack: (f.g("dack_x") as u8, f.g("dack_y") as u8),
    }
}

#[cfg(test)]
mod consistency_tests {
    use super::*;
    use crate::Target;
    use ktpu_machine::Machine;

    #[test]
    fn partial_operand_bursts_do_not_initialize_whole_entries() {
        let m = Machine::named("multimesh_v9t6").unwrap();
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let xy = t.meshes[0].mg[0];
        for complete in [false, true] {
            let mut s = Sim::new(&t, None);
            let u = s.unit((0, xy.0, xy.1)).unwrap();
            let clock = Clock::new();
            let what = What::Word("CU_DATA", 0);
            assert!(s
                .mg_land(u, BUF_L1A, 0, 1, false, &clock, &what, &clock)
                .is_none());
            if complete {
                s.mg_land(
                    u,
                    BUF_L1A,
                    1,
                    t.facts.mg_entry_granules - 1,
                    false,
                    &clock,
                    &what,
                    &clock,
                );
            }
            s.mg_initialized(u, Space::A, 0..1, "GEMM", 0);
            assert_eq!(s.sequence_findings.is_empty(), complete);
        }
    }

    #[test]
    fn peer_add_requires_a_load_opened_tile() {
        let m = Machine::named("multimesh_v9t6").unwrap();
        let t = Target::from_machine(&m, Some(1)).unwrap();
        let xy = t.meshes[0].mg[0];
        for opened in [false, true] {
            let mut s = Sim::new(&t, None);
            let u = s.unit((0, xy.0, xy.1)).unwrap();
            if opened {
                s.mg_mark(u, Space::Acc, 0..1);
            }
            let clock = Clock::new();
            s.mg_land(
                u,
                BUF_PEER,
                0,
                t.facts.mg_subtile_granules,
                false,
                &clock,
                &What::Word("CU_DATA", 0),
                &clock,
            );
            assert_eq!(s.sequence_findings.is_empty(), opened);
        }
    }
}
