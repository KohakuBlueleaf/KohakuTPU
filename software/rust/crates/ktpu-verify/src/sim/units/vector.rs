//! A V2 vector core (`vec_cu.v`, `v2_core.v`, vector-core-v2.md s2, s4).
//!
//! The CU takes IMEM and DESC words between RUNs and starts the core at a
//! RUN; the RUN completes at VHALT. The front end dispatches in program
//! order into five queues, plus U1/P1 with `vc.dualq` (FILL, DRAIN at
//! `vc.dma_queue_depth`; UNPACK, MATH, PACK at `vc.queue_depth`) and stalls
//! on a full one; each engine runs its queue in order, an entry once the
//! older register accesses it conflicts with have finished (s2), a sync
//! entry once the instructions it names have (s4), a `vwait` once its
//! counter reached its target. Counters: granules landed by COUNT bursts
//! (RX) and credits received (CR), both kept across RUNs.

use ktpu_isa::unit::vector::VecCu;
use ktpu_isa::{Isa, W256};

use crate::adapter::isa::{Fields, VOp, CR, HALT_WORD, RX};
use crate::mutant::on;

/// A core word decoded, shared by every fetch of the same word.
pub(crate) type CoreDecoded = std::rc::Rc<(VOp, Fields)>;

/// A core word decoded (`isa/vector.md`): its format and fields, the
/// register fields widened by the machine's register count.
fn core_word(isa: &Isa, word: u64) -> Option<(VOp, Fields)> {
    let (name, f) = isa.vector.decode_word_reference(word)?;
    let op = VOp::from_name(name).expect("a core format VOp names");
    let fields = if op == VOp::Xb {
        Fields::from(f).without_data(&["xk"])
    } else {
        Fields::from(f)
    };
    if op == VOp::Xb && on("verify.confinement/read-xk") {
        let _ = fields.g("xk");
    }
    Some((op, fields))
}
use crate::model::clock::Clock;
use crate::model::finding::Kind;
use crate::sim::fx::FxHashMap as HashMap;
use crate::sim::memory::{Memory, Space, What};
use crate::sim::{Kinded, Pk, Sender, Sim, Sink, Unit};

#[cfg(test)]
mod confinement_tests {
    use super::*;
    #[test]
    fn xb_amount_is_unavailable_to_hazard_checker() {
        let machine = ktpu_machine::Machine::named("multimesh_v9t6").unwrap();
        let isa = Isa::new(&machine).unwrap();
        let word = |amount| {
            isa.vector
                .core
                .set
                .by_name("XB")
                .unwrap()
                .encode(&[("xk", amount)])
                .unwrap()
                .0[0]
        };
        let a = core_word(&isa, word(0));
        for amount in 1..16 {
            assert_eq!(a, core_word(&isa, word(amount)));
        }
        #[cfg(feature = "mutants")]
        assert!(std::panic::catch_unwind(|| crate::mutant::with(
            "verify.confinement/read-xk",
            || core_word(&isa, word(7))
        ))
        .is_err());
    }
}

/// Engines, in the order the checker keeps them: FILL, DRAIN, UNPACK,
/// MATH, PACK.
pub const F: usize = 0;
pub const D: usize = 1;
pub const U: usize = 2;
pub const M: usize = 3;
pub const P: usize = 4;
const U1: usize = 5;
const P1: usize = 6;
const LETTERS: [&str; 7] = ["F", "D", "U", "M", "P", "U1", "P1"];

fn selected(queue: Option<usize>, q: i64, dualq: bool) -> Option<usize> {
    let dualq = dualq && !on("rul.003/queues-folded");
    match queue {
        Some(U) if q != 0 && dualq => Some(U1),
        Some(P) if q != 0 && dualq => Some(P1),
        other => other,
    }
}

const QUEUES: &str = "isa.vector/model_queues";
const WALK: &str = "isa.vector/model_walk";

/// Walk modes (`encode/vector.py`).
const F32: i64 = 2;
const MX7: i64 = 3;

/// VSYNC waiter -> the queue it rides (0: the front end itself).
fn waiter(w: i64) -> Option<usize> {
    if on(QUEUES) && (w == 1 || w == 3) {
        return Some(if w == 1 { P } else { U });
    }
    match w {
        1 => Some(U),
        2 => Some(M),
        3 => Some(P),
        4 => Some(F),
        6 => Some(D),
        _ => None,
    }
}

/// VSYNC kind -> the instructions it counts (DUALQ = 0: queue 1 is queue 0).
fn kind_of(on_: i64, dualq: bool) -> Option<usize> {
    let dualq = dualq && !on("rul.003/queues-folded");
    if on(QUEUES) && matches!(on_, 1 | 3 | 5) {
        return Some(if on_ == 3 { U } else { P });
    }
    match on_ {
        0 => Some(F),
        1 => Some(U),
        2 => Some(M),
        3 => Some(P),
        4 => Some(D),
        5 => Some(if dualq { U1 } else { U }),
        6 => Some(if dualq { P1 } else { P }),
        _ => None,
    }
}

