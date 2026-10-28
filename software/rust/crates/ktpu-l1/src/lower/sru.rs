//! A Streaming Row Unit's `send` block (L1.SR.001): the job's configuration
//! and the unit's instructions.
//!
//! `job` sets every global field (one left out is 0); a `pass` block sets its
//! pass and lays its `op`s into the op store and its `fin`s into the finalise
//! program, each pass after the last. `run` first writes the configuration
//! words that differ from what this program last wrote to the unit, then the
//! RUN.

use std::collections::{BTreeMap, BTreeSet};

use ktpu_isa::unit::sru::config::{self as S, lookup, Job, OpValue, Pool, PoolOp};
use ktpu_isa::unit::sru::{SruInst, LOAD_COLS, LOAD_ROWS, LOAD_TABLE};
use ktpu_isa::Coord;

use super::node::NodeReader;
use crate::error::{Error, Result};
use crate::text::{fmt, Stmt, Term};

const FLAGS: [&str; 4] = ["buf", "use_b", "mask", "r1"];
/// E8M15-valued job fields, written as numbers.
const E8_JOB: [&str; 5] = ["mval", "k0", "k1", "k2", "k3"];
const OUT: [(&str, i64); 4] = [("none", 0), ("f16", 1), ("mx7a", 2), ("mx7b", 3)];
/// Finalise operand order per opcode, as written after the arrow.
const FIN_ARGS: [(&str, &[&str]); 9] = [
    ("mov", &["A"]),
    ("add", &["A", "C"]),
    ("sub", &["A", "C"]),
    ("mul", &["A", "B"]),
    ("fma", &["A", "B", "C"]),
    ("max", &["A", "B"]),
    ("min", &["A", "B"]),
    ("rcp", &["A"]),
    ("rsqrt", &["A"]),
];
/// `op fn` modes: the FIX and MANT bits.
const FN_MODE: [(&str, (i64, i64)); 4] = [
    ("exp", (0, 0)),
    ("fix", (1, 0)),
    ("mant", (0, 1)),
    ("half", (1, 1)),
];
const FIN_DST: [(&str, i64); 4] = [("R0", 0), ("R1", 1), ("R2", 2), ("T", 3)];
const LOADS: [(&str, u32); 3] = [
    ("table", LOAD_TABLE),
    ("cols", LOAD_COLS),
    ("rows", LOAD_ROWS),
];

/// Reads one unit's `send` block. `held` is the configuration the unit holds
/// from earlier sends of this program.
pub struct RowReader<'n, 'a> {
    node: &'n NodeReader<'a>,
    pub held: BTreeMap<u32, u32>,
    pool: Pool,
    npass: usize,
    op_depth: usize,
    fin_depth: usize,
    job: Option<BTreeMap<String, i64>>,
    passes: Vec<(usize, BTreeMap<String, i64>)>,
    ops: Vec<PoolOp>,
    fin: Vec<BTreeMap<String, i64>>,
}

fn job_field(name: &str) -> Option<(&'static str, u32)> {
    S::G.iter()
        .find(|(k, _)| k.to_lowercase() == name)
        .map(|(k, (_, _, w))| (*k, *w))
}

impl<'n, 'a> RowReader<'n, 'a> {
    pub fn new(node: &'n NodeReader<'a>, held: BTreeMap<u32, u32>) -> Result<RowReader<'n, 'a>> {
        let m = node.target.machine;
        let pool = Pool::of(m)?;
        let sr = m
            .sr_facts()
            .ok_or_else(|| Error::new("L1.SR.001", format!("{} has no SR units", m.name)))?;
        Ok(RowReader {
            node,
            held,
            pool,
            npass: sr.npass as usize,
            op_depth: sr.op_depth as usize,
            fin_depth: sr.fin_depth as usize,
            job: None,
            passes: Vec::new(),
            ops: Vec::new(),
            fin: Vec::new(),
        })
    }

    fn fail(&self, st: &Stmt, msg: impl Into<String>) -> Error {
        self.node.fail(st, "L1.SR.001", msg)
    }

