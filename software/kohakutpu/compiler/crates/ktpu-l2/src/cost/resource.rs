//! Emitted L1 resource timing shared by production selection and perf reports.
//! Engine dependencies and per-physical-sysnode buses determine completion.
//! Planning uses the versioned card anchor profile; reports may supply another
//! calibration through the same parameter and task-graph interfaces.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use ktpu_l1::lower::imem::Resident;
use ktpu_l1::sched::timing::{cycles_expanded, parse, symbolic, walk_words, Params as Core, Sent};
use ktpu_l1::text::{fmt, Env, Expander, Module, Stmt, Term, Val};
use ktpu_machine::Machine;
use serde_json::{json, Value};

use crate::target::Target;

const WORD: f64 = 32.0;
const ENTRY: f64 = 128.0;

/// What the program's text cannot say, each pinned by an anchor.
#[derive(Clone, Debug)]
pub struct Params {
    pub node_start: f64,
    pub node_floor: f64,
    pub node_send: f64,
    pub node_word: f64,
    pub node_move: f64,
    pub node_image: f64,
    pub node_wait: f64,
    pub mover_run: f64,
    pub mover_word: f64,
    pub mover_statement: f64,
    pub mover_fill: f64,
    pub mover_element: f64,
    pub vc_fe_sync: f64,
    pub vc_pack_op: f64,
    pub vc_unpack_op: f64,
    pub vc_fill_trim: f64,
    pub vc_memory_word: f64,
    pub ring: f64,
    pub read_bus: f64,
    pub write_bus: f64,
    pub mg_fill_entry: f64,
    pub mg_start: f64,
    pub mg_sweep: f64,
    /// The sweep at the clusters' peak (paired blocks), whatever the tier: the
    /// matrix floor is the hardware's, not a calibration's.
    pub mg_peak_sweep: f64,
    pub mg_drain_word: f64,
}

impl Params {
    /// Lower bound on this profile's price for mandatory matrix work on one
    /// physical sysnode. Counts are modeled fill entries, GEMM commands and
    /// swept subtiles (including NK). Shared fills retain the model's per-command
    /// charge. This is a price bound, not a claim of read-once hardware traffic.
    pub fn matrix_floor(
        &self,
        entries: u64,
        issues: u64,
        steps: u64,
        clusters: u32,
    ) -> Option<f64> {
        if clusters == 0
            || [entries, issues, steps].iter().any(|&n| n > (1u64 << 53))
            || !self.read_bus.is_finite()
            || self.read_bus <= 0.0
            || [
                self.node_start,
                self.node_floor,
                self.node_word,
                self.mg_start,
                self.mg_sweep,
                self.mg_fill_entry,
            ]
            .iter()
            .any(|x| !x.is_finite() || *x < 0.0)
        {
            return None;
        }
        let units = f64::from(clusters);
        let reads = entries as f64 * ENTRY / self.read_bus;
        let fills = entries as f64 * self.mg_fill_entry / units;
        let sweeps = (issues as f64 * self.mg_start + steps as f64 * self.mg_sweep) / units;
        let commands = issues as f64 * self.node_word;
        Some(
            self.node_floor
                .max(self.node_start + reads.max(fills).max(sweeps).max(commands)),
        )
    }

    /// Card anchor coefficients, with machine-owned dispatch and unit facts.
    /// The immutable profile is bundled with the compiler, not read from tests.
    pub fn planning(m: &Machine) -> Params {
        static PROFILE: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
        let profile = PROFILE.get_or_init(|| {
            serde_json::from_str(include_str!("planning.json")).expect("bundled timing profile")
        });
        Self::of(profile, "card", m)
    }

    /// `tier` names the column read, `value` where a parameter holds no other; a
    /// parameter naming a machine `fact` (`ktpu_l2::cost`'s too) reads it from `m`.
    pub fn of(v: &Value, tier: &str, m: &Machine) -> Params {
        let at = |k: &str| {
            v[k][tier]
                .as_f64()
                .or_else(|| v[k]["value"].as_f64())
                .unwrap_or_else(|| panic!("params.json holds no {k}"))
        };
        let at_value = |k: &str| {
            v[k]["value"]
                .as_f64()
                .unwrap_or_else(|| panic!("params.json holds no {k} value"))
        };
        let fact = |k: &str, x: f64| v[k][tier].as_f64().unwrap_or(x);
        Params {
            node_start: at("node_start"),
            node_floor: at("node_floor"),
            node_send: fact("node_send", m.vc.send_cycles as f64),
            node_word: fact("node_word", m.vc.word_cycles as f64),
            node_move: fact("node_move", m.mover.move_cycles as f64),
            node_image: fact("node_image", m.vc.image_word_cycles),
            node_wait: fact("node_wait", m.vc.wait_cycles as f64),
            mover_run: at("mover_run"),
            mover_word: at("mover_word"),
            mover_statement: fact("mover_statement", m.mover.statement_cycles as f64),
            mover_fill: fact("mover_fill", m.mover.fill_cycles as f64),
            mover_element: fact("mover_element", m.mover.element_cycles),
            vc_fe_sync: at("vc_fe_sync"),
            vc_pack_op: at("vc_pack_op"),
            vc_unpack_op: at("vc_unpack_op"),
            vc_fill_trim: at("vc_fill_trim"),
            vc_memory_word: at("vc_memory_word"),
            ring: at("ring"),
            read_bus: at("read_bus"),
            write_bus: at("write_bus"),
            mg_fill_entry: at("mg_fill_entry"),
            mg_start: at("mg_start"),
            mg_sweep: at("mg_sweep"),
            mg_peak_sweep: at_value("mg_sweep"),
            mg_drain_word: at("mg_drain_word"),
        }
    }
}

