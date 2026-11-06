//! A V2 vector core (`isa/vector.md` s10): five queues, each engine in order;
//! only the program's own syncs order L1 accesses (ISA.VC.142).

pub mod decode;
pub mod inst;

use std::collections::VecDeque;
use std::rc::Rc;

use inst::*;

use crate::mem::{Dram, Word, WORD};
use crate::num;
use crate::num::mx7;
use crate::rng::Rng;
use crate::units::link::{Data, Landing, Target};
use crate::R;

pub const LANES: usize = 16;
pub const CHUNKS: usize = 8;
pub const VLMAX: usize = LANES * CHUNKS;
const L1_WORDS: usize = 512;
const QUEUE_DEPTH: usize = 8;
const DMA_QUEUE_DEPTH: usize = 4;
/// `vc.tau`: an EXP2D lane at or past 2^tau sets its sticky bit.
pub const TAU: u32 = 8;
/// A peer descriptor base's COUNT (L1.VC.005).
pub const PEER_COUNT_BIT: u32 = 34;

/// Queues, in the order `Core::step` scans them.
const Q_FILL: usize = 0;
const Q_DRAIN: usize = 1;
const Q_UNPACK: usize = 2;
const Q_MATH: usize = 3;
const Q_PACK: usize = 4;

/// A descriptor: its base and (stride, bound) dimensions, innermost first.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Desc {
    pub base: i64,
    pub dims: [(i64, u32); 4],
}

impl Desc {
    fn walk(&self, from: i64) -> Vec<i64> {
        let n: [i64; 4] = self.dims.map(|(_, b)| i64::from(b.max(1)));
        let mut out = Vec::with_capacity(n.iter().product::<i64>() as usize);
        for i3 in 0..n[3] {
            for i2 in 0..n[2] {
                for i1 in 0..n[1] {
                    for i0 in 0..n[0] {
                        let idx = [i0, i1, i2, i3];
                        let off: i64 = (0..4).map(|k| idx[k] * self.dims[k].0).sum();
                        out.push(from + off);
                    }
                }
            }
        }
        out
    }
}

/// What an entry does, its operands fixed at dispatch (ISA.VC.052, .061).
enum Work {
    Math {
        op: MathOp,
        vd: u32,
        a: Val,
        b: Val,
        c: Val,
        cfg: Cfg,
    },
    Unpack {
        reg: u32,
        words: Vec<usize>,
        mode: Mode,
        cb: u32,
        n: u32,
    },
    Pack {
        reg: u32,
        words: Vec<usize>,
        mode: Mode,
        cb: u32,
        n: u32,
    },
    Fill {
        addrs: Vec<i64>,
        off: usize,
        inner: usize,
    },
    Drain {
        addrs: Vec<i64>,
        off: usize,
        peer: Option<Peer>,
    },
    Post {
        to: Target,
        n: usize,
    },
    /// A VWAIT: until `upto` granules have landed.
    Landed {
        upto: u64,
    },
    /// A sync or a VBAR: its guard is the whole of it.
    Guard,
}

#[derive(Clone, Copy)]
enum Val {
    Reg(u32),
    Imm(u32),
}

#[derive(Clone, Copy)]
struct Guard {
    kind: usize,
    upto: i64,
}

struct Entry {
    work: Work,
    guard: Option<Guard>,
    deps: Vec<usize>,
    kind: Option<usize>,
    done: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Acc {
    MathRead,
    MathWrite,
    UnpackWrite,
    PackRead,
}

/// The state a RUN clears (ISA.VC.028).
#[derive(Default)]
struct Run {
    prog: Option<Rc<Vec<Inst>>>,
    pc: usize,
    lp: Option<(usize, usize, u32)>,
    entries: Vec<Entry>,
    queues: [VecDeque<usize>; 5],
    dispatched: [i64; 5],
    finished: [i64; 5],
    marks: [Option<Guard>; 8],
    roff: [i64; 8],
    regs: Vec<Vec<(usize, Acc)>>,
    pending: Option<Guard>,
}

/// One unit's step.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    Moved,
    /// Nothing can move: it waits on what no other unit gives a core.
    Stuck,
    Done,
}

