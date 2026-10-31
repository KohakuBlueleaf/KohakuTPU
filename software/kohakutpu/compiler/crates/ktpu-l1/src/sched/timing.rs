//! The V2 core's timing of an image (`l2.timing`): the cycles the image
//! scheduler (L1.SCHED.005) and the vector lowering compare candidates by.
//!
//! The front end dispatches a word a cycle in program order, an op's
//! configuration words (the encoder's) before it; an instruction that
//! evaluates a condition leaves on its second cycle; the math, unpack and pack
//! queues hold `vc.queue_depth` entries, fill and drain `vc.dma_queue_depth`.
//! Each engine runs its queue in order; a fill or drain occupies the one
//! memory port its words times the path's cycles a word, a fill landing the
//! path's latency after its last word. A loop runs LOOP_PASSES passes; the
//! rest are the last pass's cycles each.

use std::collections::{BTreeSet, HashMap};
use std::sync::OnceLock;

use ktpu_machine::Machine;
use regex::Regex;

use crate::error::Result;
use crate::lower::image::{CoreFacts, Encoder, EXPLICIT, UNKNOWN};
use crate::lower::{image, ops};
use crate::text::{emit_at, fmt, read, Env, Expander, Module, Stmt, Term, Val};

/// The vector core timing parameters, one table read by this timing and the
/// image scheduler alike (RUL.063).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Params {
    /// math -> math: a dependent op starts this long after its producer's
    /// last beat.
    pub math_math: i64,
    /// And this much later where it reads the value through the crossbar.
    pub xbar: i64,
    /// unpack -> math, after the unpack's last word.
    pub unpack_math: i64,
    /// math -> pack.
    pub math_pack: i64,
    /// Dispatch cycles of a front-end instruction that evaluates a condition.
    pub cond: i64,
    /// SETI dispatch occupancy including its immediate word.
    pub seti: i64,
    /// Idle cycles between MX7 packs, beyond active RF reads.
    pub mx7_issue_gap: i64,
    /// Active RF-read beats of an MX7 pack, excluding its issue gap.
    pub mx7_pack_words: i64,
    /// `vc.chunks`: a register's words, the beats of a full-length op.
    pub chunks: i64,
    /// `vc.lanes`: elements one beat carries.
    pub lanes: i64,
    /// `vc.rf_banks`: a word's bank is its chunk mod this.
    pub banks: i64,
}

impl Params {
    /// The machine's `vc.timing` (RUL.063; tier-2 measured).
    pub fn of(m: &Machine) -> Params {
        let t = &m.vc.timing;
        let halve = if crate::mutant::on("l1.sched/latency") {
            2
        } else {
            1
        };
        let less = crate::mutant::on("l2.timing/lat-mm-19") as i64;
        Params {
            math_math: t.math_math as i64 / halve - less,
            xbar: t.xbar as i64,
            unpack_math: t.unpack_math as i64,
            math_pack: t.math_pack as i64,
            cond: t.cond as i64,
            seti: t.seti as i64,
            mx7_issue_gap: t.mx7_issue_gap as i64,
            mx7_pack_words: t.mx7_pack_words as i64,
            chunks: chunks(m) as i64,
            lanes: if crate::mutant::on("l1.sched/fixed-shape") {
                16
            } else {
                m.vc.lanes as i64
            },
            banks: if crate::mutant::on("l1.sched/fixed-shape") {
                2
            } else {
                m.vc.rf_banks as i64
            },
        }
    }
}

/// The machine's `vc.chunks`, the words of a register.
pub fn chunks(m: &Machine) -> u32 {
    if crate::mutant::on("l1.sched/fixed-shape") {
        8
    } else {
        m.vc.chunks
    }
}

/// Passes a loop is timed for; the rest take the last pass's cycles each.
pub const LOOP_PASSES: i64 = 4;
/// The predicate a compare writes and a predicated op reads, as a dependence.
pub const PRED: &str = "#p1";