    /// Each statement's instructions, in order.
    pub fn ops_of(&mut self, body: &[Stmt]) -> Result<Vec<(Stmt, SruInst)>> {
        let mut out = Vec::new();
        for st in body {
            if st.target.is_some() {
                return Err(self.fail(st, format!("`{}` binds no name", st.op)));
            }
            match st.op.as_str() {
                "job" => self.read_job(st)?,
                "pass" => self.read_pass(st)?,
                "load" => out.push((st.clone(), self.load(st)?)),
                "run" => {
                    let cfg = self.config(st)?;
                    if let SruInst::Config { words } = &cfg {
                        if !words.is_empty() {
                            out.push((st.clone(), cfg.clone()));
                        }
                    }
                    out.push((st.clone(), self.run(st)?));
                }
                "store" => out.push((st.clone(), self.store(st)?)),
                "post" => {
                    let pos = st.positional();
                    let keys: BTreeSet<&str> = st.kwargs().iter().map(|(k, _)| *k).collect();
                    if !pos.is_empty() || keys != BTreeSet::from(["n", "to"]) {
                        return Err(self.fail(st, "post n=CREDITS to=UNIT"));
                    }
                    let dst = self.node.unit(st, st.kw("to").unwrap())?;
                    let n = self.c(st, st.kw("n").unwrap())? as u32;
                    out.push((
                        st.clone(),
                        SruInst::Post {
                            dst: Coord(dst.0, dst.1),
                            n,
                        },
                    ));
                }
                other => {
                    return Err(self.fail(
                        st,
                        format!(
                            "a row unit's send holds job pass load run store post, not '{other}'"
                        ),
                    ))
                }
            }
            if !st.body.is_empty() && st.op != "pass" {
                return Err(self.fail(st, format!("`{}` takes no block", st.op)));
            }
        }
        Ok(out)
    }

    // ------------------------------------------------------------ values
    fn c(&self, st: &Stmt, t: &Term) -> Result<i128> {
        self.node.const_(st, t)
    }

    fn kc(&self, st: &Stmt, key: &str, default: i128) -> Result<i128> {
        match st.kw(key) {
            Some(t) => self.c(st, t),
            None => Ok(default),
        }
    }

    /// A number: an integer, a float (`inf` too), or one negated.
    fn num(&self, st: &Stmt, t: &Term) -> Result<f64> {
        match t {
            Term::Neg(x) => Ok(-self.num(st, x)?),
            Term::Int(v, _) => Ok(*v as f64),
            Term::Float(v) => Ok(*v),
            _ => Err(self.fail(st, format!("wanted a number, got {}", fmt(t)))),
        }
    }

    // --------------------------------------------------------------- job
    fn read_job(&mut self, st: &Stmt) -> Result<()> {
        let mut g: BTreeMap<String, i64> = S::G.iter().map(|(k, _)| (k.to_string(), 0)).collect();
        for p in st.positional() {
            match p.as_name() {
                Some(n) if FLAGS.contains(&n) => {
                    g.insert(job_field(n).unwrap().0.to_string(), 1);
                }
                _ => {
                    let mut fl = FLAGS.to_vec();
                    fl.sort();
                    return Err(self.fail(st, format!("job flags are {fl:?}, got {}", fmt(p))));
                }
            }
        }
        for (k, v) in st.kwargs() {
            let Some((name, width)) = job_field(k) else {
                let mut all: Vec<String> = S::G.iter().map(|(k, _)| k.to_lowercase()).collect();
                all.sort();
                return Err(self.fail(st, format!("no job field '{k}'; they are {all:?}")));
            };
            if k == "out" && v.as_name().is_some() {
                let n = v.as_name().unwrap();
                let Some((_, code)) = OUT.iter().find(|(o, _)| *o == n) else {
                    return Err(self.fail(st, "out= none | f16 | mx7a | mx7b"));
                };
                g.insert("OUT".into(), *code);
            } else if E8_JOB.contains(&k) {
                g.insert(name.into(), S::e8(self.num(st, v)?) as i64);
            } else {
                let x = match v {
                    Term::Neg(x) => -self.c(st, x)?,
                    v => self.c(st, v)?,
                };
                g.insert(name.into(), (x & ((1i128 << width) - 1)) as i64);
            }
        }
        self.job = Some(g);
        self.passes.clear();
        self.ops.clear();
        self.fin.clear();
        Ok(())
    }

