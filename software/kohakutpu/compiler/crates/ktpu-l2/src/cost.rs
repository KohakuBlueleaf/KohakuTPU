//! A compiled kernel's cycles from its L1: the node priced from the machine facts the
//! timed model (`software/tests/perf/model`) reads, images by the core's timing.

use std::collections::{BTreeMap, HashMap};

use ktpu_l1::lower::imem::Resident;
use ktpu_l1::sched::timing::{cycles_with, Sent};
use ktpu_l1::text::{fmt, Env, Expander, Module, Stmt, Term, Val};
use ktpu_machine::Machine;

use crate::ir::Result;
use crate::target::Target;
use crate::{compile_l1, Program, Search, PASSES};

/// The reverse mutant of the node's priced mechanisms: every send and load a
/// flat 700 cycles, no program, word, wait or move-statement charge.
pub const FLAT_SEND_MUTANT: &str = "l2.cost/flat-send";

/// The cycles `p` compiles to on `m`.
pub fn of_program(p: &Program, m: &Machine) -> Result<i64> {
    let done = compile_l1(p, m, &PASSES, true, &Search::default())?;
    cycles(&done.l1, &p.name, &Target::of(m))
}

/// The cycles of `fn name.l1` of `module`: its slowest node's.
pub fn cycles(module: &Module, name: &str, t: &Target) -> Result<i64> {
    let local = local_cycles(module, name, t)?;
    Ok(t.machine.vc.launch_front as i64 + local.into_iter().max().unwrap_or(0))
}

/// Complete node budgets in top-level node-statement order, excluding launch.
/// An unscoped function has one local budget. All nodes use the same module
/// context as `cycles`, including its cluster-dependent memory timing policy.
pub fn local_cycles(module: &Module, name: &str, t: &Target) -> Result<Vec<i64>> {
    let body = module.body(name, "l1")?;
    let clusters = body.stmts.iter().any(sends_to_clusters);
    let mut timed = module.clone();
    for img in timed.images.values_mut() {
        img.stmts = unguarded(&img.stmts);
    }
    let mut clock = Clock {
        module: &timed,
        t,
        fill: t.mem(clusters),
        times: HashMap::new(),
        programs: HashMap::new(),
        now: 0.0,
        busy: BTreeMap::new(),
        descs: HashMap::new(),
        imem: HashMap::new(),
        ids: HashMap::new(),
    };
    let front = t.machine.vc.launch_front as f64;
    let (reader, macros) = (module.reader(), module.macro_table());
    let expand =
        |stmts: &[Stmt]| Expander::new(&macros, &reader, true).stmts(stmts, &Env::default());
    let nodes: Vec<&Stmt> = body.stmts.iter().filter(|s| s.op == "node").collect();
    if nodes.is_empty() {
        let flat = expand(&body.stmts)?;
        return Ok(vec![
            (front + clock.run(&flat)?).round() as i64 - t.machine.vc.launch_front as i64,
        ]);
    }
    let mut local = Vec::with_capacity(nodes.len());
    for node in nodes {
        clock.now = 0.0;
        clock.busy.clear();
        clock.descs.clear();
        clock.imem.clear();
        let flat = expand(&node.body)?;
        local.push((front + clock.run(&flat)?).round() as i64 - t.machine.vc.launch_front as i64);
    }
    Ok(local)
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

/// `stmts` with each `skipz` replaced by what it guards, as the timed model reads
/// an image: a guard's body runs whenever its count is not zero.
fn unguarded(stmts: &[Stmt]) -> Vec<Stmt> {
    let mut out = Vec::new();
    for st in stmts {
        if st.op == "skipz" {
            out.extend(unguarded(&st.body));
        } else {
            let mut st = st.clone();
            st.body = unguarded(&st.body);
            out.push(st);
        }
    }
    out
}

/// Program words of `stmts`, a loop's body once.
fn program_words(stmts: &[Stmt]) -> f64 {
    stmts.iter().map(|s| 1.0 + program_words(&s.body)).sum()
}

/// A node's program in time: `now` its front end, `busy` when each unit and the
/// mover finish, `descs` the value each (unit, descriptor, field) holds, `imem`
/// each unit's instruction memory as the lowering places programs (`ids` an
/// image at its arguments, the program's key there).
struct Clock<'a> {
    module: &'a Module,
    t: &'a Target,
    fill: (i64, i64),
    times: HashMap<(String, Vec<i128>, Vec<String>), f64>,
    programs: HashMap<(String, Vec<i128>), f64>,
    now: f64,
    busy: BTreeMap<String, f64>,
    descs: HashMap<(String, String, String), String>,
    imem: HashMap<String, Resident>,
    ids: HashMap<(String, Vec<i128>), u64>,
}