fn queue_of(op: VOp) -> Option<usize> {
    if op.is_math() {
        return Some(M);
    }
    match op {
        VOp::Vunpk if on("isa.vector/model_order") => Some(F),
        VOp::Vpost | VOp::Vbar if on(QUEUES) => Some(if op == VOp::Vbar { M } else { U }),
        VOp::Vunpk => Some(U),
        VOp::Vpack => Some(P),
        VOp::Vfill => Some(F),
        VOp::Vdrain => Some(D),
        VOp::Vpost => Some(D),
        VOp::Vbar => Some(U),
        _ => None,
    }
}

/// The instruction kind an op counts as for VSYNC and marks.
fn own_kind(op: VOp) -> Option<usize> {
    if op.is_math() {
        return Some(M);
    }
    match op {
        VOp::Vunpk => Some(U),
        VOp::Vpack => Some(P),
        VOp::Vfill => Some(F),
        VOp::Vdrain => Some(D),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Acc {
    /// math read, math write, unpack write, pack read
    Mr,
    Mw,
    Uw,
    Pr,
}

/// A sync's or a wait's guard, its target fixed at dispatch.
#[derive(Clone, Copy, Debug)]
enum Guard {
    /// Satisfied at once (the `model_no_sync` mutant).
    Always,
    Count {
        ctr: usize,
        want: i64,
    },
    Finished {
        kind: usize,
        upto: i64,
    },
}

#[derive(Clone, Copy, Debug)]
enum SVal {
    Known(i64),
    Unknown,
}

pub(crate) struct Entry {
    pub op: VOp,
    f: Fields,
    clock: Clock,
    deps: Vec<usize>,
    guard: Option<Guard>,
    /// Its clock once finished.
    done: Option<Clock>,
    /// A `rel` transfer's running offset (AINC).
    roff: i64,
    /// (L1 words, writes, what) it touches.
    l1: Option<(Vec<i64>, bool, What)>,
}

#[derive(Default)]
pub(crate) struct Engine {
    pub items: Vec<usize>,
    kinds: Vec<Option<usize>>,
    pub head: usize,
}

#[derive(Default)]
pub(crate) struct Front {
    pub halted: bool,
    pub pc: i64,
    pub why: String,
    /// Per kind: instructions dispatched, and the clock of each finished.
    count: [i64; 7],
    finished: [Vec<Clock>; 7],
    s: HashMap<i64, SVal>,
    /// (first pc, end pc, trips left)
    lp: Option<(i64, i64, i64)>,
    marks: HashMap<i64, (usize, i64)>,
    /// descriptor -> running offset, from 0 each RUN
    roff: HashMap<i64, i64>,
    /// register -> [(entry, access)] not yet finished
    regs: HashMap<i64, Vec<(usize, Acc)>>,
    pending: Option<Guard>,
}

/// The walk configuration, kept across RUNs: pointers, increments,
/// (s0, s1) strides.
struct Walk {
    uptr: i64,
    uinc: i64,
    pptr: i64,
    pinc: i64,
    ustr: (i64, i64),
    pstr: (i64, i64),
}

pub(crate) struct Vector {
    pub rx: usize,
    pub cr: usize,
    /// What earlier `vwait`s on each counter asked for (kept across RUNs).
    target: [i64; 2],
    pub imem: HashMap<i64, u64>,
    pub desc: HashMap<(i64, i64), i64>,
    pub scalars: crate::vector::Scalars,
    dims: u32,
    /// descriptor -> its running offset's increment
    ainc: HashMap<i64, i64>,
    cfg: Walk,
    pub cu: usize,
    pub fe_proc: usize,
    pub eng_procs: Vec<usize>,
    /// Stamps each fill's landing, in order; its count is the fills landed.
    landing: usize,
    fills: u32,
    /// The completion queue's word counts, by slot.
    track: Vec<i64>,
    overfilled: bool,
    cu_pc: usize,
    pub running: bool,
    pub fe: Front,
    pub entries: Vec<Entry>,
    pub eng: [Engine; 7],
}

/// L1 words a walk of `chunks` chunks takes, by mode (vector-core-v2.md s6).
fn walk_len(mode: i64, chunks: i64, pack: bool) -> i64 {
    let n = if pack && mode == MX7 {
        4
    } else if mode == F32 {
        2 * chunks
    } else {
        chunks
    };
    n + i64::from(on(WALK))
}

impl Vector {
    fn walk_words(&self, ad: i64) -> i64 {
        let n: i64 = (1..=self.dims)
            .map(|fld| {
                (self.desc.get(&(ad, i64::from(fld))).copied().unwrap_or(0)
                    & ktpu_isa::layout::mask(ktpu_isa::unit::vector::DIM_BOUND_BITS) as i64)
                    .max(1)
            })
            .product();
        n + i64::from(on(WALK))
    }

    fn finished_upto(&self, kind: usize, upto: i64) -> (bool, Vec<Clock>) {
        let done = &self.fe.finished[kind];
        if upto <= 0 {
            return (true, Vec::new());
        }
        if (done.len() as i64) < upto {
            return (false, Vec::new());
        }
        (true, vec![done[upto as usize - 1].clone()])
    }

    /// Older register accesses entry `e` waits for (vector-core-v2.md s2).
    fn deps(&mut self, e: usize) {
        let Vector { entries, fe, .. } = self;
        let regs = &mut fe.regs;
        let (op, f) = (entries[e].op, &entries[e].f);
        let mut deps = Vec::new();
        if op.is_math() {
            let reads = reads(op, f);
            let vd = f.g("vd");
            for &r in &reads {
                live(
                    regs,
                    entries,
                    r,
                    |a| matches!(a, Acc::Mw | Acc::Uw),
                    &mut deps,
                );
            }
            live(
                regs,
                entries,
                vd,
                |a| matches!(a, Acc::Uw | Acc::Pr),
                &mut deps,
            );
            for &r in &reads {
                regs.entry(r).or_default().push((e, Acc::Mr));
            }
            if !op.is_cmp() {
                regs.entry(vd).or_default().push((e, Acc::Mw));
            }
        } else if op == VOp::Vunpk {
            let r = f.g("reg");
            live(regs, entries, r, |_| true, &mut deps);
            regs.entry(r).or_default().push((e, Acc::Uw));
        } else if op == VOp::Vpack {
            let r = f.g("reg");
            let want = |a| matches!(a, Acc::Mw | Acc::Uw | Acc::Pr);
            live(regs, entries, r, want, &mut deps);
            regs.entry(r).or_default().push((e, Acc::Pr));
        }
        entries[e].deps = deps;
    }
}

/// Register `r`'s accesses not yet finished (the finished dropped), in
/// order; those `want` takes appended to `deps`.
fn live(
    regs: &mut HashMap<i64, Vec<(usize, Acc)>>,
    entries: &[Entry],
    r: i64,
    want: fn(Acc) -> bool,
    deps: &mut Vec<usize>,
) {
    if let Some(v) = regs.get_mut(&r) {
        v.retain(|(x, _)| entries[*x].done.is_none());
        deps.extend(v.iter().filter(|(_, a)| want(*a)).map(|(x, _)| *x));
    }
}

fn reads(op: VOp, f: &Fields) -> Vec<i64> {
    use VOp::*;
    let names: &[(&str, &str)] = match op {
        Vmov | Vneg | Vabs | Vexp2 | Vlog2 | Vinv | Vrsqrt => &[("va", "sa")],
        Vadd | Vsub => &[("va", "sa"), ("vc", "sc")],
        Vmul | Vmax | Vmin | Vexp2d | Vcmplt | Vcmpgt | Vcmpeq => &[("va", "sa"), ("vb", "sb")],
        _ => &[("va", "sa"), ("vb", "sb"), ("vc", "sc")],
    };
    let mut out: Vec<i64> = names
        .iter()
        .filter(|(_, s)| f.g(s) == 0)
        .map(|(n, _)| f.g(n))
        .collect();
    if f.g("om") != 0 {
        // A partial write keeps the rest: a read too.
        out.push(f.g("vd"));
    }
    out
}

impl Sim<'_> {
    pub(crate) fn new_vector(&mut self, ui: usize, key: (u32, u8, u8), name: String) -> Unit {
        let rx = self.counter(&format!("rx:{name}"));
        let cr = self.counter(&format!("cr:{name}"));
        let done = self.counter(&format!("done:{name}"));
        let recv = self.counter(&format!("recv:{name}"));
        let acks = self.counter(&format!("acks:{name}"));
        let port = self.add_proc(Pk::Port, format!("{name} port"));
        let cu = self.add_proc(Pk::VcCu(ui), format!("{name} CU"));
        let fe = self.add_proc(Pk::VcFe(ui), format!("{name} front end"));
        let mut eng = vec![0; if self.t.facts.vc_dualq { 7 } else { 5 }];
        for (k, slot) in eng.iter_mut().enumerate() {
            *slot = self.add_proc(Pk::VcEng(ui, k), format!("{name} {} engine", LETTERS[k]));
        }
        let landing = self.add_proc(Pk::Port, format!("{name} fill landing"));
        Unit {
            key,
            mem: Memory::new(&name),
            name,
            port,
            done,
            recv,
            acks,
            completer: cu,
            landings: Vec::new(),
            words: Vec::new(),
            kind: Kinded::Vc(Box::new(Vector {
                rx,
                cr,
                target: [0, 0],
                imem: HashMap::default(),
                desc: HashMap::default(),
                scalars: Default::default(),
                dims: self.t.facts.vc_descriptor_dims,
                ainc: HashMap::default(),
                cfg: Walk {
                    uptr: 0,
                    uinc: 0,
                    pptr: 0,
                    pinc: 0,
                    ustr: (1, 4),
                    pstr: (1, 4),
                },
                cu,
                fe_proc: fe,
                eng_procs: eng,
                landing,
                fills: 0,
                track: vec![0; self.t.facts.vc_fill_track_depth as usize],
                overfilled: false,
                cu_pc: 0,
                running: false,
                fe: Front {
                    halted: true,
                    ..Front::default()
                },
                entries: Vec::new(),
                eng: Default::default(),
            })),
        }
    }

    /// `n` granules at word `off` of the one L1 (`vec_cu.v`: buffer 0 only,
    /// rejected past `vc.l1_words`).
    /// `vc.l1_words`.
    fn vc_l1(&self) -> i64 {
        if on(WALK) {
            256
        } else {
            self.t.facts.vc_l1_words
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn vc_land(
        &mut self,
        u: usize,
        buf: i64,
        off: i64,
        n: i64,
        count: bool,
        clock: &Clock,
        what: &What,
    ) -> Option<String> {
        let l1 = self.vc_l1();
        let (buf, off, n) = if on("isa.vector/model_peer") {
            (0, off % 256, n.min(256))
        } else {
            (buf, off, n)
        };
        if buf != 0 {
            return Some(format!("buffer {buf}: a vector core has only buffer 0"));
        }
        if off + n > l1 {
            return Some(format!(
                "words {off}..{} past the {l1}-word L1",
                off + n - 1
            ));
        }
        let port = self.units[u].port;
        let unit = &mut self.units[u];
        unit.mem.access(
            &mut self.findings,
            Space::L1,
            off..off + n,
            true,
            clock,
            port,
            what,
        );
        if count {
            let rx = self.vc(u).rx;
            self.counters[rx].add(n, clock, port);
        }
        None
    }

    pub(crate) fn vc_rest(&self, u: usize) -> Vec<String> {
        let v = self.vc(u);
        let mut out = Vec::new();
        let rx = self.counters[v.rx].value;
        if rx != v.target[RX as usize] {
            out.push(format!(
                "{} granules landed and never waited for",
                rx - v.target[RX as usize]
            ));
        }
        let cr = self.counters[v.cr].value;
        if cr != v.target[CR as usize] {
            out.push(format!(
                "{} credits received and never waited for",
                cr - v.target[CR as usize]
            ));
        }
        out
    }

    // ------------------------------------------------------------------ CU
    pub(crate) fn vc_cu_done(&self, u: usize) -> bool {
        let v = self.vc(u);
        v.cu_pc >= self.units[u].words.len() && !v.running && self.node_done_for(u)
    }

    /// A word at a time while the core is idle.
    pub(crate) fn vc_cu_step(&mut self, u: usize) -> bool {
        let (done, recv) = (self.units[u].done, self.units[u].recv);
        let v = self.vc(u);
        let cu = v.cu;
        if v.running {
            if !v.fe.halted {
                return false;
            }
            let fe = self.clock_of(v.fe_proc);
            self.vc_mut(u).running = false;
            let clock = self.tick(cu, &[fe]);
            self.counters[done].add(1, &clock, cu);
            self.counters[recv].add(1, &clock, cu);
            return true;
        }
        let pc = v.cu_pc;
        if pc >= self.units[u].words.len() {
            return false;
        }
        let word = self.units[u].words[pc].word;
        let given = self.units[u].words[pc].given.clone();
        self.vc_mut(u).cu_pc += 1;
        let clock = self.tick(cu, &[given]);
        if !on("verify.vector/off") {
            if let Err(text) = crate::vector::cu(self.t, W256(word.0)) {
                let name = self.procs[cu].name.clone();
                self.find(Kind::Vector, name, text);
                self.counters[done].add(1, &clock, cu);
                self.counters[recv].add(1, &clock, cu);
                return true;
            }
        }
        match self.t.isa.vector.decode_cu(W256(word.0)) {
            None => {
                let name = self.procs[cu].name.clone();
                self.find(
                    Kind::Model,
                    name,
                    "a word decodes as no vector CU instruction",
                );
            }
            Some(VecCu::Imem { addr, word }) => {
                self.vc_mut(u).imem.insert(addr as i64, word);
            }
            Some(VecCu::Desc { ad, fld, value }) => {
                self.vc_mut(u)
                    .desc
                    .insert((ad as i64, fld as i64), value as i64);
            }
            Some(VecCu::Run { pc }) => {
                self.vc_start(u, pc as i64, &clock);
                self.vc_mut(u).running = true;
                return true;
            }
        }
        self.counters[done].add(1, &clock, cu);
        self.counters[recv].add(1, &clock, cu);
        true
    }

    // ----------------------------------------------------------- front end
    fn vc_start(&mut self, u: usize, pc: i64, clock: &Clock) {
        let initial = self.vc(u).scalars.clone();
        let analyzed = if on("verify.vector/off") {
            Ok(initial.clone())
        } else {
            crate::vector::image(self.t, &self.vc(u).imem, pc)
                .and_then(|()| crate::vector::scalars(self.t, &self.vc(u).imem, pc, &initial))
        };
        let invalid = analyzed.as_ref().err();
        if let Some(text) = invalid {
            let name = self.procs[self.vc(u).fe_proc].name.clone();
            self.find(Kind::Vector, name, text.clone());
        }
        let failed = analyzed.is_err();
        let v = self.vc_mut(u);
        v.scalars = analyzed.unwrap_or(initial.clone());
        let fe = &mut v.fe;
        fe.halted = failed;
        fe.pc = pc;
        fe.s = initial
            .into_iter()
            .map(|(r, value)| (r, value.map_or(SVal::Unknown, SVal::Known)))
            .collect();
        fe.lp = None;
        fe.marks.clear();
        fe.roff.clear();
        fe.regs.clear();
        fe.pending = None;
        let p = v.fe_proc;
        self.tick(p, std::slice::from_ref(clock));
    }

    fn vc_fetch(&mut self, u: usize, pc: i64) -> u64 {
        if let Some(&w) = self.vc(u).imem.get(&pc) {
            return w;
        }
        let name = self.procs[self.vc(u).fe_proc].name.clone();
        self.find(Kind::Model, name, format!("pc {pc} holds no IMEM word"));
        HALT_WORD
    }

    fn vc_advance(&mut self, u: usize, n: i64) {
        let fe = &mut self.vc_mut(u).fe;
        fe.pc += n;
        if let Some((first, end, left)) = fe.lp {
            if fe.pc >= end {
                if left > 1 {
                    fe.lp = Some((first, end, left - 1));
                    fe.pc = first;
                } else {
                    fe.lp = None;
                }
            }
        }
    }

    fn vc_halt(&mut self, u: usize) -> bool {
        self.vc_mut(u).fe.halted = true;
        false
    }

    fn vc_model(&mut self, u: usize, text: String) {
        let name = self.procs[self.vc(u).fe_proc].name.clone();
        let kind = if on("verify.vector/off") {
            Kind::Model
        } else {
            Kind::Vector
        };
        self.find(kind, name, text);
    }

    pub(crate) fn vc_fe_step(&mut self, u: usize) -> bool {
        if self.vc(u).fe.halted {
            return false;
        }
        let fp = self.vc(u).fe_proc;
        let pc = self.vc(u).fe.pc;
        let word = self.vc_fetch(u, pc);
        let isa = &self.t.isa;
        let dec = self
            .core_words
            .entry(word)
            .or_insert_with(|| core_word(isa, word).map(std::rc::Rc::new))
            .clone();
        let Some(dec) = dec else {
            self.vc_model(u, format!("pc {pc}: no core instruction"));
            return self.vc_halt(u);
        };
        let (op, f) = (dec.0, &dec.1);
        if !on("verify.vector/off") {
            if matches!(op, VOp::Vfill | VOp::Vdrain) {
                if let Err(text) = crate::vector::dma(self.t, f, &self.vc(u).desc) {
                    self.vc_model(u, format!("pc {pc}: {text}"));
                    return self.vc_halt(u);
                }
            }
            if op == VOp::Vsetvl {
                let value = self.vc(u).fe.s.get(&f.g("sreg"));
                if !matches!(value, Some(SVal::Known(n)) if *n > 0 && *n <= i64::from(self.t.isa.vector.cfg.vlmax))
                {
                    self.vc_model(
                        u,
                        format!("pc {pc}: VSETVL value is unknown or faults F_VL"),
                    );
                    return self.vc_halt(u);
                }
            }
        }
        match op {
            VOp::Vhalt => {
                let v = self.vc(u);
                let busy: String = (0..v.eng_procs.len())
                    .filter(|&k| v.eng[k].head < v.eng[k].items.len())
                    .map(|k| LETTERS[k])
                    .collect();
                if !busy.is_empty() {
                    self.vc_mut(u).fe.why = format!("halt: engines {busy} still busy");
                    return false;
                }
                let rel: Vec<Clock> = v
                    .eng_procs
                    .iter()
                    .chain([&v.landing])
                    .map(|&p| self.clock_of(p))
                    .collect();
                self.tick(fp, &rel);
                self.vc_mut(u).fe.halted = true;
                return true;
            }
            VOp::Vseti => {
                let imm = (self.vc_fetch(u, pc + 1) & 0xFF_FFFF) as i64;
                if f.g("to_k") == 0 {
                    self.vc_mut(u).fe.s.insert(f.g("sreg"), SVal::Known(imm));
                }
                self.tick(fp, &[]);
                self.vc_advance(u, 2);
                return true;
            }
            VOp::Vloop => {
                let trips = self.vc(u).fe.s.get(&f.g("sreg")).copied();
                let Some(SVal::Known(trips)) = trips else {
                    self.vc_model(u, format!("pc {pc}: a loop over a count not set by VSETI"));
                    return self.vc_halt(u);
                };
                self.tick(fp, &[]);
                // A count of 0 runs the body once, as the core does (RUL.051).
                let fe = &mut self.vc_mut(u).fe;
                if trips == 0 && crate::mutant::on("rul.051/zero-skips") {
                    fe.pc = pc + 1 + f.g("body");
                    return true;
                }
                fe.lp = Some((pc + 1, pc + 1 + f.g("body"), trips.max(1)));
                fe.pc += 1;
                return true;
            }
            VOp::Vskipz => {
                let s = self
                    .vc(u)
                    .fe
                    .s
                    .get(&f.g("sreg"))
                    .copied()
                    .unwrap_or(SVal::Unknown);
                self.tick(fp, &[]);
                match s {
                    SVal::Unknown => {
                        // Data-dependent: the block runs (the constrained
                        // path, which may touch no counter: verify.md lemma 4).
                        self.vc_check_block(u, pc + 1, f.g("skip"));
                        self.vc_advance(u, 1);
                    }
                    SVal::Known(0) => self.vc_advance(u, 1 + f.g("skip")),
                    SVal::Known(_) => self.vc_advance(u, 1),
                }
                return true;
            }
            VOp::Getstk => {
                self.vc_mut(u).fe.s.insert(f.g("sreg"), SVal::Unknown);
                self.tick(fp, &[]);
                self.vc_advance(u, 1);
                return true;
            }
            VOp::Vmark => {
                let Some(kind) = kind_of(f.g("on"), self.t.facts.vc_dualq) else {
                    self.vc_model(u, format!("pc {pc}: kind {}", f.g("on")));
                    return self.vc_halt(u);
                };
                let fe = &mut self.vc_mut(u).fe;
                let upto = (fe.count[kind] - f.g("slack")).max(0);
                fe.marks.insert(f.g("mark"), (kind, upto));
                self.tick(fp, &[]);
                self.vc_advance(u, 1);
                return true;
            }
            VOp::Vsync | VOp::Vwait if f.g("waiter") == 0 => {
                if self.vc(u).fe.pending.is_none() {
                    let g = self.vc_guard(u, op, f);
                    self.vc_mut(u).fe.pending = g;
                }
                let Some(g) = self.vc(u).fe.pending else {
                    self.vc_model(u, format!("pc {pc}: kind {}", f.g("on")));
                    return self.vc_halt(u);
                };
                let (ok, rel) = self.vc_eval(u, g);
                if !ok {
                    self.vc_mut(u).fe.why =
                        format!("{} on the front end", op.name().to_lowercase());
                    return false;
                }
                self.vc_mut(u).fe.pending = None;
                self.tick(fp, &rel);
                self.vc_advance(u, 1);
                return true;
            }
            _ => {}
        }
        let mut queue = queue_of(op);
        if queue.is_none() && matches!(op, VOp::Vsync | VOp::Vwait) {
            queue = waiter(f.g("waiter"));
            if queue.is_none() {
                self.vc_model(u, format!("pc {pc}: waiter {}", f.g("waiter")));
                return self.vc_halt(u);
            }
        }
        let queue = selected(queue, f.g("q"), self.t.facts.vc_dualq);
        let Some(queue) = queue else {
            let l1 = self.vc_l1();
            let v = self.vc_mut(u);
            match op {
                VOp::Ainc => {
                    let inc = if on("isa.vector/model_ainc") {
                        0
                    } else {
                        f.g("inc")
                    };
                    v.ainc.insert(f.g("ad"), inc);
                }
                VOp::Uptr => v.cfg.uptr = f.g("ptr") & (l1 - 1),
                VOp::Uinc => v.cfg.uinc = f.g("ptr") & (l1 - 1),
                VOp::Pptr => v.cfg.pptr = f.g("ptr") & (l1 - 1),
                VOp::Pinc => v.cfg.pinc = f.g("ptr") & (l1 - 1),
                VOp::Ustr => v.cfg.ustr = (f.g("s0"), f.g("s1")),
                VOp::Pstr => v.cfg.pstr = (f.g("s0"), f.g("s1")),
                _ => {}
            }
            // front-end state: VCFG, VSETVL, AINC, ...
            self.tick(fp, &[]);
            self.vc_advance(u, 1);
            return true;
        };
        let depth = match queue {
            U if on(QUEUES) => 2,
            F if on(QUEUES) => 8,
            F | D => self.t.facts.vc_dma_queue_depth,
            _ => self.t.facts.vc_queue_depth,
        };
        {
            let e = &self.vc(u).eng[queue];
            if e.items.len() - e.head >= depth {
                self.vc_mut(u).fe.why = format!("queue {} full", LETTERS[queue]);
                return false;
            }
        }
        let clock = self.tick(fp, &[]);
        let footprint = self.vc_footprint(u, op, f, pc);
        let v = self.vc_mut(u);
        let mut entry = Entry {
            op,
            f: f.clone(),
            clock,
            deps: Vec::new(),
            guard: None,
            done: None,
            roff: 0,
            l1: None,
        };
        if matches!(op, VOp::Vfill | VOp::Vdrain) && f.g("rel") != 0 {
            let ad = f.g("ad");
            entry.roff = v.fe.roff.get(&ad).copied().unwrap_or(0);
            let inc = v.ainc.get(&ad).copied().unwrap_or(0);
            v.fe.roff.insert(ad, entry.roff + inc);
        }
        entry.l1 = footprint;
        v.entries.push(entry);
        let e = v.entries.len() - 1;
        v.deps(e);
        if matches!(op, VOp::Vsync | VOp::Vwait | VOp::Vbar) {
            let g = self.vc_guard(u, op, f);
            self.vc_mut(u).entries[e].guard = g;
        }
        let kind = selected(own_kind(op), f.g("q"), self.t.facts.vc_dualq);
        let v = self.vc_mut(u);
        if let Some(k) = kind {
            v.fe.count[k] += 1;
        }
        v.eng[queue].kinds.push(kind);
        v.eng[queue].items.push(e);
        self.vc_advance(u, 1);
        true
    }

    /// The L1 words an instruction reads or writes, fixed at dispatch: a
    /// walk's word w at base + (w mod 4)*s0 + (w div 4)*s1, the base its
    /// offset (plus the pointer, which then advances, under `rel`); a fill
    /// or drain the walk's length from its L1 offset.
    fn vc_footprint(
        &mut self,
        u: usize,
        op: VOp,
        f: &Fields,
        pc: i64,
    ) -> Option<(Vec<i64>, bool, What)> {
        let l1 = self.vc_l1();
        let what = What::AtPc(op.name(), pc);
        let v = self.vc_mut(u);
        match op {
            VOp::Vunpk | VOp::Vpack => {
                let pack = op == VOp::Vpack;
                let mut base = f.g("off");
                let (ptr, inc, (mut s0, mut s1)) = if pack {
                    (&mut v.cfg.pptr, v.cfg.pinc, v.cfg.pstr)
                } else {
                    (&mut v.cfg.uptr, v.cfg.uinc, v.cfg.ustr)
                };
                if on("isa.vector/model_strides") {
                    std::mem::swap(&mut s0, &mut s1);
                }
                if f.g("rel") != 0 {
                    base += *ptr;
                    *ptr = (*ptr + inc) & (l1 - 1);
                }
                let n = walk_len(f.g("mode"), f.g("n1") + 1, pack);
                let words = (0..n)
                    .map(|w| (base + (w % 4) * s0 + (w / 4) * s1).rem_euclid(l1))
                    .collect();
                Some((words, pack, what))
            }
            VOp::Vfill | VOp::Vdrain => {
                let n = v.walk_words(f.g("ad"));
                let words = (0..n).map(|w| (f.g("off") + w).rem_euclid(l1)).collect();
                Some((words, op == VOp::Vfill, what))
            }
            _ => None,
        }
    }

    fn vc_guard(&mut self, u: usize, op: VOp, f: &Fields) -> Option<Guard> {
        if on("isa.vector/model_no_sync") {
            return Some(Guard::Always);
        }
        let slack = if on("isa.vector/model_slack") {
            0
        } else {
            f.g("slack")
        };
        let dualq = self.t.facts.vc_dualq;
        let v = self.vc_mut(u);
        if op == VOp::Vwait {
            let c = f.g("counter");
            let i = if c == RX { 0 } else { 1 };
            v.target[i] += f.g("n");
            let ctr = if c == RX { v.rx } else { v.cr };
            return Some(Guard::Count {
                ctr,
                want: v.target[i],
            });
        }
        let (kind, upto) = if op == VOp::Vbar {
            (F, v.fe.count[F])
        } else if f.g("marked") != 0 {
            v.fe.marks.get(&f.g("mark")).copied().unwrap_or((F, 0))
        } else {
            let kind = kind_of(f.g("on"), dualq)?;
            (kind, (v.fe.count[kind] - slack).max(0))
        };
        Some(Guard::Finished { kind, upto })
    }

    fn vc_eval(&self, u: usize, g: Guard) -> (bool, Vec<Clock>) {
        match g {
            Guard::Always => (true, Vec::new()),
            Guard::Count { ctr, want } => (
                self.counters[ctr].reached(want),
                vec![self.clock_to(ctr, want, &[])],
            ),
            Guard::Finished { kind, upto } => self.vc(u).finished_upto(kind, upto),
        }
    }

    /// Lemma 4: no VWAIT, VPOST or peer VDRAIN under a data-dependent skip.
    fn vc_check_block(&mut self, u: usize, pc: i64, n: i64) {
        let mut k = 0;
        while k < n {
            let at = pc + k;
            let w = self.vc_fetch(u, at);
            k += 1;
            let Some((op, f)) = core_word(&self.t.isa, w) else {
                continue;
            };
            if op == VOp::Vseti {
                k += 1;
            }
            if !matches!(op, VOp::Vwait | VOp::Vpost | VOp::Vdrain) {
                continue;
            }
            if op == VOp::Vdrain && f.g("node") == 0 {
                continue;
            }
            if self.flagged.insert(at) {
                self.vc_model(
                    u,
                    format!("pc {}: {} under a data-dependent skip", at, op.name()),
                );
            }
        }
    }

    // -------------------------------------------------------------- engines
    pub(crate) fn vc_eng_step(&mut self, u: usize, k: usize) -> bool {
        let v = self.vc(u);
        let eng = &v.eng[k];
        if eng.head >= eng.items.len() {
            return false;
        }
        let e = eng.items[eng.head];
        let entry = &v.entries[e];
        if entry.deps.iter().any(|&x| v.entries[x].done.is_none()) {
            return false;
        }
        let mut rel = vec![entry.clock.clone()];
        rel.extend(
            entry
                .deps
                .iter()
                .map(|&x| v.entries[x].done.clone().expect("a finished dep")),
        );
        if let Some(g) = entry.guard {
            let (ok, more) = self.vc_eval(u, g);
            if !ok {
                return false;
            }
            rel.extend(more);
        }
        let p = v.eng_procs[k];
        let clock = self.tick(p, &rel);
        let entry = &mut self.vc_mut(u).entries[e];
        let (op, f, roff) = (entry.op, entry.f.clone(), entry.roff);
        if let Some((words, write, what)) = entry.l1.take() {
            let unit = &mut self.units[u];
            unit.mem.access(
                &mut self.findings,
                Space::L1,
                words,
                write,
                &clock,
                p,
                &what,
            );
        }
        if op == VOp::Vpost {
            self.post(u, (f.g("dst_x"), f.g("dst_y")), f.g("n"), &clock, p);
        } else if op == VOp::Vdrain
            && (f.g("node") != 0
                || (crate::mutant::on("rul.052/dst-is-peer")
                    && (f.g("dst_x") != 0 || f.g("dst_y") != 0)))
        {
            // A peer drain by the node bit alone, as the core decides (RUL.052).
            self.vc_peer(u, k, &f, roff, &clock);
        }
        let clock = if op == VOp::Vfill {
            let words = self.vc(u).walk_words(f.g("ad"));
            self.vc_fill_landed(u, words, &clock)
        } else {
            clock
        };
        let v = self.vc_mut(u);
        v.entries[e].done = Some(clock.clone());
        let head = v.eng[k].head;
        if let Some(kind) = v.eng[k].kinds[head] {
            v.fe.finished[kind].push(clock);
        }
        v.eng[k].head += 1;
        true
    }

    /// A fill of `words` taken at `taken` and its landing, a later event in
    /// landing order: fills whose landing the take has not seen hold
    /// completion entries. Past `vc.fill_track_depth` the push overwrites the
    /// word count of the fill that depth back, and at twice the depth the
    /// count of entries wraps to empty (ISA.VC.193).
    fn vc_fill_landed(&mut self, u: usize, words: i64, taken: &Clock) -> Clock {
        let depth = self.t.facts.vc_fill_track_depth;
        let v = self.vc_mut(u);
        let landing = v.landing;
        let held = v.fills + 1 - taken.at(landing);
        let slot = (v.fills % depth) as usize;
        let shadowed = std::mem::replace(&mut v.track[slot], words);
        v.fills += 1;
        let lost = held > depth && (shadowed != words || held >= 2 * depth);
        if lost && !on("verify.vector/no-fill-track") && !self.vc(u).overfilled {
            self.vc_mut(u).overfilled = true;
            self.vc_model(
                u,
                format!(
                    "{held} fills taken with no sync on their landing; \
                     the completion queue holds {depth}"
                ),
            );
        }
        self.tick(landing, std::slice::from_ref(taken))
    }

    /// A peer drain: one burst of the walk's words; the descriptor's base
    /// (plus a `rel` drain's running offset) is {COUNT [34], fin [33:26],
    /// mesh [25:24], ack [23:16], offset [15:0]}.
    fn vc_peer(&mut self, u: usize, k: usize, f: &Fields, roff: i64, clock: &Clock) {
        let v = self.vc(u);
        let p = v.eng_procs[k];
        let base = v.desc.get(&(f.g("ad"), 0)).copied().unwrap_or(0) + roff;
        let n = v.walk_words(f.g("ad"));
        if n > self.t.facts.vc_walk_max {
            let name = self.procs[p].name.clone();
            self.find(
                Kind::Bounds,
                name,
                format!("a peer drain of {n} words faults (F_LEN)"),
            );
            return;
        }
        let fin = (base >> 26) & 0xFF;
        let ack = (base >> 16) & 0xFF;
        let sink = Sink {
            to: (f.g("dst_x") as u8, f.g("dst_y") as u8),
            far: if fin != 0 {
                Some(((fin & 0xF) as u8, (fin >> 4) as u8))
            } else {
                None
            },
            mesh: ((base >> 24) & 3) as u32,
            buf: f.g("buf_id"),
            count: (base >> 34) & 1 != 0,
            signal: f.g("signal") != 0,
            ack: ((ack & 0xF) as u8, (ack >> 4) as u8),
        };
        self.send(Sender::Unit(u), sink, base & 0xFFFF, n, clock, p);
    }
}