    fn read_pass(&mut self, st: &Stmt) -> Result<()> {
        if self.job.is_none() {
            return Err(self.fail(st, "a pass follows its job"));
        }
        let pos = st.positional();
        let Some(Term::Int(p, _)) = pos.first() else {
            return Err(self.fail(st, "pass P [r2] [emit] (a block of op and fin)"));
        };
        let p = *p;
        let flags: BTreeSet<&str> = pos[1..].iter().filter_map(|x| x.as_name()).collect();
        if pos.len() - 1 != flags.len() || flags.iter().any(|f| *f != "r2" && *f != "emit") {
            return Err(self.fail(st, "pass P [r2] [emit]"));
        }
        if !(0..self.npass as i128).contains(&p) || self.passes.iter().any(|(q, _)| *q as i128 == p)
        {
            return Err(self.fail(
                st,
                format!("pass {p}: one of 0..{}, once each", self.npass - 1),
            ));
        }
        let ops: Vec<&Stmt> = st.body.iter().filter(|s| s.op == "op").collect();
        let fins: Vec<&Stmt> = st.body.iter().filter(|s| s.op == "fin").collect();
        if ops.len() + fins.len() != st.body.len() {
            return Err(self.fail(st, "a pass block holds op and fin"));
        }
        let mut d = BTreeMap::new();
        d.insert("NOPS".to_string(), ops.len() as i64);
        d.insert(
            "OPBASE".into(),
            if ops.is_empty() {
                0
            } else {
                self.ops.len() as i64
            },
        );
        d.insert("R2EN".into(), flags.contains("r2") as i64);
        d.insert("EMIT".into(), flags.contains("emit") as i64);
        d.insert("FINLEN".into(), fins.len() as i64);
        d.insert(
            "FINBASE".into(),
            if fins.is_empty() {
                0
            } else {
                self.fin.len() as i64
            },
        );
        for s in &ops {
            let op = self.op(s, ops.len())?;
            self.ops.push(op);
        }
        for s in &fins {
            let f = self.fin_op(s)?;
            self.fin.push(f);
        }
        if self.ops.len() > self.op_depth || self.fin.len() > self.fin_depth {
            return Err(self.fail(
                st,
                format!(
                    "the op store holds {}, the finalise {}",
                    self.op_depth, self.fin_depth
                ),
            ));
        }
        self.passes.push((p as usize, d));
        Ok(())
    }