const COND_OPS: [&str; 3] = ["vsync", "setvl", "halt"];

fn engine(name: &str) -> Option<&'static str> {
    Some(match name {
        "unpack" => "U",
        "math" => "M",
        "pack" => "P",
        "fill" => "F",
        "drain" => "D",
        "fe" => "fe",
        _ => return None,
    })
}

const ENGINES: [&str; 5] = ["M", "U", "P", "F", "D"];

fn re(cell: &'static OnceLock<Regex>, pat: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pat).expect("a timing pattern"))
}

static REG: OnceLock<Regex> = OnceLock::new();
static SYM: OnceLock<Regex> = OnceLock::new();
static NKW: OnceLock<Regex> = OnceLock::new();
static CB0: OnceLock<Regex> = OnceLock::new();
static MODS: OnceLock<Regex> = OnceLock::new();
static VLKW: OnceLock<Regex> = OnceLock::new();
static PREDKW: OnceLock<Regex> = OnceLock::new();
static SEL: OnceLock<Regex> = OnceLock::new();
static PAIR: OnceLock<Regex> = OnceLock::new();

/// An image line as `(template, registers)`: each `vN` named `{i}` by first
/// appearance, braces doubled (`banks.symbolic`).
pub fn symbolic(text: &str) -> (String, Vec<String>) {
    let r = re(&REG, r"\bv(\d+)\b");
    let mut regs: Vec<String> = Vec::new();
    for c in r.captures_iter(text) {
        let name = format!("v{}", &c[1]);
        if !regs.contains(&name) {
            regs.push(name);
        }
    }
    let escaped = text.replace('{', "{{").replace('}', "}}");
    let out = r.replace_all(&escaped, |c: &regex::Captures| {
        let name = format!("v{}", &c[1]);
        format!("{{{}}}", regs.iter().position(|x| *x == name).unwrap())
    });
    (out.into_owned(), regs)
}

/// One instruction's engine, reads, writes and occupancy (`modulo.parse`).
#[derive(Clone, Debug)]
pub struct MOp {
    pub eng: &'static str,
    pub reads: Vec<String>,
    pub writes: Vec<String>,
    pub occ: i64,
    pub xreads: Vec<String>,
}

/// Top-level comma-separated operands of a math op.
fn operands(rest: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut cur = String::new();
    for ch in rest.chars() {
        if ch == '(' || ch == '[' {
            depth += 1;
        } else if ch == ')' || ch == ']' {
            depth -= 1;
        }
        if ch == ',' && depth == 0 {
            out.push(cur.trim().to_string());
            cur.clear();
        } else {
            cur.push(ch);
        }
    }
    if !cur.trim().is_empty() {
        out.push(cur.trim().to_string());
    }
    out
}

fn sym_ids(s: &str) -> Vec<usize> {
    re(&SYM, r"\{(\d+)\}")
        .captures_iter(s)
        .map(|c| c[1].parse().unwrap())
        .collect()
}

fn walk_of(opnd: &str, syms: &[String]) -> Option<(String, i64, i64)> {
    let first = *sym_ids(opnd).first()?;
    let sym = syms[first].clone();
    match re(&SEL, r"\}\[(\d+)\s*(:?)\s*\]").captures(opnd) {
        None => Some((sym, 0, 1)),
        Some(c) => Some((
            sym,
            c[1].parse().unwrap(),
            if c[2].is_empty() { 0 } else { 1 },
        )),
    }
}