pub struct Core {
    regs: Vec<[u32; VLMAX]>,
    pub s: [u32; 16],
    k: [u32; 4],
    p: [[bool; VLMAX]; 4],
    sticky: [bool; VLMAX],
    cfg: Cfg,
    ustr: (i64, i64),
    pstr: (i64, i64),
    uptr: i64,
    uinc: i64,
    pptr: i64,
    pinc: i64,
    pub desc: [Desc; 8],
    ainc: [i64; 8],
    l1: Vec<Word>,
    /// Granules COUNT bursts landed, and those a VWAIT has asked for.
    landed: u64,
    asked: u64,
    /// Bursts a peer drain sent, for the machine to carry.
    pub outbox: Vec<Landing>,
    run: Run,
}

impl Core {
    pub fn new(regs: usize) -> Core {
        Core {
            regs: vec![[0; VLMAX]; regs],
            s: [0; 16],
            k: [0, num::ONE, 0xBF8000, 0],
            p: [[false; VLMAX]; 4],
            sticky: [false; VLMAX],
            cfg: Cfg::RESET,
            ustr: (1, 4),
            pstr: (1, 4),
            uptr: 0,
            uinc: 0,
            pptr: 0,
            pinc: 0,
            desc: [Desc::default(); 8],
            ainc: [0; 8],
            l1: vec![[0; WORD]; L1_WORDS],
            landed: 0,
            asked: 0,
            outbox: Vec::new(),
            run: Run::default(),
        }
    }

    pub fn idle(&self) -> bool {
        self.run.prog.is_none()
    }

    /// A burst into L1 (ISA.VC.180).
    pub fn land(&mut self, at: usize, words: &[Word], count: bool) {
        for (i, w) in words.iter().enumerate() {
            self.l1[(at + i) % L1_WORDS] = *w;
        }
        if count {
            self.landed += words.len() as u64;
        }
    }

    /// Whether any engine or the front end can move.
    pub fn runnable(&self) -> bool {
        self.idle() || (0..5).any(|q| self.head_ready(q)) || self.front_ready()
    }

    pub fn state(&self) -> String {
        format!("a core at pc {}: {}", self.run.pc, self.blocked())
    }

    /// A RUN of `prog` (ISA.VC.028: queues, counts, marks and offsets clear).
    pub fn start(&mut self, prog: Rc<Vec<Inst>>) {
        self.run = Run {
            prog: Some(prog),
            regs: vec![Vec::new(); self.regs.len()],
            ..Run::default()
        };
    }

    /// One move of the front end or of an engine ready to go.
    pub fn step(&mut self, mem: &mut Dram, rng: &mut Rng) -> R<Step> {
        if self.idle() {
            return Ok(Step::Done);
        }
        let mut ready = [0usize; 6];
        let mut n = 0;
        for q in 0..5 {
            if self.head_ready(q) {
                ready[n] = q;
                n += 1;
            }
        }
        if self.front_ready() {
            ready[n] = 5;
            n += 1;
        }
        if n == 0 {
            return Err(format!(
                "a core stuck at pc {}: {}",
                self.run.pc,
                self.blocked()
            ));
        }
        match ready[rng.pick(n)] {
            5 => self.dispatch(),
            q => self.engine(q, mem),
        }
    }

    fn blocked(&self) -> String {
        let depths: Vec<usize> = self.run.queues.iter().map(VecDeque::len).collect();
        format!(
            "queues {depths:?}, finished {:?} of {:?}",
            self.run.finished, self.run.dispatched
        )
    }

    fn guard_ok(&self, g: Guard) -> bool {
        self.run.finished[g.kind] >= g.upto
    }