    fn op(&self, st: &Stmt, n: usize) -> Result<PoolOp> {
        let pos = st.positional();
        let kind = pos.first().and_then(|p| p.as_name());
        let flags: BTreeSet<&str> = pos.iter().skip(1).filter_map(|x| x.as_name()).collect();
        let keys: BTreeSet<&str> = st.kwargs().iter().map(|(k, _)| *k).collect();
        let mut f = PoolOp::new();
        match kind {
            Some("fma") => {
                if keys != BTreeSet::from(["a", "b", "c"]) || !flags.is_empty() {
                    return Err(self.fail(st, "op fma a=SRC b=SRC c=SRC"));
                }
                f.insert("KIND".into(), OpValue::Int(0));
                self.src(st, &mut f, "A", st.kw("a").unwrap(), n)?;
                self.src(st, &mut f, "B", st.kw("b").unwrap(), n)?;
                self.src(st, &mut f, "C", st.kw("c").unwrap(), n)?;
                Ok(f)
            }
            Some("fn") => {
                let modes: Vec<&str> = flags
                    .iter()
                    .copied()
                    .filter(|x| FN_MODE.iter().any(|(m, _)| m == x))
                    .collect();
                let allowed = ["a", "k", "cs", "pre"];
                let bad = !keys.contains("a")
                    || !keys.contains("k")
                    || keys.iter().any(|k| !allowed.contains(k))
                    || flags
                        .iter()
                        .any(|x| *x != "neg" && !FN_MODE.iter().any(|(m, _)| m == x))
                    || modes.len() > 1
                    || (flags.contains("neg")
                        && !modes.iter().any(|m| *m == "mant" || *m == "half"));
                if bad {
                    return Err(self.fail(
                        st,
                        "op fn a=SRC k=SRC [fix | mant [neg] | half [neg]] [cs=N] [pre=N]",
                    ));
                }
                let mode = modes.first().copied().unwrap_or("exp");
                let (fix, mant) = FN_MODE.iter().find(|(m, _)| *m == mode).unwrap().1;
                f.insert("KIND".into(), OpValue::Int(1));
                f.insert("FIX".into(), OpValue::Int(fix));
                f.insert("MANT".into(), OpValue::Int(mant));
                f.insert("NEG".into(), OpValue::Int(flags.contains("neg") as i64));
                f.insert("CS".into(), OpValue::Int(self.kc(st, "cs", 0)? as i64));
                let zero = Term::int(0);
                f.insert(
                    "PRE".into(),
                    OpValue::Int(self.signed(st, st.kw("pre").unwrap_or(&zero), 6)?),
                );
                self.src(st, &mut f, "A", st.kw("a").unwrap(), n)?;
                self.src(st, &mut f, "B", st.kw("k").unwrap(), n)?;
                Ok(f)
            }
            _ => Err(self.fail(st, "op fma ... | op fn ...")),
        }
    }

    fn signed(&self, st: &Stmt, t: &Term, bits: u32) -> Result<i64> {
        let v = match t {
            Term::Neg(x) => -self.c(st, x)?,
            t => self.c(st, t)?,
        };
        let half = 1i128 << (bits - 1);
        if !(-half..half).contains(&v) {
            return Err(self.fail(st, format!("{v} does not fit {bits} signed bits")));
        }
        Ok((v & ((1i128 << bits) - 1)) as i64)
    }

    /// Operand `slot` (A, B, C): X B O (A), a number, R0..R2, C0 C1, B, X
    /// (B, C), or H<k> (op k's result); A and C may be negated.
    fn src(&self, st: &Stmt, f: &mut PoolOp, slot: &str, t: &Term, n: usize) -> Result<()> {
        let mut t = t;
        if let Term::Neg(x) = t {
            if !matches!(x.as_ref(), Term::Int(..) | Term::Float(_)) {
                if slot == "B" {
                    return Err(self.fail(st, "b is not negated: negate a or c"));
                }
                f.insert(format!("N{slot}"), OpValue::Int(1));
                t = x;
            }
        }
        let name = t.as_name();
        if let Some(k) = name
            .filter(|n| {
                n.len() > 1 && n.starts_with('H') && n[1..].chars().all(|c| c.is_ascii_digit())
            })
            .map(|n| n[1..].parse::<usize>().unwrap_or(usize::MAX))
        {
            if k >= n {
                return Err(self.fail(st, format!("{}: the program has {n} ops", name.unwrap())));
            }
            f.insert(slot.into(), OpValue::Tap("H".into(), k));
        } else if slot == "A" {
            match name {
                Some(x @ ("X" | "B" | "O")) => {
                    f.insert("A".into(), OpValue::Name(x.into()));
                }
                _ => return Err(self.fail(st, format!("a is X, B, O or H<k>, got {}", fmt(t)))),
            }
        } else if let Some(x @ ("X" | "B")) = name {
            f.insert(slot.into(), OpValue::Name(x.into()));
        } else if let Some(x @ ("R0" | "R1" | "R2")) = name {
            f.insert(slot.into(), OpValue::Name("R".into()));
            f.insert(format!("R{slot}"), OpValue::Int(x[1..].parse().unwrap()));
        } else if let Some(x @ ("C0" | "C1")) = name {
            f.insert(slot.into(), OpValue::Name("C".into()));
            f.insert(format!("C{slot}"), OpValue::Int(x[1..].parse().unwrap()));
        } else {
            f.insert(slot.into(), OpValue::Name("K".into()));
            f.insert(
                format!("K{slot}"),
                OpValue::Int(S::e8(self.num(st, t)?) as i64),
            );
        }
        Ok(())
    }