/// Beats a two-copy register file holds: a, b and c all vector, three
/// distinct {register, chunk} words of one bank.
fn hold_beats(op: &str, opnds: &[String], syms: &[String], beats: i64, p: &Params) -> i64 {
    let mut fields = vec!["a"];
    if ops::reads_b(op) {
        fields.push("b");
    }
    if ops::reads_c(op) {
        fields.push("c");
    }
    let srcs: HashMap<&str, &String> = fields.iter().copied().zip(opnds.iter().skip(1)).collect();
    if srcs.len() != 3 || !["a", "b", "c"].iter().all(|f| srcs.contains_key(f)) {
        return 0;
    }
    if srcs["c"].contains('(') {
        return 0;
    }
    let walks: Vec<_> = ["a", "b", "c"]
        .iter()
        .map(|f| walk_of(srcs[f], syms))
        .collect();
    if walks.iter().any(Option::is_none) {
        return 0;
    }
    let walks: Vec<(String, i64, i64)> = walks.into_iter().map(Option::unwrap).collect();
    let mut held = 0;
    for i in 0..beats {
        let words: BTreeSet<(String, i64)> = walks
            .iter()
            .map(|(s, b, st)| (s.clone(), (b + st * i).rem_euclid(p.chunks)))
            .collect();
        let banks: BTreeSet<i64> = words.iter().map(|(_, c)| c % p.banks).collect();
        if words.len() == 3 && banks.len() == 1 {
            held += 1;
        }
    }
    held
}

/// `modulo.parse`: one instruction of a symbolic template.
pub fn parse(tmpl: &str, syms: &[String], copies: u32, p: &Params) -> MOp {
    let (op, rest) = tmpl.split_once(' ').unwrap_or((tmpl, ""));
    let n = re(&NKW, r"\bn=(\d+)")
        .captures(tmpl)
        .map(|c| c[1].parse::<i64>().unwrap());
    let f32 = tmpl.contains(" f32");
    let sym0 = || syms.first().cloned().unwrap_or_default();
    if op == "vunpk" {
        let cb = re(&CB0, r"\{0\}\[(\d+)\s*:\s*\]")
            .captures(tmpl)
            .map(|c| c[1].parse::<i64>().unwrap());
        let chunks = n.unwrap_or(p.chunks - cb.unwrap_or(0));
        let words = chunks * if f32 { 2 } else { 1 };
        let reads = if chunks < p.chunks {
            vec![sym0()]
        } else {
            Vec::new()
        };
        return MOp {
            eng: "U",
            reads,
            writes: vec![sym0()],
            occ: words,
            xreads: Vec::new(),
        };
    }
    if op == "vpack" {
        let mut words = if tmpl.contains(" mx7") {
            p.mx7_pack_words + p.mx7_issue_gap
        } else {
            n.unwrap_or(p.chunks)
        };
        if f32 {
            words *= 2;
        }
        return MOp {
            eng: "P",
            reads: vec![sym0()],
            writes: Vec::new(),
            occ: words,
            xreads: Vec::new(),
        };
    }
    let body = re(&MODS, r"\s+(vl|if|unless)=\S+").replace_all(rest, "");
    let opnds = operands(&body);
    let beats = match re(&VLKW, r"\bvl=(\d+)").captures(tmpl) {
        Some(c) => {
            let vl: i64 = c[1].parse().unwrap();
            1.max((vl + p.lanes - 1).div_euclid(p.lanes))
        }
        None => p.chunks,
    };
    let dest = opnds.first().cloned().unwrap_or_default();
    let tail = opnds.iter().skip(1).cloned().collect::<Vec<_>>().join(",");
    let mut reads: BTreeSet<String> = sym_ids(&tail)
        .into_iter()
        .map(|i| syms[i].clone())
        .collect();
    let xreads: BTreeSet<String> = opnds
        .iter()
        .skip(1)
        .filter(|x| x.contains('('))
        .flat_map(|x| sym_ids(x))
        .map(|i| syms[i].clone())
        .collect();
    let writes = if ops::is_compare(op) {
        vec![PRED.to_string()]
    } else if let Some(i) = sym_ids(&dest).first() {
        vec![syms[*i].clone()]
    } else {
        Vec::new()
    };
    if re(&PREDKW, r"\b(if|unless)=p").is_match(tmpl) {
        reads.insert(PRED.to_string());
    }
    let hold = if copies == 2 {
        hold_beats(op, &opnds, syms, beats, p)
    } else {
        0
    };
    MOp {
        eng: "M",
        reads: reads.into_iter().collect(),
        writes,
        occ: beats + hold,
        xreads: xreads.into_iter().collect(),
    }
}