    fn head_ready(&self, q: usize) -> bool {
        let Some(&e) = self.run.queues[q].front() else {
            return false;
        };
        let entry = &self.run.entries[e];
        let landed = match entry.work {
            Work::Landed { upto } => self.landed >= upto,
            _ => true,
        };
        landed
            && entry.deps.iter().all(|d| self.run.entries[*d].done)
            && entry.guard.is_none_or(|g| self.guard_ok(g))
    }

    fn inst(&self) -> &Inst {
        let prog = self.run.prog.as_ref().expect("a program");
        &prog[self.run.pc]
    }

    fn queue_room(&self, q: usize) -> bool {
        let depth = if q == Q_FILL || q == Q_DRAIN {
            DMA_QUEUE_DEPTH
        } else {
            QUEUE_DEPTH
        };
        self.run.queues[q].len() < depth
    }

    fn front_ready(&self) -> bool {
        if let Some(g) = self.run.pending {
            return self.guard_ok(g);
        }
        match self.inst() {
            Inst::Math { .. } => self.queue_room(Q_MATH),
            Inst::Walk { pack: false, .. } | Inst::Bar => self.queue_room(Q_UNPACK),
            Inst::Walk { pack: true, .. } => self.queue_room(Q_PACK),
            Inst::Fill { .. } => self.queue_room(Q_FILL),
            Inst::Drain { .. } | Inst::Post { .. } => self.queue_room(Q_DRAIN),
            Inst::Landed { waiter, .. } | Inst::Sync { waiter, .. } => match waiter {
                Waiter::Fe => true,
                Waiter::Unpack => self.queue_room(Q_UNPACK),
                Waiter::Math => self.queue_room(Q_MATH),
                Waiter::Pack => self.queue_room(Q_PACK),
                Waiter::Fill => self.queue_room(Q_FILL),
                Waiter::Drain => self.queue_room(Q_DRAIN),
            },
            Inst::Halt => self.run.queues.iter().all(VecDeque::is_empty),
            _ => true,
        }
    }

    fn advance(&mut self) {
        self.run.pc += 1;
        if let Some((first, end, left)) = self.run.lp {
            if self.run.pc >= end {
                if left > 1 {
                    self.run.lp = Some((first, end, left - 1));
                    self.run.pc = first;
                } else {
                    self.run.lp = None;
                }
            }
        }
    }

    /// The guard of a sync (fixed at dispatch, ISA.VC.144, .146).
    fn guard(&self, on: On) -> R<Guard> {
        match on {
            On::Kind { kind, slack } => Ok(Guard {
                kind,
                upto: (self.run.dispatched[kind] - slack).max(0),
            }),
            On::Mark(m) => self.run.marks[m as usize].ok_or(format!("m{m} never marked")),
        }
    }

    /// `reg`'s older accesses `want` takes, the finished dropped.
    fn older(&mut self, reg: u32, want: fn(Acc) -> bool, deps: &mut Vec<usize>) {
        let run = &mut self.run;
        let list = &mut run.regs[reg as usize];
        let entries = &run.entries;
        list.retain(|(e, _)| !entries[*e].done);
        deps.extend(list.iter().filter(|(_, a)| want(*a)).map(|(e, _)| *e));
    }

    fn push(
        &mut self,
        q: usize,
        work: Work,
        guard: Option<Guard>,
        deps: Vec<usize>,
        kind: Option<usize>,
    ) -> usize {
        let e = self.run.entries.len();
        self.run.entries.push(Entry {
            work,
            guard,
            deps,
            kind,
            done: false,
        });
        self.run.queues[q].push_back(e);
        if let Some(k) = kind {
            self.run.dispatched[k] += 1;
        }
        e
    }

    fn scalar(&self, s: Src) -> Val {
        match s {
            Src::V(r) => Val::Reg(r),
            Src::S(r) => Val::Imm(self.s[r as usize & 15]),
            Src::K(r) => Val::Imm(self.k[r as usize & 3]),
        }
    }