struct Task {
    engine: usize,
    label: &'static str,
    cycles: f64,
    /// Its cycles while no cluster of its node streams (a vector core's
    /// fills alone, `Target::mem(false)`); `cycles` beside streaming clusters.
    alone: f64,
    read: f64,
    write: f64,
    node: usize,
    deps: Vec<usize>,
}

/// What a kernel's text does, unit-level counts for the floors.
#[derive(Clone, Copy, Debug, Default)]
pub struct Work {
    pub macs: f64,
    pub math_beats: f64,
    pub unpack_words: f64,
    pub pack_words: f64,
    pub read_bytes: f64,
    pub write_bytes: f64,
    pub mover_bytes: f64,
    pub mover_statements: f64,
    /// Words and statements of moves whose write side steps one-word runs:
    /// the shape silicon flaw F-001 corrupts.
    pub lone_words: f64,
    pub lone_moves: f64,
    pub image_words: f64,
    pub words: f64,
    pub fe_syncs: f64,
    pub pack_ops: f64,
    pub unpack_ops: f64,
}

#[derive(Default)]
struct Graph {
    tasks: Vec<Task>,
    engines: Vec<String>,
    by_name: HashMap<String, usize>,
    last: HashMap<usize, usize>,
    work: Work,
    node_work: BTreeMap<usize, Work>,
    arrivals: HashMap<usize, ((usize, String), i64)>,
    receives: HashMap<usize, ((usize, String), i64)>,
    rings: HashMap<usize, ((usize, i64), i64)>,
    slots: HashMap<usize, ((usize, i64), i64, bool)>,
    unmodelled: BTreeMap<String, usize>,
}

impl Graph {
    fn engine(&mut self, name: String) -> usize {
        if let Some(&e) = self.by_name.get(&name) {
            return e;
        }
        let e = self.engines.len();
        self.engines.push(name.clone());
        self.by_name.insert(name, e);
        e
    }

    #[allow(clippy::too_many_arguments)]
    fn push(
        &mut self,
        engine: usize,
        label: &'static str,
        cycles: f64,
        read: f64,
        write: f64,
        node: usize,
        mut deps: Vec<usize>,
    ) -> usize {
        if let Some(&t) = self.last.get(&engine) {
            deps.push(t);
        }
        let id = self.tasks.len();
        self.tasks.push(Task {
            engine,
            label,
            cycles,
            alone: cycles,
            read,
            write,
            node,
            deps,
        });
        self.last.insert(engine, id);
        id
    }

    fn lasts(&self, prefix: &str) -> Vec<usize> {
        self.by_name
            .iter()
            .filter(|(n, _)| n.starts_with(prefix))
            .filter_map(|(_, e)| self.last.get(e).copied())
            .collect()
    }

    fn odd(&mut self, what: &str) {
        *self.unmodelled.entry(what.to_string()).or_default() += 1;
    }
}

fn int(t: Option<&Term>) -> Option<i64> {
    t.and_then(Term::as_int).map(|v| v as i64)
}

fn unit_of(t: &Term) -> Option<String> {
    match t {
        Term::View(n, s) if s.len() == 1 => Some(format!("{n}{}", s[0].as_int()?)),
        _ => None,
    }
}

/// `(words, runs)` of a mover statement's walks.
fn walked(st: &Stmt) -> (f64, f64) {
    let walks: Vec<&Vec<Term>> = st
        .positional()
        .into_iter()
        .filter_map(|t| match t {
            Term::Call(name, levels) if name == "w" => Some(levels),
            _ => None,
        })
        .collect();
    let levels = |w: &[Term]| -> Vec<(i64, i64)> {
        w.iter()
            .filter_map(|t| match t {
                Term::Tuple(l) => Some((l.first()?.as_int()? as i64, signed(l.get(1)?)?)),
                _ => None,
            })
            .collect()
    };
    let rows = int(st.kw("n"));
    let row = int(st.kw("words"));
    if let (Some(r), Some(w)) = (rows, row) {
        return ((r * w) as f64, r as f64);
    }
    let Some(first) = walks.first() else {
        return (rows.unwrap_or(1) as f64, 1.0);
    };
    let words: i64 = levels(first).iter().map(|l| l.0).product();
    let run = walks
        .iter()
        .map(|w| match levels(w).last() {
            Some(&(count, 32)) => count,
            _ => 1,
        })
        .min()
        .unwrap_or(1);
    (words as f64, (words as f64 / run.max(1) as f64).ceil())
}

/// Whether a copy, gather or scatter writes one-word runs: a copy's
/// destination walk (its second) ends in a level that is not contiguous
/// words, a gather or scatter moves rows of one word.
fn lone_writes(st: &Stmt) -> bool {
    match st.op.as_str() {
        "gather" | "scatter" => int(st.kw("words")) == Some(1),
        "copy" => {
            let walks: Vec<&Vec<Term>> = st
                .positional()
                .into_iter()
                .filter_map(|t| match t {
                    Term::Call(name, levels) if name == "w" => Some(levels),
                    _ => None,
                })
                .collect();
            let last = walks.get(1).and_then(|w| {
                w.iter().rev().find_map(|t| match t {
                    Term::Tuple(l) => Some((l.first()?.as_int()?, signed(l.get(1)?)?)),
                    _ => None,
                })
            });
            !matches!(last, Some((count, 32)) if count > 1)
        }
        _ => false,
    }
}

fn signed(t: &Term) -> Option<i64> {
    match t {
        Term::Neg(v) => Some(-(v.as_int()? as i64)),
        _ => t.as_int().map(|v| v as i64),
    }
}

/// An image's counts, its loops at their trip counts.
struct Census<'a> {
    machine: &'a Machine,
    core: Core,
    copies: u32,
    scalars: HashMap<String, i128>,
    walks: HashMap<String, f64>,
    work: Work,
}