/// The core's state as a sequence of instructions issues on it.
#[derive(Clone, Debug)]
pub(crate) struct Core {
    p: Params,
    copies: u32,
    lat: i64,
    cpw: i64,
    queue: usize,
    depth: HashMap<&'static str, usize>,
    fe: i64,
    mem: i64,
    free: HashMap<&'static str, i64>,
    starts: HashMap<&'static str, Vec<i64>>,
    done: HashMap<&'static str, Vec<i64>>,
    ready: HashMap<String, i64>,
    ready_p: HashMap<String, i64>,
    read_end: HashMap<String, i64>,
    pack_end: HashMap<String, i64>,
    unpack_end: HashMap<String, i64>,
    by_math: BTreeSet<String>,
    parsed: HashMap<String, MOp>,
}

impl Core {
    pub(crate) fn new(copies: u32, fill: (i64, i64), m: &Machine) -> Core {
        let q = m.vc.queue_depth as usize;
        let dq = m.vc.dma_queue_depth as usize;
        Core {
            p: Params::of(m),
            copies,
            lat: fill.0,
            cpw: fill.1,
            queue: q,
            depth: [("M", q), ("U", q), ("P", q), ("F", dq), ("D", dq)].into(),
            fe: 0,
            mem: 0,
            free: ENGINES.iter().map(|e| (*e, 0)).collect(),
            starts: ENGINES.iter().map(|e| (*e, Vec::new())).collect(),
            done: ENGINES.iter().map(|e| (*e, Vec::new())).collect(),
            ready: HashMap::new(),
            ready_p: HashMap::new(),
            read_end: HashMap::new(),
            pack_end: HashMap::new(),
            unpack_end: HashMap::new(),
            by_math: BTreeSet::new(),
            parsed: HashMap::new(),
        }
    }

    /// Every time in the state `d` cycles later.
    fn shift(&mut self, d: i64) {
        self.fe += d;
        self.mem += d;
        for e in ENGINES {
            *self.free.get_mut(e).unwrap() += d;
            let s = self.starts.get_mut(e).unwrap();
            let keep = s.len().saturating_sub(self.queue);
            *s = s[keep..].iter().map(|x| x + d).collect();
            for t in self.done.get_mut(e).unwrap() {
                *t += d;
            }
        }
        for m in [
            &mut self.ready,
            &mut self.ready_p,
            &mut self.read_end,
            &mut self.pack_end,
            &mut self.unpack_end,
        ] {
            for v in m.values_mut() {
                *v += d;
            }
        }
    }

    /// The front end's cycle after dispatching `words` into `eng`'s queue.
    fn entered(&self, eng: &'static str, words: i64) -> i64 {
        let q = &self.starts[eng];
        let depth = self.depth[eng];
        let fe = self.fe + words;
        if q.len() >= depth {
            fe.max(q[q.len() - depth])
        } else {
            fe
        }
    }

    /// Dispatch `words` into `eng`'s queue: the cycle the entry is in.
    fn enter(&mut self, eng: &'static str, words: i64) -> i64 {
        self.fe = self.entered(eng, words);
        self.fe
    }

    fn mop(&mut self, text: &str) -> MOp {
        match self.parsed.get(text) {
            Some(o) => o.clone(),
            None => {
                let (tmpl, syms) = symbolic(text);
                let o = parse(&tmpl, &syms, self.copies, &self.p);
                self.parsed.insert(text.to_string(), o.clone());
                o
            }
        }
    }

