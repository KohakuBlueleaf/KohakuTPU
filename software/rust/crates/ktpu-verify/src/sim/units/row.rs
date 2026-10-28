//! A Streaming Row Unit (`sru_cu.v`).
//!
//! The shell runs its words in order, one at a time; every word is one
//! completion. CFG, LOAD and STORE touch only the unit and memory. A RUN
//! holds the shell until the job is done and its last output left:
//!
//! * A from a COUNT stream (`amem` 0): the unit posts its producer min(the A
//!   FIFO's depth, the stream's words) at the RUN and min(burst, the rest)
//!   for every burst of words it takes; the words come by bursts into
//!   buffer 0, counted.
//! * Out to a unit (`peer`): bursts of at most `sr.burst` words, never across
//!   a sub-row, each leaving once the credits hold its length, into the
//!   sink's buffer `pbuf` from granule `poff` on.
//!
//! A sub-row's output is taken as ready once all its input is in (the
//! buffered form; a single-pass job is earlier on the card, which only
//! relaxes the waits), and input runs at most `sr.held` sub-rows plus the A
//! FIFO ahead of the output that has left.

use ktpu_isa::unit::sru::config::{lookup, G};
use ktpu_isa::W256;

use crate::adapter::isa::Fields;
use crate::model::clock::Clock;
use crate::model::finding::Kind;
use crate::sim::memory::Memory;
use crate::sim::{Kinded, Pk, Sender, Sim, Sink, Unit};

/// One RUN: input words taken, output words sent, in the shell's events.
pub(crate) struct Job {
    f: Fields,
    nw: i64,
    nsr: i64,
    onw: i64,
    stream: bool,
    peer: bool,
    total: i64,
    inn: i64,
    out: i64,
    /// Stream words the producer is still to be posted (RUN `words`).
    cr_left: i64,
    clock: Clock,
}

impl Job {
    fn rows_in(&self) -> i64 {
        if self.nw != 0 {
            self.inn / self.nw
        } else {
            self.nsr
        }
    }
}

pub(crate) struct Row {
    pub rx: usize,
    pub taken: i64,
    pub cr: usize,
    pub spent: i64,
    /// The configuration words the shell reads (sru_cu.v cfg[0], cfg[1]).
    cfg: [i64; 2],
    pub shell: usize,
    pc: usize,
    job: Option<Job>,
    why: String,
}

impl Row {
    /// Global configuration field `name` (`isa/sru.py` G).
    fn field(&self, name: &str) -> i64 {
        let (word, lo, width) = lookup(G, name).expect("a G field");
        let v = self.cfg.get(word as usize).copied().unwrap_or(0);
        (v >> lo) & ((1i64 << width) - 1)
    }
}