    /// The word walk `w` of an unpack or pack lands on (ISA.VC.102).
    fn walk_words(base: i64, stride: (i64, i64), n: u32, mode: Mode) -> Vec<usize> {
        let count = match mode {
            Mode::F32 => 2 * n as i64,
            Mode::Mx7(_) => 4,
            Mode::F16 | Mode::Gt4 => n as i64,
        };
        (0..count)
            .map(|w| {
                (base + (w % 4) * stride.0 + (w / 4) * stride.1).rem_euclid(L1_WORDS as i64)
                    as usize
            })
            .collect()
    }

    fn dispatch(&mut self) -> R<Step> {
        if let Some(_g) = self.run.pending.take() {
            self.advance();
            return Ok(Step::Moved);
        }
        let inst = self.inst().clone();
        match inst {
            Inst::Math {
                op,
                vd,
                a,
                b,
                c,
                om,
                cfg,
            } => self.dispatch_math(op, vd, a, b, c, om, cfg),
            Inst::Walk {
                pack,
                reg,
                off,
                n,
                mode,
                cb,
                rel,
                stride,
            } => {
                let walk = stride.unwrap_or(if pack { self.pstr } else { self.ustr });
                let mut base = i64::from(off);
                if rel {
                    let (ptr, inc) = if pack {
                        (&mut self.pptr, self.pinc)
                    } else {
                        (&mut self.uptr, self.uinc)
                    };
                    base += *ptr;
                    *ptr = (*ptr + inc).rem_euclid(L1_WORDS as i64);
                }
                let words = Self::walk_words(base, walk, n, mode);
                let mut deps = Vec::new();
                if pack {
                    self.older(reg, |a| a != Acc::MathRead, &mut deps);
                    let work = Work::Pack {
                        reg,
                        words,
                        mode,
                        cb,
                        n,
                    };
                    let e = self.push(Q_PACK, work, None, deps, Some(PACK));
                    self.run.regs[reg as usize].push((e, Acc::PackRead));
                } else {
                    self.older(reg, |_| true, &mut deps);
                    let work = Work::Unpack {
                        reg,
                        words,
                        mode,
                        cb,
                        n,
                    };
                    let e = self.push(Q_UNPACK, work, None, deps, Some(UNPACK));
                    self.run.regs[reg as usize].push((e, Acc::UnpackWrite));
                }
            }
            Inst::Fill { ad, off, rel } => {
                let addrs = self.dma_addrs(ad, rel)?;
                let (stride, bound) = self.desc[ad as usize].dims[0];
                let work = Work::Fill {
                    inner: if stride == 32 {
                        bound.max(1) as usize
                    } else {
                        1
                    },
                    addrs,
                    off: off as usize,
                };
                self.push(Q_FILL, work, None, Vec::new(), Some(FILL));
            }
            Inst::Drain { ad, off, rel, peer } => {
                let addrs = self.dma_addrs(ad, rel)?;
                let work = Work::Drain {
                    addrs,
                    off: off as usize,
                    peer,
                };
                self.push(Q_DRAIN, work, None, Vec::new(), Some(DRAIN));
            }
            Inst::Post { to, n } => {
                // ISA.VC.144, .153: ordered with drains, not a drain counter tick.
                self.push(Q_DRAIN, Work::Post { to, n }, None, Vec::new(), None);
            }
            Inst::Landed { waiter, n } => {
                self.asked += u64::from(n);
                let q = match waiter {
                    Waiter::Fe => Q_UNPACK,
                    Waiter::Unpack => Q_UNPACK,
                    Waiter::Math => Q_MATH,
                    Waiter::Pack => Q_PACK,
                    Waiter::Fill => Q_FILL,
                    Waiter::Drain => Q_DRAIN,
                };
                self.push(q, Work::Landed { upto: self.asked }, None, Vec::new(), None);
            }
            Inst::Sync { waiter, on } => {
                let g = self.guard(on)?;
                let q = match waiter {
                    Waiter::Fe => {
                        self.run.pending = Some(g);
                        return Ok(Step::Moved);
                    }
                    Waiter::Unpack => Q_UNPACK,
                    Waiter::Math => Q_MATH,
                    Waiter::Pack => Q_PACK,
                    Waiter::Fill => Q_FILL,
                    Waiter::Drain => Q_DRAIN,
                };
                self.push(q, Work::Guard, Some(g), Vec::new(), None);
            }
            Inst::Bar => {
                let g = Guard {
                    kind: FILL,
                    upto: self.run.dispatched[FILL],
                };
                self.push(Q_UNPACK, Work::Guard, Some(g), Vec::new(), None);
            }
            Inst::Mark { mark, kind, slack } => {
                let upto = (self.run.dispatched[kind] - slack).max(0);
                self.run.marks[mark as usize] = Some(Guard { kind, upto });
            }
            Inst::Ainc { ad, inc } => self.ainc[ad as usize] = inc,
            Inst::Ptr { reg, value } => {
                let v = i64::from(value) & (L1_WORDS as i64 - 1);
                match reg {
                    Ptr::Uptr => self.uptr = v,
                    Ptr::Uinc => self.uinc = v,
                    Ptr::Pptr => self.pptr = v,
                    Ptr::Pinc => self.pinc = v,
                }
            }
            Inst::Seti { sreg, to_k, value } => {
                if to_k {
                    self.k[3] = value;
                } else {
                    self.s[sreg as usize] = value;
                }
            }
            Inst::Setvl { sreg } => {
                let vl = self.s[sreg as usize];
                if vl == 0 || vl as usize > VLMAX {
                    return Err(format!("VSETVL {vl} faults F_VL"));
                }
                self.cfg.vl = vl;
            }
            Inst::Getstk { sreg } => {
                let any = self.sticky.iter().any(|b| *b);
                self.s[sreg as usize] = if any { num::ONE } else { 0 };
                self.sticky = [false; VLMAX];
            }
            Inst::Loop { sreg, body } => {
                if self.run.lp.is_some() {
                    return Err("a VLOOP inside a loop faults F_LOOP".into());
                }
                let first = self.run.pc + 1;
                let trips = self.s[sreg as usize].max(1);
                self.run.lp = Some((first, first + body as usize, trips));
                self.run.pc = first;
                return Ok(Step::Moved);
            }
            Inst::Skipz { sreg, n } => {
                if self.s[sreg as usize] == 0 {
                    self.run.pc += n as usize;
                }
            }
            Inst::Config(c) => self.config(c),
            Inst::Halt => {
                self.run.prog = None;
                return Ok(Step::Done);
            }
        }
        self.advance();
        Ok(Step::Moved)
    }