    /// The cycle `o` would start at, dispatched `words` words now.
    fn start_of(&self, o: &MOp, words: i64) -> i64 {
        let eng = o.eng;
        let mut start = self.entered(eng, words).max(self.free[eng]);
        let get = |m: &HashMap<String, i64>, k: &String| *m.get(k).unwrap_or(&0);
        match eng {
            "M" => {
                for r in &o.reads {
                    let x = if self.by_math.contains(r) && o.xreads.contains(r) {
                        self.p.xbar
                    } else {
                        0
                    };
                    start = start.max(get(&self.ready, r) + x);
                }
                for w in &o.writes {
                    start = start.max(get(&self.pack_end, w).max(get(&self.unpack_end, w)));
                }
            }
            "U" => {
                for w in &o.writes {
                    start = start.max(get(&self.read_end, w));
                }
            }
            _ => {
                for r in &o.reads {
                    start = start.max(get(&self.ready_p, r));
                }
            }
        }
        start
    }

    /// The cycle instruction `text` of `words` dispatch words would start
    /// at, the core unchanged.
    pub(crate) fn peek(&mut self, text: &str, words: i64) -> i64 {
        let o = self.mop(text);
        self.start_of(&o, words)
    }

    /// Issue the instruction `text` of `words` dispatch words: the cycle it
    /// starts.
    pub(crate) fn op(&mut self, text: &str, words: i64) -> i64 {
        let o = self.mop(text);
        let eng = o.eng;
        let start = self.start_of(&o, words);
        self.enter(eng, words);
        let end;
        let get = |m: &HashMap<String, i64>, k: &String| *m.get(k).unwrap_or(&0);
        match eng {
            "M" => {
                end = start + o.occ;
                for r in &o.reads {
                    let v = get(&self.read_end, r).max(end);
                    self.read_end.insert(r.clone(), v);
                }
                for w in &o.writes {
                    self.ready.insert(w.clone(), end + self.p.math_math);
                    self.ready_p.insert(w.clone(), end + self.p.math_pack);
                    self.by_math.insert(w.clone());
                }
            }
            "U" => {
                end = start + o.occ;
                for w in &o.writes {
                    self.ready.insert(w.clone(), end + self.p.unpack_math);
                    self.ready_p.insert(w.clone(), end + self.p.unpack_math);
                    self.unpack_end.insert(w.clone(), end);
                    self.by_math.remove(w);
                }
            }
            _ => {
                end = start + o.occ;
                for r in &o.reads {
                    let v = get(&self.pack_end, r).max(end);
                    self.pack_end.insert(r.clone(), v);
                }
            }
        }
        self.free.insert(eng, end);
        self.starts.get_mut(eng).unwrap().push(start);
        self.done.get_mut(eng).unwrap().push(end);
        start
    }

    /// A fill (F) or drain (D) of `words` memory words, `extra` cycles of its runs.
    fn dma(&mut self, eng: &'static str, words: i64, extra: i64) {
        if crate::mutant::on("l2.timing/fill-free") {
            self.lat = 0;
        }
        let start = self.enter(eng, 1).max(self.free[eng]).max(self.mem);
        self.mem = start + words * self.cpw + extra;
        self.free.insert(eng, self.mem);
        self.starts.get_mut(eng).unwrap().push(start);
        let land = self.mem + if eng == "F" { self.lat } else { 0 };
        self.done.get_mut(eng).unwrap().push(land);
    }

    fn sync(&mut self, waiter: &'static str, kind: &'static str, slack: i64) {
        let done = &self.done[kind];
        let n = done.len() as i64 - slack;
        let at = if n > 0 { done[n as usize - 1] } else { 0 };
        if waiter == "fe" {
            self.fe = (self.fe + self.p.cond).max(at);
            return;
        }
        let start = self.enter(waiter, 1).max(self.free[waiter]).max(at);
        self.free.insert(waiter, start);
        self.starts.get_mut(waiter).unwrap().push(start);
    }

    /// The unpack queue waits for every fill issued before.
    fn bar(&mut self) {
        let at = self.done["F"].iter().copied().max().unwrap_or(0);
        let start = self.enter("U", 1).max(self.free["U"]).max(at);
        self.free.insert("U", start);
        self.starts.get_mut("U").unwrap().push(start);
    }

