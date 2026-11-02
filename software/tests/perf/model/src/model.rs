//! The timed model: a kernel's L1 as a task graph on the node, its units and the
//! memory bus. Tasks of one engine run in order; tasks moving bytes share the bus
//! in proportion to what they ask of it. `params.json` holds every timing number
//! with the anchor that pins it; the rest is the program's text.

use std::collections::{BTreeMap, HashMap, HashSet};

use ktpu_l1::sched::timing::{cycles_with, parse, symbolic, walk_words, Params as Core, Sent};
use ktpu_l1::text::{fmt, Env, Expander, Module, Stmt, Term, Val};
use ktpu_l2::target::Target;
use ktpu_machine::Machine;
use serde_json::{json, Value};

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
    /// `tier` names the column read, `value` where a parameter holds no other.
    pub fn of(v: &Value, tier: &str) -> Params {
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
        Params {
            node_start: at("node_start"),
            node_floor: at("node_floor"),
            node_send: at("node_send"),
            node_word: at("node_word"),
            node_move: at("node_move"),
            node_image: at("node_image"),
            node_wait: at("node_wait"),
            mover_run: at("mover_run"),
            mover_word: at("mover_word"),
            mover_statement: at("mover_statement"),
            mover_fill: at("mover_fill"),
            mover_element: at("mover_element"),
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
                    if let (Some(Term::Assign(lhs, _)), Some(walk)) = (bind, st.kw("walk")) {
                        if let Some(n) = lhs.as_name() {
                            self.walks.insert(n.to_string(), walk_words(walk) as f64);
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

/// `stmts` with each `skipz` replaced by what it guards: image timing reads a block
/// only under `loop`, and a guard's body runs whenever the count is not zero.
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
    stmts
        .iter()
        .map(|s| 1.0 + program_words(&s.body))
        .sum::<f64>()
}

/// One image: its cycles alone, its counts, its program's words.
fn image(
    module: &Module,
    run: &Stmt,
    descs: &[Stmt],
    t: &Target,
    fill: (i64, i64),
    p: &Params,
) -> Result<(String, f64, Work, f64), String> {
    let Some(Term::Call(name, args)) = run.positional().first().copied() else {
        return Err("run IMAGE(args)".into());
    };
    let Some(img) = module.images.get(name) else {
        return Ok((name.clone(), 0.0, Work::default(), 0.0));
    };
    let values: Vec<i128> = args.iter().map(|a| a.as_int().unwrap_or(0)).collect();
    let sent = Sent {
        args: &values,
        descs,
        run: t.machine.mover.copy_cost.0 as i64,
    };
    let took = cycles_with(module, name, &sent, t.vregs, t.vcopies, fill, &t.machine)
        .map_err(|e| e.to_string())?;

    let r = module.reader();
    let macros = module.macro_table();
    let env: Env = img
        .params
        .iter()
        .zip(values.iter().copied().chain(std::iter::repeat(0)))
        .map(|(p, v)| (p.clone(), Val::Int(v)))
        .collect();
    let stmts = Expander::new(&macros, &r, true)
        .stmts(&img.stmts, &env)
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
    c.run(&stmts, 1.0);
    let w = c.work;
    // What the core's own timing leaves out, each from its anchor.
    let took = took as f64
        + w.fe_syncs * p.vc_fe_sync
        + w.pack_ops * p.vc_pack_op
        + w.unpack_ops * p.vc_unpack_op;
    Ok((name.clone(), took, w, program_words(&stmts)))
}

fn add(a: &mut Work, b: &Work, n: f64) {
    a.macs += b.macs * n;
    a.math_beats += b.math_beats * n;
    a.unpack_words += b.unpack_words * n;
    a.pack_words += b.pack_words * n;
    a.read_bytes += b.read_bytes * n;
    a.write_bytes += b.write_bytes * n;
    a.mover_bytes += b.mover_bytes * n;
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
    node_engine: usize,
    mesh: usize,
    prefix: String,
    marks: HashMap<String, usize>,
    moves: HashMap<String, usize>,
    rings: Vec<(usize, i64, i64, usize)>,
    slots: Vec<(usize, usize, i64, i64)>,
    resident: HashSet<(usize, String, String)>,
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
                self.g.odd("fill recv");
                self.g.push(seq, "mg.recv", 0.0, 0.0, 0.0, m, vec![sent]);
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
                self.g.push(seq, "mg.drain", c, 0.0, w, m, deps);
                self.g.work.write_bytes += w;
            }
            op => self.g.odd(&format!("cluster {op}")),
        }
        Ok(())
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
        let descs: Vec<Stmt> = st.body.iter().filter(|s| s.op == "desc").cloned().collect();
        // A descriptor word writing what its field holds is never sent.
        let mesh = self.mesh;
        let fresh = st
            .body
            .iter()
            .filter(|s| s.op != "desc" || self.resident.insert((mesh, u.clone(), fmt_stmt(s))))
            .count();
        let words = fresh as f64 * repeats;
        let sent = self.front(
            "node.send",
            self.p.node_send + words * self.p.node_word,
            vec![],
        );
        self.g.work.words += words;
        for s in st.body.iter().filter(|s| s.op == "run") {
            let (name, took, work, program) =
                image(self.module, s, &descs, self.t, self.fill, self.p)?;
            let (r, w) = (work.read_bytes, work.write_bytes);
            // A program goes to its core once a launch.
            let sent = if self.resident.insert((self.mesh, u.clone(), name)) {
                self.g.work.image_words += program;
                self.front("node.image", program * self.p.node_image, vec![sent])
            } else {
                sent
            };
            add(&mut self.g.work, &work, repeats);
            self.g.push(
                engine,
                "vc.run",
                took * repeats,
                r * repeats,
                w * repeats,
                self.mesh,
                vec![sent],
            );
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
                self.slots.push((at, self.mesh, slot, n));
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
    let sends = |st: &Stmt| {
        st.op == "send"
            && st
                .positional()
                .first()
                .and_then(|u| unit_of(u))
                .is_some_and(|u| u.starts_with("mg"))
    };
    let clusters = body
        .stmts
        .iter()
        .any(|s| sends(s) || s.body.iter().any(sends));
    let mut timed = module.clone();
    for img in timed.images.values_mut() {
        img.stmts = unguarded(&img.stmts);
    }
    let module = &timed;
    let mut b = Builder {
        g: Graph::default(),
        p,
        module,
        t,
        fill: {
            let (latency, word) = t.mem(clusters);
            let word = (word as f64 * p.vc_memory_word).round() as i64;
            ((latency as f64 - p.vc_fill_trim).max(0.0) as i64, word)
        },
        node_engine: 0,
        mesh: 0,
        prefix: String::new(),
        marks: HashMap::new(),
        moves: HashMap::new(),
        rings: Vec::new(),
        slots: Vec::new(),
        resident: HashSet::new(),
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
    }

    // A wait holds for the ring that brings its slot to its count.
    let rings = std::mem::take(&mut b.rings);
    for (at, mesh, slot, n) in std::mem::take(&mut b.slots) {
        let mut sum = 0;
        for &(m, s, amount, ring) in &rings {
            if m == mesh && s == slot {
                sum += amount;
                b.g.tasks[at].deps.push(ring);
                if sum >= n {
                    break;
                }
            }
        }
    }
    Ok(b.g)
}

/// One kernel in time.
#[derive(Debug, Default)]
pub struct Timed {
    pub cycles: f64,
    pub busy: BTreeMap<String, BTreeMap<String, f64>>,
    pub stalled: f64,
    pub read_peak: f64,
}

fn simulate(g: &Graph, p: &Params) -> Timed {
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
    while done < n {
        let mut i = 0;
        let mut finished = Vec::new();
        while i < active.len() {
            if left[active[i]] <= 1e-9 {
                finished.push(active.swap_remove(i));
            } else {
                i += 1;
            }
        }
        if !finished.is_empty() {
            for t in finished {
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
        if active.is_empty() {
            break;
        }

        // Each node's bus shared in proportion to what is asked of it.
        let mut asked: HashMap<usize, (f64, f64)> = HashMap::new();
        for &a in &active {
            let t = &g.tasks[a];
            let e = asked.entry(t.node).or_default();
            e.0 += t.read / t.cycles;
            e.1 += t.write / t.cycles;
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
        let dt = active
            .iter()
            .map(|&a| left[a] / share(&g.tasks[a]))
            .fold(f64::INFINITY, f64::min);
        for &a in &active {
            let t = &g.tasks[a];
            let f = share(t);
            left[a] -= dt * f;
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
    out
}

/// `fn name.l1` of `module`: the row the reports read.
pub fn kernel(module: &Module, name: &str, m: &Machine, p: &Params) -> Result<Value, String> {
    let t = Target::of(m);
    let g = build(module, name, &t, p)?;
    let timed = simulate(&g, p);
    let w = g.work;
    let units = |e: &str| g.engines.iter().filter(|n| n.contains(e)).count();
    let mgs = (m.mg.count as f64).max(1.0);
    let vcs = (m.vc.count as f64).max(1.0);
    Ok(json!({
        "name": name,
        "cycles": timed.cycles,
        "stalled": timed.stalled,
        "tasks": g.tasks.len(),
        "busy": timed.busy,
        "unmodelled": g.unmodelled,
        "work": {
            "macs": w.macs,
            "math_beats": w.math_beats,
            "unpack_words": w.unpack_words,
            "pack_words": w.pack_words,
            "read_bytes": w.read_bytes,
            "write_bytes": w.write_bytes,
            "mover_bytes": w.mover_bytes,
            "mover_statements": w.mover_statements,
            "image_words": w.image_words,
            "words": w.words,
        },
        "floor": {
            "mg": w.macs * p.mg_peak_sweep / (16.0 * m.mg.kblock as f64 * mgs),
            "vc": w.math_beats / vcs,
            "read": (w.read_bytes + w.mover_bytes) / p.read_bus,
            "write": w.write_bytes / p.write_bus,
        },
        "used": {"mg": units(".sweep"), "vc": units(".vc")},
    }))
}