    fn config(&mut self, c: Config) {
        match c {
            Config::Chk(chk) => self.cfg.chk = chk,
            Config::Chkvl(vl, chk) => {
                self.cfg.vl = vl;
                self.cfg.chk = chk;
            }
            Config::Xb {
                xm,
                xk,
                sh,
                xc,
                pm,
                pr,
            } => {
                self.cfg.xm = xm;
                self.cfg.xk = xk;
                self.cfg.sh = sh;
                self.cfg.xc = xc;
                self.cfg.pm = pm;
                self.cfg.pr = pr;
            }
            Config::Ustr(s0, s1) => self.ustr = (s0, s1),
            Config::Pstr(s0, s1) => self.pstr = (s0, s1),
        }
    }

    fn dma_addrs(&mut self, ad: u32, rel: bool) -> R<Vec<i64>> {
        let ad = ad as usize;
        let mut base = self.desc[ad].base;
        if rel {
            base += self.run.roff[ad];
            self.run.roff[ad] += self.ainc[ad];
        }
        Ok(self.desc[ad].walk(base))
    }

    #[allow(clippy::too_many_arguments)]
    fn dispatch_math(
        &mut self,
        op: MathOp,
        vd: u32,
        a: Src,
        b: Src,
        c: Src,
        om: bool,
        cfg: Option<Cfg>,
    ) {
        let compare = matches!(op, MathOp::Vcmplt | MathOp::Vcmpgt | MathOp::Vcmpeq);
        let cfg = match cfg {
            Some(c) => c,
            None if om => self.cfg,
            None => Cfg {
                vl: self.cfg.vl,
                ..Cfg::RESET
            },
        };
        let (ra, rb, rc) = reads(op);
        let c = if cfg.xc { b } else { c };
        let mut sources = Vec::new();
        for (read, s) in [(ra, a), (rb, b), (rc, c)] {
            if let (true, Src::V(r)) = (read, s) {
                sources.push(r);
            }
        }
        if cfg.xc {
            if let Src::V(r) = b {
                sources.push(r);
            }
        }
        let partial = cfg.vl as usize != VLMAX || cfg.pm != 0 || cfg.chk[3] != (0, 1);
        if partial && !compare {
            sources.push(vd);
        }
        let mut deps = Vec::new();
        for &r in &sources {
            self.older(
                r,
                |a| matches!(a, Acc::MathWrite | Acc::UnpackWrite),
                &mut deps,
            );
        }
        if !compare {
            self.older(
                vd,
                |a| matches!(a, Acc::UnpackWrite | Acc::PackRead),
                &mut deps,
            );
        }
        let work = Work::Math {
            op,
            vd,
            a: self.scalar(a),
            b: self.scalar(b),
            c: self.scalar(c),
            cfg,
        };
        let e = self.push(Q_MATH, work, None, deps, Some(MATH));
        for &r in &sources {
            self.run.regs[r as usize].push((e, Acc::MathRead));
        }
        if !compare {
            self.run.regs[vd as usize].push((e, Acc::MathWrite));
        }
    }