    fn fin_op(&self, st: &Stmt) -> Result<BTreeMap<String, i64>> {
        let pos = st.positional();
        let form = "fin OP DST <- SRC[, SRC...]";
        if pos.len() < 4 || !pos[2].is_arrow("<-") || pos[0].as_name().is_none() {
            return Err(self.fail(st, form));
        }
        let op = pos[0].as_name().unwrap();
        let Some((_, slots)) = FIN_ARGS.iter().find(|(o, _)| *o == op) else {
            let mut all: Vec<&str> = FIN_ARGS.iter().map(|(o, _)| *o).collect();
            all.sort();
            return Err(self.fail(st, format!("finalise ops are {all:?}")));
        };
        let dst = pos[1].as_name().and_then(|d| lookup(&FIN_DST, d));
        let Some(dst) = dst else {
            return Err(self.fail(st, "a finalise writes R0 R1 R2 T"));
        };
        let args = &pos[3..];
        if args.len() != slots.len() {
            return Err(self.fail(st, format!("fin {op} takes {} sources", slots.len())));
        }
        let mut f: BTreeMap<String, i64> = BTreeMap::new();
        f.insert("OP".into(), lookup(S::FOP, op).unwrap());
        f.insert("DST".into(), dst);
        for k in ["A", "B", "C"] {
            f.insert(k.into(), 0);
        }
        for (slot, a) in slots.iter().zip(args) {
            let code = a.as_name().and_then(|k| lookup(S::SRC, k));
            let Some(code) = code else {
                let names: Vec<&str> = S::SRC.iter().map(|(n, _)| *n).collect();
                return Err(self.fail(st, format!("finalise sources are {}", names.join(" "))));
            };
            f.insert(slot.to_string(), code);
        }
        Ok(f)
    }

    // ------------------------------------------------------- instructions
    fn words(&self, st: &Stmt) -> Result<BTreeMap<u32, u32>> {
        let Some(job) = &self.job else {
            return Err(self.fail(st, "a run follows its job"));
        };
        let zero: BTreeMap<String, i64> = S::P.iter().map(|(k, _)| (k.to_string(), 0)).collect();
        let mut passes = vec![zero; self.npass];
        for (p, d) in &self.passes {
            passes[*p] = d.clone();
        }
        let mut g = job.clone();
        let top = self.passes.iter().map(|(p, _)| *p).max().unwrap_or(0) as i64;
        if g["BUF"] == 0 && top != 0 {
            return Err(self.fail(st, "passes past 0 replay the buffer: job buf"));
        }
        if g["BUF"] != 0 && g["NPASS"] == 0 {
            g.insert("NPASS".into(), top);
        }
        let job = Job {
            g,
            passes,
            ops: self.ops.clone(),
            fin: self.fin.clone(),
        };
        S::words(&job, &self.pool).map_err(|e| {
            self.fail(
                st,
                format!("the pool cannot run this program: {}", e.message),
            )
        })
    }

    fn config(&mut self, st: &Stmt) -> Result<SruInst> {
        let want = self.words(st)?;
        let diff: Vec<(u32, u32)> = want
            .iter()
            .filter(|(a, v)| self.held.get(a) != Some(v))
            .map(|(a, v)| (*a, *v))
            .collect();
        self.held.extend(want);
        Ok(SruInst::Config { words: diff })
    }