impl Census<'_> {
    fn run(&mut self, stmts: &[Stmt], times: f64) {
        for st in stmts {
            let pos = st.positional();
            match st.op.as_str() {
                "loop" => {
                    let key = pos.first().and_then(|p| p.as_name()).unwrap_or("");
                    let n = *self.scalars.get(key).unwrap_or(&1) as f64;
                    self.run(&st.body, times * n);
                }
                "skipz" => self.run(&st.body, times),
                "seti" => {
                    if let Some(Term::Assign(lhs, v)) = st.args.first() {
                        if let (Some(k), Some(v)) = (lhs.as_name(), v.as_int()) {
                            self.scalars.insert(k.to_string(), v);
                        }
                    }
                }
                "desc" => {
                    let bind = st.args.iter().find(|x| matches!(x, Term::Assign(..)));
                    if let Some(Term::Assign(lhs, _)) = bind {
                        if let Some(n) = lhs.as_name() {
                            let words = st.kw("walk").map_or(1, walk_words);
                            self.walks.insert(n.to_string(), words as f64);
                        }
                    }
                }
                "vfill" | "vdrain" => {
                    let a = pos.first().and_then(|p| p.as_name()).unwrap_or("");
                    let bytes = self.walks.get(a).copied().unwrap_or(1.0) * WORD * times;
                    match st.op.as_str() {
                        "vfill" => self.work.read_bytes += bytes,
                        _ => self.work.write_bytes += bytes,
                    }
                }
                "vsync" if pos.first().and_then(|p| p.as_name()) == Some("fe") => {
                    if st.kw("on").and_then(Term::as_name) == Some("math") {
                        self.work.fe_syncs += times;
                    }
                }
                op if op == "vunpk" || op == "vpack" || is_math(op) => {
                    let text = fmt_stmt(st);
                    let (tmpl, syms) = symbolic(&text);
                    let o = parse(&tmpl, &syms, self.copies, &self.core);
                    let n = o.occ as f64 * times;
                    match o.eng {
                        "U" => {
                            self.work.unpack_words += n;
                            self.work.unpack_ops += times;
                        }
                        "P" => {
                            self.work.pack_words += n;
                            if !text.contains("mx7") {
                                self.work.pack_ops += times;
                            }
                        }
                        _ => self.work.math_beats += n,
                    }
                }
                _ => {}
            }
        }
        let _ = self.machine;
    }
}

fn is_math(op: &str) -> bool {
    op.starts_with('v')
        && !matches!(
            op,
            "vfill" | "vdrain" | "vbar" | "vsync" | "vhalt" | "vpost" | "vwait" | "vunpk" | "vpack"
        )
}

fn fmt_stmt(st: &Stmt) -> String {
    let mut line = st.op.clone();
    for (i, a) in st.args.iter().enumerate() {
        line.push_str(if i == 0 { " " } else { ", " });
        line.push_str(&fmt(a));
    }
    line
}

/// Program words of `stmts`, a loop's body once.
fn program_words(stmts: &[Stmt]) -> f64 {
    stmts
        .iter()
        .filter(|s| s.op != "desc")
        .map(|s| 1.0 + program_words(&s.body))
        .sum::<f64>()
}

/// One image: its cycles alone, its counts, its program's words.
fn image(
    module: &Module,
    stmts: &[Stmt],
    descs: &[Stmt],
    t: &Target,
    fill: (i64, i64),
    p: &Params,
) -> Result<(f64, Work, f64), String> {
    let sent = Sent {
        args: &[],
        descs,
        run: t.machine.mover.copy_cost.0 as i64,
    };
    let took = cycles_expanded(module, stmts, &sent, t.vregs, t.vcopies, fill, &t.machine)
        .map_err(|e| e.to_string())?;

    let mut c = Census {
        machine: &t.machine,
        core: Core::of(&t.machine),
        copies: t.vcopies,
        scalars: HashMap::new(),
        walks: HashMap::new(),
        work: Work::default(),
    };
    c.run(descs, 1.0);
    c.run(stmts, 1.0);
    let w = c.work;
    // What the core's own timing leaves out, each from its anchor.
    let took = took as f64
        + w.fe_syncs * p.vc_fe_sync
        + w.pack_ops * p.vc_pack_op
        + w.unpack_ops * p.vc_unpack_op;
    Ok((took, w, program_words(stmts)))
}

fn add(a: &mut Work, b: &Work, n: f64) {
    a.macs += b.macs * n;
    a.math_beats += b.math_beats * n;
    a.unpack_words += b.unpack_words * n;
    a.pack_words += b.pack_words * n;
    a.read_bytes += b.read_bytes * n;
    a.write_bytes += b.write_bytes * n;
    a.mover_bytes += b.mover_bytes * n;
    a.mover_statements += b.mover_statements * n;
    a.lone_words += b.lone_words * n;
    a.lone_moves += b.lone_moves * n;
    a.image_words += b.image_words * n;
    a.words += b.words * n;
    a.fe_syncs += b.fe_syncs * n;
    a.pack_ops += b.pack_ops * n;
    a.unpack_ops += b.unpack_ops * n;
}