    fn engine(&mut self, q: usize, mem: &mut Dram) -> R<Step> {
        let e = self.run.queues[q].pop_front().expect("a head");
        let work = std::mem::replace(&mut self.run.entries[e].work, Work::Guard);
        match work {
            Work::Math {
                op,
                vd,
                a,
                b,
                c,
                cfg,
            } => self.math(op, vd, a, b, c, &cfg),
            Work::Unpack {
                reg,
                words,
                mode,
                cb,
                n,
            } => self.unpack(reg, &words, mode, cb, n)?,
            Work::Pack {
                reg,
                words,
                mode,
                cb,
                n,
            } => self.pack(reg, &words, mode, cb, n),
            Work::Fill { addrs, off, inner } => {
                if let Some(reach) = &mut mem.reach {
                    reach.vector(&addrs, off, inner);
                }
                for (i, at) in addrs.iter().enumerate() {
                    self.l1[(off + i) % L1_WORDS] = mem.word(*at)?;
                }
            }
            Work::Drain {
                addrs,
                off,
                peer: None,
            } => {
                for (i, at) in addrs.iter().enumerate() {
                    mem.set_word(*at, &self.l1[(off + i) % L1_WORDS])?;
                }
            }
            Work::Drain {
                addrs,
                off,
                peer: Some(p),
            } => {
                let words = (0..addrs.len())
                    .map(|i| self.l1[(off + i) % L1_WORDS])
                    .collect();
                self.outbox.push(Landing {
                    to: p.to,
                    buf: p.buf,
                    at: (addrs[0] & 0xFFFF) as usize,
                    data: Data::Words(words),
                    count: addrs[0] >> PEER_COUNT_BIT & 1 != 0,
                });
            }
            Work::Post { to, n } => self.outbox.push(Landing {
                to,
                buf: 0,
                at: 0,
                data: Data::Credit(n),
                count: false,
            }),
            Work::Landed { .. } | Work::Guard => {}
        }
        let entry = &mut self.run.entries[e];
        entry.done = true;
        if let Some(k) = entry.kind {
            self.run.finished[k] += 1;
        }
        Ok(Step::Moved)
    }

    fn read(&self, v: Val, chunk: usize, lane: usize) -> u32 {
        match v {
            Val::Imm(x) => x,
            Val::Reg(r) => self.regs[r as usize][chunk * LANES + lane],
        }
    }