    fn other(&mut self, words: i64) {
        self.fe += words;
    }

    pub(crate) fn end(&self) -> i64 {
        let mut out = self.fe;
        for e in ENGINES {
            out = out.max(self.free[e]);
            out = out.max(self.done[e].iter().copied().max().unwrap_or(0));
        }
        out
    }
}

/// The words a descriptor's walk moves: the product of its counts.
pub fn walk_words(walk: &Term) -> i64 {
    let text = fmt(walk);
    let counts: Vec<i64> = re(&PAIR, r"\(\s*-?\d+\s*,\s*(\d+)\s*\)")
        .captures_iter(&text)
        .map(|c| c[1].parse().unwrap())
        .collect();
    if counts.is_empty() {
        1
    } else {
        counts.iter().product()
    }
}

/// The words of a walk's contiguous runs: its first level's at a word stride.
fn walk_run(walk: &Term) -> i64 {
    let text = fmt(walk);
    let first = re(&PAIR, r"\(\s*-?\d+\s*,\s*(\d+)\s*\)").find(&text);
    let pair = first.map_or(String::new(), |m| m.as_str().to_string());
    let (stride, count) = pair
        .trim_matches(['(', ')'])
        .split_once(',')
        .unwrap_or(("", "1"));
    match (stride.trim().parse::<i64>(), count.trim().parse::<i64>()) {
        (Ok(32), Ok(count)) => count,
        _ => 1,
    }
}

/// What a launch gives an image beyond its text: its integer arguments, the
/// `desc` statements sent with it, the cycles of a memory walk's run.
#[derive(Clone, Copy, Default)]
pub struct Sent<'a> {
    pub args: &'a [i128],
    pub descs: &'a [Stmt],
    pub run: i64,
}

/// Cycles image `name` of `text` takes on a core of `vregs` registers and
/// `copies` read copies, its parameters at 0, a fill or drain taking `fill`
/// = (latency, cycles a word).
pub fn cycles(
    text: &str,
    name: &str,
    vregs: u32,
    copies: u32,
    fill: (i64, i64),
    machine: &Machine,
) -> Result<i64> {
    if crate::mutant::on("l1.sched/timing-flat") {
        return Ok(0);
    }
    let mut module = read(text, &format!("<{name}>"))?;
    module.vregs = vregs;
    cycles_of(&module, name, vregs, copies, fill, machine)
}

/// [`cycles`] of a module already read (its `vregs` set).
pub fn cycles_of(
    module: &Module,
    name: &str,
    vregs: u32,
    copies: u32,
    fill: (i64, i64),
    machine: &Machine,
) -> Result<i64> {
    cycles_with(module, name, &Sent::default(), vregs, copies, fill, machine)
}

/// [`cycles_of`] of a launch's `sent` image, its parameters past the
/// arguments at 0.
pub fn cycles_with(
    module: &Module,
    name: &str,
    sent: &Sent,
    vregs: u32,
    copies: u32,
    fill: (i64, i64),
    machine: &Machine,
) -> Result<i64> {
    let r = module.reader();
    let img = &module.images[name];
    let macros = module.macro_table();
    let values = sent.args.iter().copied().chain(std::iter::repeat(0));
    let env: Env = img
        .params
        .iter()
        .zip(values)
        .map(|(p, v)| (p.clone(), Val::Int(v)))
        .collect();
    let stmts = Expander::new(&macros, &r, true).stmts(&img.stmts, &env)?;
    let isa = image::isa_for(machine, vregs)?;
    let facts = CoreFacts::of(machine, vregs);
    let mut t = Timing {
        enc: Encoder::new(&r, &isa, &facts, UNKNOWN, None),
        core: Core::new(copies, fill, machine),
        state: UNKNOWN,
        scalars: HashMap::new(),
        words_of: HashMap::new(),
        runs_of: HashMap::new(),
        run: sent.run,
    };
    t.run(sent.descs)?;
    t.run(&stmts)?;
    Ok(t.core.end())
}