struct Builder<'a> {
    g: Graph,
    p: &'a Params,
    module: &'a Module,
    t: &'a Target,
    fill: (i64, i64),
    /// In a kernel with clusters, a vector core's fill while none streams.
    alone: Option<(i64, i64)>,
    node_engine: usize,
    mesh: usize,
    prefix: String,
    marks: HashMap<String, usize>,
    moves: HashMap<String, usize>,
    rings: Vec<(usize, i64, i64, usize)>,
    slots: Vec<(usize, usize, i64, i64, bool)>,
    arrivals: Vec<(usize, String, i64, usize)>,
    receives: Vec<(usize, usize, String, i64)>,
    descriptors: HashMap<(usize, String, String, String), String>,
    walks: HashMap<(usize, String), BTreeMap<String, Stmt>>,
    expanded: HashMap<(String, Vec<i128>), Arc<Vec<Stmt>>>,
    resident: HashMap<(usize, String), Resident>,
    image_ids: HashMap<(String, Vec<i128>), u64>,
    images: HashMap<(String, Vec<i128>, Vec<(String, String)>), (f64, f64, Work, f64)>,
}

impl Builder<'_> {
    fn front(&mut self, label: &'static str, cycles: f64, deps: Vec<usize>) -> usize {
        self.g
            .push(self.node_engine, label, cycles, 0.0, 0.0, self.mesh, deps)
    }

    fn cluster(&mut self, u: &str, st: &Stmt, sent: usize) -> Result<(), String> {
        let (seq, sweep, emit) = (
            self.g.engine(format!("{}{u}", self.prefix)),
            self.g.engine(format!("{}{u}.sweep", self.prefix)),
            self.g.engine(format!("{}{u}.emit", self.prefix)),
        );
        let m = self.mesh;
        let n = int(st.kw("n")).unwrap_or(0) as f64;
        let flag = |name: &str| st.positional().iter().any(|p| p.as_name() == Some(name));
        match st.op.as_str() {
            "fill" if flag("recv") => {
                let at = self.g.push(seq, "mg.recv", 0.0, 0.0, 0.0, m, vec![sent]);
                self.receives
                    .push((at, m, u.to_string(), (n * ENTRY / WORD) as i64));
            }
            "fill" => {
                let c = n * self.p.mg_fill_entry;
                self.g
                    .push(seq, "mg.fill", c, n * ENTRY, 0.0, m, vec![sent]);
                self.g.work.read_bytes += n * ENTRY;
            }
            "gemm" => {
                let Some(Term::Dims(d)) = st.positional().first().copied() else {
                    return Err("gemm GMxGNxNK".into());
                };
                let (gm, gn, nk) = (d[0] as f64, d[1] as f64, d[2] as f64);
                let c = self.p.mg_start + gm * gn * nk * self.p.mg_sweep;
                let mut deps = vec![sent];
                deps.extend(self.g.last.get(&sweep).copied());
                let at = self.g.push(seq, "mg.issue", 0.0, 0.0, 0.0, m, deps);
                self.g.push(sweep, "mg.sweep", c, 0.0, 0.0, m, vec![at]);
                self.g.work.macs += gm * gn * nk * 16.0 * self.t.machine.mg.kblock as f64;
                if st.kw("emit").is_some() {
                    let tiles = gm * gn;
                    let c = c.max(tiles * self.p.mg_drain_word);
                    let local = st.kw("to").is_none();
                    let w = if local { tiles * WORD } else { 0.0 };
                    self.g.push(emit, "mg.emit", c, 0.0, w, m, vec![at]);
                    self.g.work.write_bytes += w;
                }
            }
            "drain" if flag("fused") => {
                let mut deps = vec![sent];
                deps.extend(self.g.last.get(&emit).copied());
                self.g.push(seq, "mg.fused", 0.0, 0.0, 0.0, m, deps);
            }
            "drain" => {
                let c = n * self.p.mg_drain_word;
                let w = if st.kw("to").is_none() { n * WORD } else { 0.0 };
                let mut deps = vec![sent];
                deps.extend(self.g.last.get(&sweep).copied());
                let at = self.g.push(seq, "mg.drain", c, 0.0, w, m, deps);
                if flag("count") {
                    if let Some(Term::View(name, index)) = st.kw("to") {
                        let (node, kind) = match name.rsplit_once('.') {
                            Some((prefix, kind)) => {
                                let node = prefix
                                    .strip_prefix("mesh")
                                    .and_then(|v| v.parse::<usize>().ok())
                                    .ok_or("peer drain physical sysnode")?;
                                (node, kind)
                            }
                            None => (m, name.as_str()),
                        };
                        if kind == "mg" {
                            let unit = index
                                .first()
                                .and_then(Term::as_int)
                                .ok_or("peer drain unit")?;
                            self.arrivals
                                .push((node, format!("mg{unit}"), n as i64, at));
                        }
                    }
                }
                self.g.work.write_bytes += w;
            }
            op => self.g.odd(&format!("cluster {op}")),
        }
        Ok(())
    }

    /// A descriptor update is redundant only while its fields still hold it.
    fn described(&mut self, unit: &str, descriptor: &Stmt) -> bool {
        let fields = descriptor.kwargs();
        let Some((id, _)) = fields.first() else {
            return true;
        };
        let mut changed = false;
        for (index, (field, value)) in fields.iter().enumerate() {
            let field = if index == 0 { "base" } else { field };
            let key = (
                self.mesh,
                unit.to_string(),
                id.to_string(),
                field.to_string(),
            );
            let value = fmt(value);
            changed |= self.descriptors.get(&key) != Some(&value);
            self.descriptors.insert(key, value);
        }
        if descriptor.kw("walk").is_none() {
            changed |= self
                .descriptors
                .remove(&(self.mesh, unit.to_string(), id.to_string(), "walk".into()))
                .is_some();
        }
        changed
    }

    /// Image-owned descriptors are installed at launch, even on an image hit.
    fn expanded(&mut self, run: &Stmt) -> Result<Arc<Vec<Stmt>>, String> {
        let Some(Term::Call(name, args)) = run.positional().first().copied() else {
            return Err("run IMAGE(args)".into());
        };
        let values: Vec<i128> = args.iter().map(|a| a.as_int().unwrap_or(0)).collect();
        let key = (name.clone(), values);
        if let Some(stmts) = self.expanded.get(&key) {
            return Ok(Arc::clone(stmts));
        }
        let Some(img) = self.module.images.get(name) else {
            return Ok(Arc::new(Vec::new()));
        };
        let env: Env = img
            .params
            .iter()
            .zip(key.1.iter().copied().chain(std::iter::repeat(0)))
            .map(|(p, v)| (p.clone(), Val::Int(v)))
            .collect();
        let reader = self.module.reader();
        let macros = self.module.macro_table();
        let stmts = Expander::new(&macros, &reader, true)
            .stmts(&img.stmts, &env)
            .map_err(|e| e.to_string())?;
        let stmts = Arc::new(stmts);
        self.expanded.insert(key, Arc::clone(&stmts));
        Ok(stmts)
    }

    fn image_price(&mut self, run: &Stmt, descs: &[Stmt]) -> Result<(f64, f64, Work, f64), String> {
        let walks: Vec<_> = descs
            .iter()
            .filter_map(|d| {
                let fields = d.kwargs();
                let (id, _) = fields.first()?;
                Some((id.to_string(), d.kw("walk").map(fmt).unwrap_or_default()))
            })
            .collect();
        let Some(Term::Call(name, args)) = run.positional().first().copied() else {
            return Err("run IMAGE(args)".into());
        };
        let arguments: Vec<i128> = args.iter().map(|a| a.as_int().unwrap_or(0)).collect();
        let key = (name.clone(), arguments, walks);
        if let Some(value) = self.images.get(&key) {
            return Ok(*value);
        }
        let stmts = self.expanded(run)?;
        let (took, work, program) = image(self.module, &stmts, descs, self.t, self.fill, self.p)?;
        let alone = if let Some(fill) = self.alone {
            image(self.module, &stmts, descs, self.t, fill, self.p)?.0
        } else {
            took
        };
        let value = (took, alone, work, program);
        self.images.insert(key, value);
        Ok(value)
    }

    fn send(&mut self, st: &Stmt) -> Result<(), String> {
        let u = st
            .positional()
            .first()
            .and_then(|t| unit_of(t))
            .ok_or("send UNIT")?;
        let repeats = int(st.kw("repeats")).unwrap_or(1).max(1) as f64;
        if u.starts_with("mg") {
            for s in &st.body {
                let sent = self.front("node.send", self.p.node_word, vec![]);
                self.g.work.words += 1.0;
                self.cluster(&u, s, sent)?;
            }
            return Ok(());
        }
        let engine = self.g.engine(format!("{}{u}", self.prefix));
        let mut body = Vec::new();
        for s in &st.body {
            if u.starts_with("vc") && s.op == "run" {
                body.extend(
                    self.expanded(s)?
                        .iter()
                        .take_while(|s| s.op == "desc")
                        .cloned(),
                );
            }
            body.push(s.clone());
        }
        // A descriptor word writing what its field holds is never sent.
        let fresh = body
            .iter()
            .filter(|s| s.op != "desc" || self.described(&u, s))
            .count();
        let words = fresh as f64 * repeats;
        let sent = self.front(
            "node.send",
            self.p.node_send + words * self.p.node_word,
            vec![],
        );
        self.g.work.words += words;
        if !u.starts_with("vc") {
            self.g.odd(&format!("{u} execution"));
            self.g
                .push(engine, "unit.command", 0.0, 0.0, 0.0, self.mesh, vec![sent]);
            return Ok(());
        }
        // Fixed descriptor writes reach their steady state after one traversal.
        // Keep a second state only for a repeated body that changes an inherited walk.
        let mut steady = if repeats > 1.0 {
            let initial = self.walks.get(&(self.mesh, u.clone()));
            let mut final_walks = initial.cloned().unwrap_or_default();
            for s in body.iter().filter(|s| s.op == "desc") {
                if let Some((id, _)) = s.kwargs().first() {
                    final_walks.insert(id.to_string(), s.clone());
                }
            }
            let changed = final_walks.iter().any(|(id, desc)| {
                initial
                    .and_then(|walks| walks.get(id))
                    .map(|s| s.kw("walk"))
                    != Some(desc.kw("walk"))
            });
            changed.then_some(final_walks)
        } else {
            None
        };
        for s in &body {
            if s.op == "desc" {
                if let Some((id, _)) = s.kwargs().first() {
                    self.walks
                        .entry((self.mesh, u.clone()))
                        .or_default()
                        .insert(id.to_string(), s.clone());
                    if let Some(steady) = &mut steady {
                        steady.insert(id.to_string(), s.clone());
                    }
                }
                continue;
            }
            if s.op != "run" {
                continue;
            }
            let descs: Vec<_> = self
                .walks
                .get(&(self.mesh, u.clone()))
                .into_iter()
                .flat_map(|descs| descs.values().cloned())
                .collect();
            let Some(Term::Call(name, args)) = s.positional().first().copied() else {
                return Err("run IMAGE(args)".into());
            };
            let arguments: Vec<i128> = args.iter().map(|a| a.as_int().unwrap_or(0)).collect();
            let (first, first_alone, first_work, program) = self.image_price(s, &descs)?;
            let (took, alone, work) = if let Some(steady) = &steady {
                let held: Vec<_> = steady.values().cloned().collect();
                let (next, next_alone, next_work, _) = self.image_price(s, &held)?;
                let mut work = first_work;
                add(&mut work, &next_work, repeats - 1.0);
                (
                    first + next * (repeats - 1.0),
                    first_alone + next_alone * (repeats - 1.0),
                    work,
                )
            } else {
                let mut work = Work::default();
                add(&mut work, &first_work, repeats);
                (first * repeats, first_alone * repeats, work)
            };
            let (r, w) = (work.read_bytes, work.write_bytes);
            let next = self.image_ids.len() as u64;
            let id = *self
                .image_ids
                .entry((name.clone(), arguments))
                .or_insert(next);
            let vc = &self.t.machine.vc;
            let (_, loaded) = self
                .resident
                .entry((self.mesh, u.clone()))
                .or_insert_with(|| Resident::new(vc.imem_words, vc.run_reach))
                .place(vec![(0, id)], program as u32)
                .map_err(|e| e.to_string())?;
            let sent = if !loaded {
                self.g.work.image_words += program;
                self.front("node.image", program * self.p.node_image, vec![sent])
            } else {
                sent
            };
            add(&mut self.g.work, &work, 1.0);
            let id = self
                .g
                .push(engine, "vc.run", took, r, w, self.mesh, vec![sent]);
            self.g.tasks[id].alone = alone;
        }
        Ok(())
    }

    fn moved(&mut self, st: &Stmt) -> Result<(), String> {
        let posted = !st.positional().is_empty();
        let mover = self.g.engine(format!("{}mover", self.prefix));
        let (a_run, a_word) = (self.p.mover_run, self.p.mover_word);
        let mut deps = Vec::new();
        if !posted {
            let prefix = self.prefix.clone();
            let floor = self.g.by_name.get(&format!("{prefix}firmware")).copied();
            let at = floor.and_then(|e| self.g.last.get(&e).copied());
            deps = self.g.lasts(&prefix);
            deps.retain(|t| Some(*t) != at);
        }
        let sent = self.front("node.move", self.p.node_move, deps);
        let mut last = sent;
        for (i, s) in st.body.iter().enumerate() {
            let (words, runs) = walked(s);
            let bytes = words * WORD;
            // The first statement's start is in `node_move`.
            let start = match (i, s.op.as_str()) {
                (0, _) => 0.0,
                (_, "fill") => self.p.mover_fill,
                _ => self.p.mover_statement,
            };
            let each = match s.op.as_str() {
                "gather" | "scatter" => self.p.mover_element,
                _ => a_run,
            };
            let c = start + each * runs + a_word * words;
            let quant = matches!(s.op.as_str(), "quant" | "gt4");
            let label = if quant { "mover.form" } else { "mover.copy" };
            // A mover's writes never share the drains' path.
            last = self
                .g
                .push(mover, label, c, bytes, 0.0, self.mesh, vec![sent]);
            self.g.work.mover_bytes += bytes;
            self.g.work.mover_statements += 1.0;
            if lone_writes(s) && words > 1.0 {
                self.g.work.lone_words += words;
                self.g.work.lone_moves += 1.0;
            }
        }
        if posted {
            if let Some(n) = st.target_name() {
                self.moves.insert(n.to_string(), last);
            }
        } else {
            self.front("node.wait", 0.0, vec![last]);
        }
        Ok(())
    }

    fn statement(&mut self, st: &Stmt) -> Result<(), String> {
        let pos = st.positional();
        match st.op.as_str() {
            "send" => self.send(st),
            "move" => self.moved(st),
            "load" | "assert_zero" => {
                self.front("node.step", self.p.node_word, vec![]);
                Ok(())
            }
            "mark" => {
                let u = pos.first().and_then(|t| unit_of(t)).ok_or("mark UNIT")?;
                let e = self.g.engine(format!("{}{u}", self.prefix));
                let at = self.g.last.get(&e).copied();
                if let (Some(n), Some(at)) = (st.target_name(), at) {
                    self.marks.insert(n.to_string(), at);
                }
                Ok(())
            }
            "wait" if pos.first().and_then(|p| p.as_name()) == Some("slot") => {
                let slot = int(pos.get(1).copied()).ok_or("wait slot N")?;
                let n = int(st.kw("n")).unwrap_or(1);
                let at = self.front("node.ring", 0.0, vec![]);
                let consume = pos.iter().any(|p| p.as_name() == Some("consume"));
                self.slots.push((at, self.mesh, slot, n, consume));
                Ok(())
            }
            "wait" if pos.first().and_then(|p| p.as_name()) == Some("moves") => {
                let name = pos.get(1).and_then(|p| p.as_name()).unwrap_or("");
                let dep: Vec<usize> = self.moves.get(name).copied().into_iter().collect();
                self.front("node.wait", 0.0, dep);
                Ok(())
            }
            "wait" => {
                let u = pos.first().and_then(|t| unit_of(t)).ok_or("wait UNIT")?;
                let e = self.g.engine(format!("{}{u}", self.prefix));
                let mut dep: Vec<usize> = match st.kw("upto").and_then(Term::as_name) {
                    Some(name) => self.marks.get(name).copied().into_iter().collect(),
                    None => self.g.last.get(&e).copied().into_iter().collect(),
                };
                let extra = format!("{}{u}.", self.prefix);
                if st.kw("upto").is_none() {
                    dep.extend(self.g.lasts(&extra));
                }
                self.front("node.wait", self.p.node_wait, dep);
                Ok(())
            }
            "barrier" => {
                let prefix = self.prefix.clone();
                let deps = self.g.lasts(&prefix);
                self.front("node.wait", 0.0, deps);
                Ok(())
            }
            "ring" => {
                let mesh = int(st.kw("mesh")).unwrap_or(0) as usize;
                let slot = int(st.kw("slot")).unwrap_or(0);
                let amount = int(st.kw("amount")).unwrap_or(1);
                let at = self.front("node.ring", self.p.ring, vec![]);
                self.rings.push((mesh, slot, amount, at));
                Ok(())
            }
            op => {
                self.g.odd(op);
                Ok(())
            }
        }
    }
}