    fn math(&mut self, op: MathOp, vd: u32, a: Val, b: Val, c: Val, cfg: &Cfg) {
        let beats = (cfg.vl as usize).div_ceil(LANES);
        let at = |x: usize, i: usize| {
            let (base, stride) = cfg.chk[x];
            (base as usize + stride as usize * i) % CHUNKS
        };
        // Every beat reads before the first result lands, so a chunk the op
        // wraps onto reads its old value (anchors `inplace.ktpu`).
        let mut results = Vec::with_capacity(beats * LANES);
        for i in 0..beats {
            let tail = cfg.vl as usize % LANES;
            let lanes = if i + 1 == beats && tail != 0 {
                tail
            } else {
                LANES
            };
            let d = at(3, i);
            let (ca, cb, cc) = (at(0, i), at(1, i), at(2, i));
            for l in 0..LANES {
                let src = match cfg.xm {
                    XM_XOR => l ^ cfg.xk as usize,
                    XM_ROT => (l + cfg.xk as usize) % LANES,
                    XM_BCAST => (cfg.xk as usize + (i >> cfg.sh)) % LANES,
                    _ => l,
                };
                let (mut x, mut y) = (self.read(a, ca, l), self.read(b, cb, src));
                if cfg.xm == XM_MERGE {
                    let k = cfg.xk as usize;
                    (x, y) = if l & k == 0 {
                        (self.read(a, ca, l), self.read(a, ca, l ^ k))
                    } else {
                        (self.read(b, cb, l), self.read(b, cb, l ^ k))
                    };
                }
                let z = if cfg.xc { y } else { self.read(c, cc, l) };
                if l >= lanes {
                    continue;
                }
                let pred = self.p[cfg.pr as usize][d * LANES + l];
                let on = match cfg.pm {
                    0 => true,
                    1 => pred,
                    _ => !pred,
                };
                let at = d * LANES + l;
                match op {
                    MathOp::Vcmplt | MathOp::Vcmpgt | MathOp::Vcmpeq => {
                        self.p[cfg.pr as usize][at] = match op {
                            MathOp::Vcmplt => num::lt(x, y),
                            MathOp::Vcmpgt => num::gt(x, y),
                            _ => num::eq(x, y),
                        };
                    }
                    _ if !on => {}
                    MathOp::Vexp2d => {
                        let (r, s) = num::exp2d(x, y, TAU);
                        results.push((at, r));
                        self.sticky[at] |= s;
                    }
                    _ => results.push((at, lane_op(op, x, y, z))),
                }
            }
        }
        for (at, r) in results {
            self.regs[vd as usize][at] = r;
        }
    }

    fn unpack(&mut self, reg: u32, words: &[usize], mode: Mode, cb: u32, n: u32) -> R<()> {
        let cb = cb as usize;
        let r = &mut self.regs[reg as usize];
        match mode {
            Mode::F16 => {
                for (i, w) in words.iter().enumerate() {
                    let chunk = (cb + i) % CHUNKS;
                    for l in 0..LANES {
                        r[chunk * LANES + l] = num::from_f16(f16_at(&self.l1[*w], l));
                    }
                }
            }
            Mode::Gt4 => {
                for (q, group) in words.chunks(4).enumerate() {
                    for i in 0..4 {
                        let chunk = (cb + 4 * q + i) % CHUNKS;
                        for (g, w) in group.iter().enumerate() {
                            for e in 0..4 {
                                let b = f16_at(&self.l1[*w], i * 4 + e);
                                r[chunk * LANES + g * 4 + e] = num::from_f16(b);
                            }
                        }
                    }
                }
            }
            Mode::F32 => {
                for i in 0..n as usize {
                    let chunk = (cb + i) % CHUNKS;
                    for l in 0..LANES {
                        let w = &self.l1[words[2 * i + l / 8]];
                        let at = (l % 8) * 4;
                        let b = u32::from_le_bytes(w[at..at + 4].try_into().expect("4 bytes"));
                        r[chunk * LANES + l] = num::from_f32(b);
                    }
                }
            }
            Mode::Mx7(_) => return Err("an MX7 unpack faults F_MODE".into()),
        }
        Ok(())
    }