struct Timing<'a> {
    enc: Encoder<'a>,
    core: Core,
    state: image::State,
    scalars: HashMap<String, i128>,
    words_of: HashMap<String, i64>,
    runs_of: HashMap<String, i64>,
    run: i64,
}

impl Timing<'_> {
    fn run(&mut self, stmts: &[Stmt]) -> Result<()> {
        for st in stmts {
            let op = st.op.as_str();
            if op == "loop" {
                let key = st
                    .positional()
                    .first()
                    .and_then(|p| p.as_name())
                    .unwrap_or("");
                let n = *self.scalars.get(key).unwrap_or(&1) as i64;
                let passes = if crate::mutant::on("l2.timing/loop-passes-2") {
                    2
                } else {
                    LOOP_PASSES
                };
                let mut marks = Vec::new();
                for _ in 0..n.min(passes) {
                    self.run(&st.body)?;
                    marks.push(self.core.end());
                }
                if n > passes {
                    let d = marks[marks.len() - 1] - marks[marks.len() - 2];
                    self.core.shift(d * (n - passes));
                }
                continue;
            }
            if op == "desc" {
                let bind = st.args.iter().find(|x| matches!(x, Term::Assign(..)));
                if let (Some(Term::Assign(lhs, _)), Some(walk)) = (bind, st.kw("walk")) {
                    if let Some(n) = lhs.as_name() {
                        let words = walk_words(walk);
                        self.words_of.insert(n.to_string(), words);
                        self.runs_of
                            .insert(n.to_string(), words / walk_run(walk).max(1));
                    }
                }
                continue;
            }
            if op == "seti" {
                if let [Term::Assign(a, b)] = st.args.as_slice() {
                    if let (Some(n), Some(v)) = (a.as_name(), b.as_int()) {
                        self.scalars.insert(n.to_string(), v);
                    }
                }
            }
            if op == "vunpk" || op == "vpack" || ops::is_math(op) {
                let (words, state) = self.enc.stmt(st, self.state, 1)?;
                self.state = state;
                let text = emit_at(std::slice::from_ref(st), 0, 48);
                self.core.op(text.trim(), words.len() as i64);
            } else if EXPLICIT.contains(&op) || matches!(op, "vcfg" | "setvl" | "word") {
                let (words, state) = self.enc.stmt(st, self.state, 1)?;
                self.state = state;
                self.core.other(if op == "setvl" {
                    self.core.p.cond
                } else {
                    words.len() as i64
                });
            } else if op == "vfill" || op == "vdrain" {
                let ad = st
                    .positional()
                    .first()
                    .and_then(|p| p.as_name())
                    .unwrap_or("");
                let words = *self.words_of.get(ad).unwrap_or(&self.core.p.chunks);
                let runs = self.runs_of.get(ad).copied().unwrap_or(1);
                self.core.dma(
                    if op == "vfill" { "F" } else { "D" },
                    words,
                    runs * self.run,
                );
            } else if op == "vsync" && st.kw("on").is_some() {
                let waiter = st
                    .positional()
                    .first()
                    .and_then(|p| p.as_name())
                    .unwrap_or("");
                let kind = st.kw("on").and_then(|t| t.as_name()).and_then(engine);
                let slack = st.kw("slack").and_then(Term::as_int).unwrap_or(0) as i64;
                match (kind, engine(waiter)) {
                    (Some(k), Some(w)) if k != "fe" => self.core.sync(w, k, slack),
                    _ => self.core.other(self.core.p.cond),
                }
            } else if op == "vbar" {
                self.core.bar();
            } else {
                self.core.other(if COND_OPS.contains(&op) {
                    self.core.p.cond
                } else if op == "seti" {
                    self.core.p.seti
                } else {
                    1
                });
            }
        }
        Ok(())
    }
}