fn build(module: &Module, name: &str, t: &Target, p: &Params) -> Result<Graph, String> {
    let body = module.body(name, "l1").map_err(|e| e.to_string())?;
    let clusters = body.stmts.iter().any(super::sends_to_clusters);
    let timed = super::timed_module(module);
    let module = &timed;
    let fill = |shared: bool| {
        let (latency, word) = t.mem(shared);
        let word = (word as f64 * p.vc_memory_word).round() as i64;
        ((latency as f64 - p.vc_fill_trim).max(0.0) as i64, word)
    };
    // A vector core is priced beside streaming clusters only while they stream.
    let mut b = Builder {
        g: Graph::default(),
        p,
        module,
        t,
        fill: fill(clusters),
        alone: clusters.then(|| fill(false)),
        node_engine: 0,
        mesh: 0,
        prefix: String::new(),
        marks: HashMap::new(),
        moves: HashMap::new(),
        rings: Vec::new(),
        slots: Vec::new(),
        arrivals: Vec::new(),
        receives: Vec::new(),
        descriptors: HashMap::new(),
        walks: HashMap::new(),
        expanded: HashMap::new(),
        resident: HashMap::new(),
        image_ids: HashMap::new(),
        images: HashMap::new(),
    };
    let nodes: Vec<&Stmt> = body.stmts.iter().filter(|s| s.op == "node").collect();
    let parts: Vec<(usize, &[Stmt])> = if nodes.is_empty() {
        vec![(0, &body.stmts[..])]
    } else {
        nodes
            .iter()
            .map(|s| {
                (
                    int(s.positional().first().copied()).unwrap_or(0) as usize,
                    &s.body[..],
                )
            })
            .collect()
    };
    let (reader, macros) = (module.reader(), module.macro_table());
    for (mesh, stmts) in parts {
        let previous = std::mem::take(&mut b.g.work);
        b.mesh = mesh;
        b.prefix = format!("n{mesh}.");
        b.node_engine = b.g.engine(format!("n{mesh}.node"));
        b.marks.clear();
        b.moves.clear();

        // The firmware's own path runs beside the units: a floor, not a sum.
        let floor = b.g.engine(format!("n{mesh}.firmware"));
        b.g.push(floor, "node.floor", p.node_floor, 0.0, 0.0, mesh, vec![]);
        b.front("node.start", p.node_start, vec![]);
        let flat = Expander::new(&macros, &reader, true)
            .stmts(stmts, &Env::default())
            .map_err(|e| e.to_string())?;
        for st in &flat {
            b.statement(st)?;
        }
        add(b.g.node_work.entry(mesh).or_default(), &b.g.work, 1.0);
        add(&mut b.g.work, &previous, 1.0);
    }

    let mut signalled = HashSet::new();
    for (mesh, slot, amount, task) in std::mem::take(&mut b.rings) {
        signalled.insert((mesh, slot));
        b.g.rings.insert(task, ((mesh, slot), amount));
    }
    for (at, mesh, slot, needed, consume) in std::mem::take(&mut b.slots) {
        if signalled.contains(&(mesh, slot)) {
            b.g.slots.insert(at, ((mesh, slot), needed, consume));
        } else {
            b.g.odd("external slot notification");
        }
    }
    let mut counted = HashSet::new();
    for (mesh, unit, words, task) in std::mem::take(&mut b.arrivals) {
        counted.insert((mesh, unit.clone()));
        b.g.arrivals.insert(task, ((mesh, unit), words));
    }
    for (at, mesh, unit, words) in std::mem::take(&mut b.receives) {
        if counted.contains(&(mesh, unit.clone())) {
            b.g.receives.insert(at, ((mesh, unit), words));
        } else {
            b.g.odd("external counted fill");
        }
    }
    Ok(b.g)
}