    fn pack(&mut self, reg: u32, words: &[usize], mode: Mode, cb: u32, n: u32) {
        let cb = cb as usize;
        let r = &self.regs[reg as usize];
        match mode {
            Mode::F16 => {
                for (i, w) in words.iter().enumerate() {
                    let chunk = (cb + i) % CHUNKS;
                    for l in 0..LANES {
                        let b = num::to_f16(r[chunk * LANES + l]);
                        self.l1[*w][l * 2..l * 2 + 2].copy_from_slice(&b.to_le_bytes());
                    }
                }
            }
            Mode::Gt4 => {
                for (q, group) in words.chunks(4).enumerate() {
                    for i in 0..4 {
                        let chunk = (cb + 4 * q + i) % CHUNKS;
                        for (g, w) in group.iter().enumerate() {
                            for e in 0..4 {
                                let b = num::to_f16(r[chunk * LANES + g * 4 + e]);
                                let at = (i * 4 + e) * 2;
                                self.l1[*w][at..at + 2].copy_from_slice(&b.to_le_bytes());
                            }
                        }
                    }
                }
            }
            Mode::F32 => {
                for i in 0..n as usize {
                    let chunk = (cb + i) % CHUNKS;
                    for l in 0..LANES {
                        let b = num::to_f32(r[chunk * LANES + l]);
                        let at = (l % 8) * 4;
                        self.l1[words[2 * i + l / 8]][at..at + 4].copy_from_slice(&b.to_le_bytes());
                    }
                }
            }
            Mode::Mx7(b) => {
                let mut x = [[0u16; mx7::KBLOCK]; mx7::LANES];
                for (l, row) in x.iter_mut().enumerate() {
                    for (k, v) in row.iter_mut().enumerate() {
                        let chunk = 2 * l + k / LANES;
                        *v = num::to_f16(r[chunk * LANES + k % LANES]);
                    }
                }
                let out = mx7::entry(&x).write(b);
                for (w, word) in words.iter().zip(out) {
                    self.l1[*w] = word;
                }
            }
        }
    }
}

fn f16_at(w: &Word, e: usize) -> u16 {
    u16::from_le_bytes([w[e * 2], w[e * 2 + 1]])
}

/// Which of a, b, c an op reads (ISA.VC.053, .063, .064).
fn reads(op: MathOp) -> (bool, bool, bool) {
    use MathOp::*;
    match op {
        Vmov | Vneg | Vabs | Vexp2 | Vlog2 | Vinv | Vrsqrt => (true, false, false),
        Vadd | Vsub => (true, false, true),
        Vmul | Vmax | Vmin | Vexp2d | Vcmplt | Vcmpgt | Vcmpeq => (true, true, false),
        Vsel | Vfma | Vfnma => (true, true, true),
    }
}

/// One lane of `op` (ISA.VC.054-.056).
pub fn lane_op(op: MathOp, a: u32, b: u32, c: u32) -> u32 {
    use MathOp::*;
    match op {
        Vmov => num::picked(a),
        Vneg => num::neg(a),
        Vabs => num::abs(a),
        Vadd => num::fma(a, num::ONE, c),
        Vsub => num::fma(a, num::ONE, c ^ 1 << 23),
        Vmul => num::fma(a, b, 0),
        Vfma => num::fma(a, b, c),
        Vfnma => num::fma(a ^ 1 << 23, b, c),
        Vmax => num::picked(if num::lt(a, b) { b } else { a }),
        Vmin => num::picked(if num::gt(a, b) { b } else { a }),
        Vsel => num::picked(if num::nonzero(c) { a } else { b }),
        Vexp2 => num::exp2(a),
        Vlog2 => num::log2(a),
        Vinv => num::inv(a),
        Vrsqrt => num::rsqrt(a),
        Vcmplt | Vcmpgt | Vcmpeq | Vexp2d => unreachable!("a compare or vexp2d has its own lane"),
    }
}