    /// Out words a sub-row: f16 one per word, MXFP7 one per two.
    fn out_words(&self) -> i128 {
        let job = self.job.as_ref().unwrap();
        let (nw, out) = (job["NW"] as i128, job["OUT"]);
        if out <= 1 {
            nw
        } else {
            nw / 2
        }
    }

    fn run(&self, st: &Stmt) -> Result<SruInst> {
        let pos = st.positional();
        let flags: BTreeSet<&str> = pos.iter().filter_map(|x| x.as_name()).collect();
        let sorted_flags: Vec<&str> = flags.iter().copied().collect();
        let as_flags: Vec<Option<&str>> = pos.iter().map(|p| p.as_name()).collect();
        let same = as_flags.len() == sorted_flags.len()
            && as_flags
                .iter()
                .zip(&sorted_flags)
                .all(|(a, b)| *a == Some(*b));
        if !same
            || flags.iter().any(|f| *f != "count")
            || st.kw("a").is_none()
            || st.kw("out").is_none()
        {
            return Err(self.fail(
                st,
                "run a=ADDR|recv [from=UNIT] [astride=W] [b=ADDR] [bstride=W] out=ADDR|UNIT [ostride=W] [buf=B] [at=GRANULE] [count]",
            ));
        }
        let job = self.job.as_ref().unwrap();
        let nw = job["NW"] as i128;
        let a = match st.kw("a").unwrap() {
            t if t.as_name() == Some("recv") => None,
            t => Some(self.c(st, t)? as u64),
        };
        if a.is_none() && st.kw("from").is_none() {
            return Err(self.fail(st, "a=recv names its producer: from=UNIT"));
        }
        let out_t = st.kw("out").unwrap();
        let to = match out_t {
            Term::View(..) => Some(self.node.unit(st, out_t)?),
            _ => None,
        };
        let out = if to.is_some() {
            0
        } else {
            self.c(st, out_t)? as u64
        };
        let src = match st.kw("from") {
            Some(t) => self.node.unit(st, t)?,
            None => (0, 0),
        };
        Ok(SruInst::Run {
            a,
            astride: self.kc(st, "astride", nw)? as u32,
            out,
            ostride: self.kc(st, "ostride", self.out_words())? as u32,
            b: self.kc(st, "b", 0)? as u64,
            bstride: self.kc(st, "bstride", nw)? as u32,
            src: Coord(src.0, src.1),
            to: to.map(|c| Coord(c.0, c.1)),
            buf: self.kc(st, "buf", 0)? as u32,
            at: self.kc(st, "at", 0)? as u32,
            count: flags.contains("count"),
            words: (nw * job["NSR"] as i128) as u32,
        })
    }

    fn load(&self, st: &Stmt) -> Result<SruInst> {
        let pos = st.positional();
        let what = pos
            .first()
            .and_then(|p| p.as_name())
            .and_then(|n| lookup(&LOADS, n));
        let ok = pos.len() == 3 && what.is_some() && pos[1].is_arrow("<-") && st.kw("n").is_some();
        if !ok {
            return Err(self.fail(st, "load table|cols|rows <- ADDR n=WORDS [at=FIRST]"));
        }
        Ok(SruInst::Load {
            what: what.unwrap(),
            addr: self.c(st, pos[2])? as u64,
            words: self.c(st, st.kw("n").unwrap())? as u32,
            first: self.kc(st, "at", 0)? as u32,
        })
    }

    fn store(&self, st: &Stmt) -> Result<SruInst> {
        let pos = st.positional();
        let ok = pos.len() == 3
            && pos[0].as_name() == Some("rows")
            && pos[1].is_arrow("->")
            && st.kw("n").is_some();
        if !ok {
            return Err(self.fail(st, "store rows -> ADDR n=ROWS [at=FIRST]"));
        }
        Ok(SruInst::Store {
            addr: self.c(st, pos[2])? as u64,
            rows: self.c(st, st.kw("n").unwrap())? as u32,
            first: self.kc(st, "at", 0)? as u32,
        })
    }
}