/// One kernel in time.
#[derive(Debug, Default)]
pub struct Timed {
    pub cycles: f64,
    /// Completion times including dependencies on other physical sysnodes.
    pub nodes: BTreeMap<usize, f64>,
    pub busy: BTreeMap<String, BTreeMap<String, f64>>,
    pub stalled: f64,
    pub read_peak: f64,
}

fn simulate(g: &Graph, p: &Params) -> Result<Timed, String> {
    let n = g.tasks.len();
    let mut left: Vec<f64> = g.tasks.iter().map(|t| t.cycles.max(0.0)).collect();
    let mut after: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut waiting: Vec<usize> = vec![0; n];
    for (i, t) in g.tasks.iter().enumerate() {
        waiting[i] = t.deps.len();
        for &d in &t.deps {
            after[d].push(i);
        }
    }
    let mut active: Vec<usize> = (0..n).filter(|&i| waiting[i] == 0).collect();
    let mut out = Timed::default();
    let mut now = 0.0;
    let mut done = 0;
    let mut counted: HashMap<(usize, String), i64> = HashMap::new();
    let mut slots: HashMap<(usize, i64), i64> = HashMap::new();
    while done < n {
        let mut i = 0;
        let mut finished = Vec::new();
        while i < active.len() {
            let task = active[i];
            if left[task] <= 1e-9 {
                if let Some((key, needed, consume)) = g.slots.get(&task) {
                    let available = slots.entry(*key).or_default();
                    if *available < *needed {
                        i += 1;
                        continue;
                    }
                    if *consume {
                        *available -= *needed;
                    }
                }
                if let Some((key, amount)) = g.rings.get(&task) {
                    *slots.entry(*key).or_default() += *amount;
                }
                if let Some((key, words)) = g.receives.get(&task) {
                    let available = counted.entry(key.clone()).or_default();
                    if *available < *words {
                        i += 1;
                        continue;
                    }
                    *available -= *words;
                }
                if let Some((key, words)) = g.arrivals.get(&task) {
                    *counted.entry(key.clone()).or_default() += *words;
                }
                finished.push(active.swap_remove(i));
            } else {
                i += 1;
            }
        }
        if !finished.is_empty() {
            for t in finished {
                out.nodes.insert(g.tasks[t].node, now);
                done += 1;
                for &s in &after[t] {
                    waiting[s] -= 1;
                    if waiting[s] == 0 {
                        active.push(s);
                    }
                }
            }
            continue;
        }
        let running: Vec<_> = active.iter().copied().filter(|&a| left[a] > 1e-9).collect();
        if running.is_empty() {
            return Err("resource dependencies cannot complete".into());
        }

        // The memory policy belongs to the physical sysnode's current phase.
        let streaming: HashSet<usize> = running
            .iter()
            .map(|&a| &g.tasks[a])
            .filter(|t| t.label.starts_with("mg.") && t.cycles > 0.0)
            .map(|t| t.node)
            .collect();
        let pace = |t: &Task| match streaming.contains(&t.node) || t.alone <= 0.0 {
            true => 1.0,
            false => t.cycles / t.alone,
        };
        // Each task requests bytes per wall cycle, including its phase speed.
        let mut asked: HashMap<usize, (f64, f64)> = HashMap::new();
        for &a in &running {
            let t = &g.tasks[a];
            let e = asked.entry(t.node).or_default();
            e.0 += t.read / t.cycles * pace(t);
            e.1 += t.write / t.cycles * pace(t);
        }
        let share = |t: &Task| {
            let (r, w) = asked[&t.node];
            let fr = if t.read > 0.0 {
                (p.read_bus / r).min(1.0)
            } else {
                1.0
            };
            let fw = if t.write > 0.0 {
                (p.write_bus / w).min(1.0)
            } else {
                1.0
            };
            fr.min(fw)
        };
        let dt = running
            .iter()
            .map(|&a| left[a] / (share(&g.tasks[a]) * pace(&g.tasks[a])))
            .fold(f64::INFINITY, f64::min);
        for &a in &running {
            let t = &g.tasks[a];
            let f = share(t);
            left[a] -= dt * f * pace(t);
            *out.busy
                .entry(g.engines[t.engine].clone())
                .or_default()
                .entry(t.label.to_string())
                .or_default() += dt;
            out.stalled += dt * (1.0 - f);
        }
        out.read_peak = asked.values().map(|v| v.0).fold(out.read_peak, f64::max);
        now += dt;
    }
    out.cycles = now;
    Ok(out)
}

