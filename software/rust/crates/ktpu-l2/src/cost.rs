//! A compiled kernel's cycles from its L1: images by the core's timing, moves
//! by words and runs, joined at the waits; cluster programs take no time here.

use std::collections::{BTreeMap, HashMap};

use ktpu_l1::sched::timing::{cycles_with, Sent};
use ktpu_l1::text::{Module, Stmt, Term};
use ktpu_machine::Machine;

use crate::ir::Result;
use crate::target::Target;
use crate::{compile_l1, Program, Search, PASSES};

/// The cycles `p` compiles to on `m`.
pub fn of_program(p: &Program, m: &Machine) -> Result<i64> {
    let done = compile_l1(p, m, &PASSES, true, &Search::default())?;
    cycles(&done.l1, &p.name, &Target::of(m))
}

/// The cycles of `fn name.l1` of `module`: its slowest node's.
pub fn cycles(module: &Module, name: &str, t: &Target) -> Result<i64> {
    let body = module.body(name, "l1")?;
    let clusters = body.stmts.iter().any(sends_to_clusters);
    let mut clock = Clock {
        module,
        t,
        fill: t.mem(clusters),
        times: HashMap::new(),
        now: 0,
        busy: BTreeMap::new(),
    };
    let front = t.machine.vc.launch_front as i64;
    let nodes: Vec<&Stmt> = body.stmts.iter().filter(|s| s.op == "node").collect();
    if nodes.is_empty() {
        return Ok(front + clock.run(&body.stmts)?);
    }
    let mut slowest = 0;
    for node in nodes {
        clock.now = 0;
        clock.busy.clear();
        slowest = slowest.max(clock.run(&node.body)?);
    }
    Ok(front + slowest)
}

fn sends_to_clusters(st: &Stmt) -> bool {
    let unit = st.positional().first().map(|t| unit_of(t));
    (st.op == "send" && unit.is_some_and(|u| u.starts_with("mg")))
        || st.body.iter().any(sends_to_clusters)
}

fn unit_of(t: &Term) -> String {
    match t {
        Term::View(n, args) => format!("{n}{args:?}"),
        other => format!("{other:?}"),
    }
}

/// A node's program in time: `now` where its front end is, `busy` when each
/// core it sent work to finishes.
struct Clock<'a> {
    module: &'a Module,
    t: &'a Target,
    fill: (i64, i64),
    times: HashMap<(String, Vec<i128>, Vec<String>), i64>,
    now: i64,
    busy: BTreeMap<String, i64>,
}

impl Clock<'_> {
    fn run(&mut self, stmts: &[Stmt]) -> Result<i64> {
        for st in stmts {
            match st.op.as_str() {
                "load" | "assert_zero" => self.now += self.t.machine.vc.step_cycles as i64,
                "send" => {
                    let unit = st.positional().first().map(|t| unit_of(t));
                    let unit = unit.unwrap_or_default();
                    self.now += self.t.machine.vc.step_cycles as i64;
                    let descs: Vec<Stmt> =
                        st.body.iter().filter(|s| s.op == "desc").cloned().collect();
                    for s in st.body.iter().filter(|s| s.op == "run") {
                        let took = self.image(s, &descs)?;
                        let free = self.busy.entry(unit.clone()).or_insert(0);
                        *free = (*free).max(self.now) + took;
                    }
                }
                "move" => {
                    let took = st.body.iter().map(|s| self.moved(s)).sum::<i64>();
                    if st.positional().is_empty() {
                        self.now += took;
                    } else {
                        let free = self.busy.entry(unit_of(&Term::name("moves"))).or_insert(0);
                        *free = (*free).max(self.now) + took;
                    }
                }
                "wait" => {
                    let unit = st.positional().first().map(|t| unit_of(t));
                    let done = unit.and_then(|u| self.busy.get(&u).copied());
                    self.now = self.now.max(done.unwrap_or(0));
                }
                "barrier" => self.now = self.now.max(self.last()),
                _ => {}
            }
        }
        Ok(self.now.max(self.last()))
    }

    fn last(&self) -> i64 {
        self.busy.values().copied().max().unwrap_or(0)
    }

    /// `run NAME(args)` with the `descs` sent before it: its integer arguments
    /// as given, an address at 0.
    fn image(&mut self, run: &Stmt, descs: &[Stmt]) -> Result<i64> {
        let Some(Term::Call(name, args)) = run.positional().first().copied() else {
            return Ok(0);
        };
        if !self.module.images.contains_key(name) {
            return Ok(0);
        }
        let args: Vec<i128> = args.iter().map(|a| a.as_int().unwrap_or(0)).collect();
        let walks = descs.iter().map(|d| format!("{:?}", d.kw("walk")));
        let key = (name.clone(), args, walks.collect());
        if let Some(c) = self.times.get(&key) {
            return Ok(*c);
        }
        let sent = Sent {
            args: &key.1,
            descs,
            run: self.t.machine.mover.copy_cost.0 as i64,
        };
        let took = cycles_with(
            self.module,
            name,
            &sent,
            self.t.vregs,
            self.t.vcopies,
            self.fill,
            &self.t.machine,
        )?;
        self.times.insert(key, took);
        Ok(took)
    }

    /// One mover statement: a run's latency each contiguous run, then its words.
    fn moved(&self, st: &Stmt) -> i64 {
        let mover = &self.t.machine.mover;
        let (a_run, a_word) = mover.copy_cost;
        let walks: Vec<&Vec<Term>> = st
            .positional()
            .into_iter()
            .filter_map(|t| match t {
                Term::Call(name, levels) if name == "w" => Some(levels),
                _ => None,
            })
            .collect();
        let rows = st.kw("n").and_then(Term::as_int).map(|n| n as i64);
        let row = st.kw("words").and_then(Term::as_int).map(|w| w as i64);
        let (words, run) = match (rows, row) {
            (Some(rows), Some(row)) => (rows * row, row),
            _ => {
                let words = walks.first().map_or(rows.unwrap_or(1), |w| walk_words(w));
                let run = walks.iter().map(|w| walk_run(w, self.t.word_bytes)).min();
                (words, run.unwrap_or(words))
            }
        };
        let run = run.min(*mover.burst_max.runtime() as i64).max(1);
        a_run as i64 * ((words + run - 1) / run) + a_word as i64 * words
    }
}

/// `(COUNT, STRIDE)` of each level of a walk, outermost first.
fn levels(walk: &[Term]) -> impl Iterator<Item = (i64, i64)> + '_ {
    walk.iter().filter_map(|t| match t {
        Term::Tuple(level) => Some((signed(level.first()?)?, signed(level.get(1)?)?)),
        _ => None,
    })
}

fn signed(t: &Term) -> Option<i64> {
    match t {
        Term::Neg(v) => Some(-(v.as_int()? as i64)),
        _ => Some(t.as_int()? as i64),
    }
}

fn walk_words(walk: &[Term]) -> i64 {
    levels(walk).map(|(count, _)| count).product()
}

/// The words of a walk's contiguous ascending runs.
fn walk_run(walk: &[Term], word: i64) -> i64 {
    match levels(walk).last() {
        Some((count, stride)) if stride == word => count,
        _ => 1,
    }
}