impl Sim<'_> {
    pub(crate) fn new_row(&mut self, ui: usize, key: (u32, u8, u8), name: String) -> Unit {
        let rx = self.counter(&format!("rx:{name}"));
        let cr = self.counter(&format!("cr:{name}"));
        let done = self.counter(&format!("done:{name}"));
        let recv = self.counter(&format!("recv:{name}"));
        let acks = self.counter(&format!("acks:{name}"));
        let port = self.add_proc(Pk::Port, format!("{name} port"));
        let shell = self.add_proc(Pk::SrShell(ui), format!("{name} shell"));
        Unit {
            key,
            mem: Memory::new(&name),
            name,
            port,
            done,
            recv,
            acks,
            completer: shell,
            landings: Vec::new(),
            words: Vec::new(),
            kind: Kinded::Sr(Box::new(Row {
                rx,
                taken: 0,
                cr,
                spent: 0,
                cfg: [0, 0],
                shell,
                pc: 0,
                job: None,
                why: String::new(),
            })),
        }
    }

    /// `n` words into the A FIFO (buffer 0, any offset: the FIFO takes them
    /// in arrival order).
    pub(crate) fn sr_land(
        &mut self,
        u: usize,
        buf: i64,
        n: i64,
        count: bool,
        clock: &Clock,
    ) -> Option<String> {
        if buf != 0 {
            return Some(format!("buffer {buf}: a row unit takes only buffer 0 (A)"));
        }
        if count {
            let (rx, port) = (self.sr(u).rx, self.units[u].port);
            self.counters[rx].add(n, clock, port);
        }
        None
    }

    pub(crate) fn sr_rest(&self, u: usize) -> Vec<String> {
        let r = self.sr(u);
        let mut out = Vec::new();
        let rx = self.counters[r.rx].value;
        if rx != r.taken {
            out.push(format!("{} words landed and never taken", rx - r.taken));
        }
        let cr = self.counters[r.cr].value;
        if cr != r.spent {
            out.push(format!("{} credits received and never spent", cr - r.spent));
        }
        out
    }

    pub(crate) fn sr_done(&self, u: usize) -> bool {
        let r = self.sr(u);
        r.pc >= self.units[u].words.len() && r.job.is_none() && self.node_done_for(u)
    }

    pub(crate) fn sr_blocked(&self, u: usize) -> String {
        let r = self.sr(u);
        if r.job.is_some() {
            format!("RUN: {}", r.why)
        } else {
            "waiting for words from the node".into()
        }
    }

    fn sr_retire(&mut self, u: usize, clock: &Clock) {
        let (done, recv, sh) = (self.units[u].done, self.units[u].recv, self.sr(u).shell);
        self.counters[done].add(1, clock, sh);
        self.counters[recv].add(1, clock, sh);
    }

    pub(crate) fn sr_step(&mut self, u: usize) -> bool {
        if self.sr(u).job.is_some() {
            return self.sr_job_step(u);
        }
        let r = self.sr(u);
        let (pc, sh) = (r.pc, r.shell);
        if pc >= self.units[u].words.len() {
            return false;
        }
        let word = self.units[u].words[pc].word;
        let given = self.units[u].words[pc].given.clone();
        self.sr_mut(u).pc += 1;
        let clock = self.tick(sh, &[given]);
        let dec = self.t.isa.sru.set.decode(W256(word.0)).ok();
        match dec.map(|(n, f)| (n, Fields::from(f))) {
            None => {
                let name = self.procs[sh].name.clone();
                self.find(
                    Kind::Model,
                    name,
                    "a word decodes as no row unit instruction",
                );
            }
            Some(("CFG", f)) => {
                let w = f.g("word");
                if w == 0 || w == 1 {
                    self.sr_mut(u).cfg[w as usize] = f.g("value");
                }
            }
            Some(("POST", f)) => {
                self.post(u, (f.g("x"), f.g("y")), f.g("n"), &clock, sh);
            }
            Some(("RUN", f)) => {
                self.sr_start(u, f, clock);
                return true;
            }
            Some(_) => {}
        }
        self.sr_retire(u, &clock);
        true
    }

    fn sr_start(&mut self, u: usize, f: Fields, clock: Clock) {
        let r = self.sr(u);
        let sh = r.shell;
        let (nw, nsr, out) = (r.field("NW"), r.field("NSR"), r.field("OUT"));
        let onw = match out {
            0 => 0,
            1 => nw,
            _ => nw / 2,
        };
        let stream = f.g("amem") == 0;
        let total = nw * nsr;
        let words = f.g("words");
        let job = Job {
            nw,
            nsr,
            onw,
            stream,
            peer: f.g("peer") != 0 && onw > 0,
            total,
            inn: if stream { 0 } else { total },
            out: 0,
            cr_left: words,
            clock: clock.clone(),
            f,
        };
        self.sr_mut(u).job = Some(job);
        if stream {
            if words != total {
                let name = self.procs[sh].name.clone();
                self.find(
                    Kind::Model,
                    name,
                    format!("RUN words={words}, the job streams {total}"),
                );
            }
            let fifo = self.t.facts.sr_fifo;
            self.sr_post(u, fifo, &clock);
        }
    }

    /// min(n, what is left) to the producer: the posts sum to the stream.
    fn sr_post(&mut self, u: usize, n: i64, clock: &Clock) {
        let sh = self.sr(u).shell;
        let job = self.sr_mut(u).job.as_mut().expect("a job");
        let n = n.min(job.cr_left);
        if n > 0 {
            let to = (job.f.g("ax"), job.f.g("ay"));
            job.cr_left -= n;
            self.post(u, to, n, clock, sh);
        }
    }

    fn sr_job_step(&mut self, u: usize) -> bool {
        let r = self.sr(u);
        let sh = r.shell;
        let job = r.job.as_ref().expect("a job");
        if job.stream && job.inn < job.total {
            let rows_out = if job.peer { job.out / job.onw } else { job.nsr };
            let (burst, held) = (self.t.facts.sr_burst, self.t.facts.sr_held);
            let room = (rows_out + held) * job.nw + self.t.facts.sr_fifo - job.inn;
            let have = self.counters[r.rx].value - r.taken;
            let n = have.min(room).min(job.total - job.inn);
            if n > 0 {
                let rel = [
                    job.clock.clone(),
                    self.counters[r.rx].clock_to(r.taken + n, &[]),
                ];
                let clock = self.tick(sh, &rel);
                let r = self.sr_mut(u);
                let job = r.job.as_mut().expect("a job");
                let before = job.inn / burst;
                job.inn += n;
                let posts = job.inn / burst - before;
                job.clock = clock.clone();
                r.taken += n;
                for _ in 0..posts {
                    self.sr_post(u, burst, &clock);
                }
                return true;
            }
        }
        let r = self.sr(u);
        let job = r.job.as_ref().expect("a job");
        if job.peer && job.out < job.onw * job.nsr {
            let ready = job.rows_in() * job.onw;
            let end = (job.out / job.onw + 1) * job.onw;
            let n = self.t.facts.sr_burst.min(end - job.out);
            if job.out + n <= ready && self.counters[r.cr].value - r.spent >= n {
                let rel = [
                    job.clock.clone(),
                    self.counters[r.cr].clock_to(r.spent + n, &[]),
                ];
                let clock = self.tick(sh, &rel);
                let mesh = self.units[u].key.0;
                let r = self.sr_mut(u);
                r.spent += n;
                let job = r.job.as_mut().expect("a job");
                let f = &job.f;
                let sink = Sink {
                    to: (f.g("px") as u8, f.g("py") as u8),
                    far: None,
                    mesh,
                    buf: f.g("pbuf"),
                    count: f.g("count") != 0,
                    signal: false,
                    ack: (0, 0),
                };
                let off = (f.g("poff") + job.out) & 0xFFFF;
                job.out += n;
                job.clock = clock.clone();
                self.send(Sender::Unit(u), sink, off, n, &clock, sh);
                return true;
            }
        }
        let r = self.sr(u);
        let job = r.job.as_ref().expect("a job");
        if job.inn >= job.total && (!job.peer || job.out >= job.onw * job.nsr) {
            let last = job.clock.clone();
            self.sr_mut(u).job = None;
            let clock = self.tick(sh, &[last]);
            self.sr_retire(u, &clock);
            return true;
        }
        let why = format!(
            "{} of {} words in ({} landed), {} out, credits {}",
            job.inn,
            job.total,
            self.counters[r.rx].value - r.taken,
            job.out,
            self.counters[r.cr].value - r.spent
        );
        self.sr_mut(u).why = why;
        false
    }
}