/// Resource work in the report schema, for a node or the whole card.
fn reported_work(w: &Work) -> Value {
    json!({
        "macs": w.macs,
        "math_beats": w.math_beats,
        "unpack_words": w.unpack_words,
        "pack_words": w.pack_words,
        "read_bytes": w.read_bytes,
        "write_bytes": w.write_bytes,
        "mover_bytes": w.mover_bytes,
        "mover_statements": w.mover_statements,
        "lone_words": w.lone_words,
        "lone_moves": w.lone_moves,
        "image_words": w.image_words,
        "words": w.words,
    })
}

/// `fn name.l1` of `module`: the row the reports read.
pub fn kernel(module: &Module, name: &str, m: &Machine, p: &Params) -> Result<Value, String> {
    let t = Target::of(m);
    let g = build(module, name, &t, p)?;
    let timed = simulate(&g, p)?;
    let node_work: BTreeMap<_, _> = g
        .node_work
        .iter()
        .map(|(node, w)| (*node, reported_work(w)))
        .collect();
    let units = |e: &str| g.engines.iter().filter(|n| n.contains(e)).count();
    let mgs = (m.mg.count as f64).max(1.0);
    let vcs = (m.vc.count as f64).max(1.0);
    let floor = |resource: fn(&Work) -> f64| g.node_work.values().map(resource).fold(0.0, f64::max);
    Ok(json!({
        "name": name,
        "cycles": timed.cycles,
        "node_cycles": timed.nodes,
        "stalled": timed.stalled,
        "tasks": g.tasks.len(),
        "busy": timed.busy,
        "unmodelled": g.unmodelled,
        "work": reported_work(&g.work),
        "node_work": node_work,
        "floor": {
            "mg": floor(|w| w.macs) * p.mg_peak_sweep / (16.0 * m.mg.kblock as f64 * mgs),
            "vc": floor(|w| w.math_beats) / vcs,
            "read": floor(|w| w.read_bytes + w.mover_bytes) / p.read_bus,
            "write": floor(|w| w.write_bytes) / p.write_bus,
        },
        "used": {"mg": units(".sweep"), "vc": units(".vc")},
    }))
}

/// Per-sysnode completion in function scope order, excluding the launch front.
/// This is the same task graph and shared-bus scheduler used by [`kernel`].
pub fn local_cycles(module: &Module, name: &str, t: &Target) -> crate::ir::Result<Vec<i64>> {
    let parameters = Params::planning(&t.machine);
    let graph = build(module, name, t, &parameters)
        .map_err(|message| ktpu_l1::Error::new("L2.COST.001", message))?;
    let timed = simulate(&graph, &parameters)
        .map_err(|message| ktpu_l1::Error::new("L2.COST.001", message))?;
    let body = module.body(name, "l1")?;
    let ids: Vec<_> = body.stmts.iter().filter(|s| s.op == "node").collect();
    if ids.is_empty() {
        return Ok(vec![timed.cycles.ceil() as i64]);
    }
    ids.iter()
        .map(|s| {
            let node = int(s.positional().first().copied()).ok_or_else(|| {
                ktpu_l1::Error::new("L2.COST.001", "physical sysnode scope needs an integer")
            })? as usize;
            Ok(timed.nodes.get(&node).copied().unwrap_or(0.0).ceil() as i64)
        })
        .collect()
}