impl Clock<'_> {
    fn run(&mut self, stmts: &[Stmt]) -> Result<f64> {
        let t = self.t;
        let vc = &t.machine.vc;
        let flat = crate::mutant::on(FLAT_SEND_MUTANT);
        let word = if flat { 700.0 } else { vc.word_cycles as f64 };
        for st in stmts {
            match st.op.as_str() {
                "load" | "assert_zero" => self.now += word,
                "send" => self.send(st, flat)?,
                "move" => self.moved(st, flat),
                "wait" => {
                    let first = st.positional().first().copied();
                    match first.and_then(Term::as_name) {
                        Some("slot") => {}
                        Some("moves") => {
                            let done = self.busy.get(&moves()).copied().unwrap_or(0.0);
                            self.now = self.now.max(done);
                        }
                        _ => {
                            let unit = first.map(unit_of).unwrap_or_default();
                            let done = self.busy.get(&unit).copied().unwrap_or(0.0);
                            let wait = if flat { 0.0 } else { vc.wait_cycles as f64 };
                            self.now = self.now.max(done) + wait;
                        }
                    }
                }
                "barrier" => self.now = self.now.max(self.last()),
                _ => {}
            }
        }
        Ok(self.now.max(self.last()))
    }

    fn last(&self) -> f64 {
        self.busy.values().copied().fold(0.0, f64::max)
    }

    /// A send: its words, each program its unit does not hold yet, then each run
    /// on the unit once both the unit and the send are done.
    fn send(&mut self, st: &Stmt, flat: bool) -> Result<()> {
        let t = self.t;
        let vc = &t.machine.vc;
        let unit = st
            .positional()
            .first()
            .map(|t| unit_of(t))
            .unwrap_or_default();
        if unit.starts_with("mg") {
            self.now += match flat {
                true => 700.0,
                false => vc.word_cycles as f64 * st.body.len() as f64,
            };
            return Ok(());
        }
        let repeats = st.kw("repeats").and_then(Term::as_int).unwrap_or(1).max(1) as f64;
        let descs: Vec<Stmt> = st.body.iter().filter(|s| s.op == "desc").cloned().collect();
        // A descriptor word writing what its fields hold is never sent.
        let fresh = st
            .body
            .iter()
            .filter(|s| s.op != "desc" || self.described(&unit, s))
            .count() as f64;
        self.now += match flat {
            true => 700.0,
            false => vc.send_cycles as f64 + fresh * repeats * vc.word_cycles as f64,
        };
        for s in st.body.iter().filter(|s| s.op == "run") {
            let Some(Term::Call(name, args)) = s.positional().first().copied() else {
                continue;
            };
            if !self.module.images.contains_key(name) {
                continue;
            }
            let args: Vec<i128> = args.iter().map(|a| a.as_int().unwrap_or(0)).collect();
            let took = self.image(name, &args, &descs)? * repeats;
            if !flat {
                let words = self.program(name, &args)?;
                let next = self.ids.len() as u64;
                let id = *self.ids.entry((name.clone(), args.clone())).or_insert(next);
                let (_, loaded) = self
                    .imem
                    .entry(unit.clone())
                    .or_insert_with(|| Resident::new(vc.imem_words, vc.run_reach))
                    .place(vec![(0, id)], words as u32)?;
                if !loaded {
                    self.now += words * vc.image_word_cycles;
                }
            }
            let free = self.busy.entry(unit.clone()).or_insert(0.0);
            *free = free.max(self.now) + took;
        }
        Ok(())
    }

    /// Records descriptor `d`'s fields on `unit`: whether any changes what it holds.
    fn described(&mut self, unit: &str, d: &Stmt) -> bool {
        let fields = d.kwargs();
        let id = fields[0].0.to_string();
        let mut changed = false;
        for (k, (name, value)) in fields.iter().enumerate() {
            let field = if k == 0 { "base" } else { name };
            let key = (unit.to_string(), id.clone(), field.to_string());
            let value = fmt(value);
            changed |= self.descs.get(&key) != Some(&value);
            self.descs.insert(key, value);
        }
        changed
    }

    /// Image `name` at `args` with the `descs` sent before it, by the core's timing.
    fn image(&mut self, name: &str, args: &[i128], descs: &[Stmt]) -> Result<f64> {
        let fields = descs.iter().map(|d| {
            let kw = d.kwargs().into_iter();
            kw.map(|(k, v)| format!("{k}={}", fmt(v)))
                .collect::<Vec<_>>()
                .join(" ")
        });
        let key = (name.to_string(), args.to_vec(), fields.collect());
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
        )? as f64;
        self.times.insert(key, took);
        Ok(took)
    }

    /// The program words image `name` sends at `args`.
    fn program(&mut self, name: &str, args: &[i128]) -> Result<f64> {
        let key = (name.to_string(), args.to_vec());
        if let Some(w) = self.programs.get(&key) {
            return Ok(*w);
        }
        let img = &self.module.images[name];
        let env: Env = img
            .params
            .iter()
            .zip(args.iter().copied().chain(std::iter::repeat(0)))
            .map(|(p, v)| (p.clone(), Val::Int(v)))
            .collect();
        let (reader, macros) = (self.module.reader(), self.module.macro_table());
        let stmts = Expander::new(&macros, &reader, true).stmts(&img.stmts, &env)?;
        let words = program_words(&stmts);
        self.programs.insert(key, words);
        Ok(words)
    }

    /// A move: the node's statement, then its commands on the mover in turn; one
    /// not posted waits for every unit first and holds the node until it is done.
    fn moved(&mut self, st: &Stmt, flat: bool) {
        let t = self.t;
        let mover = &t.machine.mover;
        let posted = !st.positional().is_empty();
        if !posted {
            self.now = self.now.max(self.last());
        }
        if !flat {
            self.now += mover.move_cycles as f64;
        }
        let took: f64 = st
            .body
            .iter()
            .enumerate()
            .map(|(i, s)| {
                let start = match (i, s.op.as_str(), flat) {
                    (0, _, _) | (_, _, true) => 0.0,
                    (_, "fill", _) => mover.fill_cycles as f64,
                    _ => mover.statement_cycles as f64,
                };
                start + self.command(s, flat)
            })
            .sum();
        let free = self.busy.entry(moves()).or_insert(0.0);
        *free = free.max(self.now) + took;
        if !posted {
            self.now = *free;
        }
    }

    /// One mover command: a run's (an indexed element's) latency each contiguous
    /// run, then its words.
    fn command(&self, st: &Stmt, flat: bool) -> f64 {
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
        let run = run.min(mover.burst_max as i64).max(1);
        let each = match st.op.as_str() {
            "gather" | "scatter" if !flat => mover.element_cycles,
            _ => a_run as f64,
        };
        each * ((words + run - 1) / run) as f64 + a_word as f64 * words as f64
    }
}

/// The mover's entry in a clock's `busy`.
fn moves() -> String {
    unit_of(&Term::name("moves"))
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

#[cfg(test)]
mod tests {
    use super::*;

    /// A two-trip loop sending a0 = x then a0 = `second`: the front end alone.
    fn priced(second: &str) -> i64 {
        let text = format!(
            "target ktpu.v9\n\n\
             fn ab.l1(x: f16[4096] @dram, y: f16[4096] @dram)\n    for k in 0..2\n\
             \x20       send vc[0]\n            desc a0 = x walk=(32, 64)\n\
             \x20       send vc[0]\n            desc a0 = {second} walk=(32, 64)\n\
             \x20   barrier\n"
        );
        let module = ktpu_l1::text::read(&text, "<ab>").unwrap();
        let m = Machine::named("multimesh_v9t8").unwrap();
        cycles(&module, "ab", &Target::of(&m)).unwrap()
    }

    #[test]
    fn a_descriptor_alternating_in_a_loop_is_sent_every_change() {
        // x y x y: four descriptor words; x x x x: one.
        let word = Machine::named("multimesh_v9t8").unwrap().vc.word_cycles as i64;
        assert_eq!(priced("y") - priced("x"), 3 * word);
    }
}
