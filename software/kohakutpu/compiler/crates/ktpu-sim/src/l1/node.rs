//! An L1 body on the machine: the node's statements in order (L1.NODE).

use std::collections::HashMap;
use std::rc::Rc;

use ktpu_l1::text::{fmt, Module, Stmt, Term};

use super::image;
use super::term::{int, Env};
use crate::machine::{Machine, Unit, VcWord};
use crate::units::cluster;
use crate::units::core::inst::Inst;
use crate::units::core::Desc;
use crate::units::mover::{Index, Level, Move, Walk};
use crate::R;

/// A parameter's byte address.
pub type Binds = HashMap<String, i64>;

/// A read image: its leading descriptors and instructions.
type Image = (Vec<(u32, Desc)>, Rc<Vec<Inst>>);

/// One node's program as it runs (L1.NODE, each `node N` a mesh).
struct Program<'a> {
    mesh: usize,
    stmts: &'a [Stmt],
    pc: usize,
    marks: HashMap<String, (Unit, u64)>,
    moves: HashMap<String, u64>,
    regs: HashMap<String, u64>,
    /// The moves a blocking `move` still waits for.
    moving: u64,
}

impl<'a> Program<'a> {
    fn new(mesh: usize, stmts: &'a [Stmt]) -> Program<'a> {
        Program {
            mesh,
            stmts,
            pc: 0,
            marks: HashMap::new(),
            moves: HashMap::new(),
            regs: HashMap::new(),
            moving: 0,
        }
    }
}

struct Node<'a> {
    module: &'a Module,
    m: &'a mut Machine,
    env: Env,
    p: Program<'a>,
    /// Rings a mesh's slot has received (L1.NODE, `ring`).
    rings: HashMap<(usize, i64), i64>,
    images: HashMap<(String, Vec<i64>), Image>,
}

/// Each `node N`'s statements, or the body as mesh 0's.
fn programs(stmts: &[Stmt]) -> R<Vec<Program<'_>>> {
    if !stmts.iter().any(|s| s.op == "node") {
        return Ok(vec![Program::new(0, stmts)]);
    }
    let of = |s: &'_ Stmt| {
        s.positional()
            .first()
            .and_then(|t| t.as_int())
            .map(|n| n as usize)
    };
    stmts
        .iter()
        .map(|s| Ok(Program::new(of(s).ok_or("node N")?, &s.body)))
        .collect()
}

/// Runs `fn name.l1`, every unit run out at its end (L1.LOW.011).
pub fn run(module: &Module, name: &str, binds: &Binds, m: &mut Machine) -> R<()> {
    let body = module.body(name, "l1").map_err(|e| e.to_string())?;
    let mut rest = programs(&body.stmts)?;
    let mut node = Node {
        module,
        m,
        env: binds.clone(),
        p: Program::new(0, &[]),
        rings: HashMap::new(),
        images: HashMap::new(),
    };
    while rest.iter().any(|p| p.pc < p.stmts.len()) {
        let mut moved = false;
        for p in rest.iter_mut() {
            std::mem::swap(p, &mut node.p);
            let out = node.advance().map_err(|e| format!("{name}.l1 {e}"));
            std::mem::swap(p, &mut node.p);
            moved |= out?;
        }
        if !moved && !node.m.step()? {
            return Err(format!("{name}.l1: a wait no unit can end"));
        }
    }
    node.m.until(Machine::idle)
}

fn unit(t: &Term) -> R<Unit> {
    match t {
        Term::View(n, s) if s.len() == 1 => {
            let i = s[0].as_int().ok_or(format!("unit {}", fmt(t)))? as usize;
            match n.as_str() {
                "vc" => Ok(Unit::Vc(i)),
                "mg" => Ok(Unit::Mg(i)),
                _ => Err(format!("unit {}", fmt(t))),
            }
        }
        _ => Err(format!("unit {}", fmt(t))),
    }
}

impl Node<'_> {
    /// This program's statements until one must wait; whether any ran.
    fn advance(&mut self) -> R<bool> {
        let mut moved = false;
        while let Some(st) = self.p.stmts.get(self.p.pc) {
            let line = |e: String| format!("line {}: {e}", st.line);
            if !self.ready(st).map_err(line)? {
                break;
            }
            self.statement(st).map_err(line)?;
            self.p.pc += 1;
            moved = true;
        }
        Ok(moved)
    }

    fn local(&self, t: &Term) -> R<Unit> {
        Ok(self.m.in_mesh(self.p.mesh, unit(t)?))
    }

    fn sent(&self, u: Unit) -> u64 {
        match u {
            Unit::Vc(i) => self.m.vc[i].sent,
            Unit::Mg(i) => self.m.mg[i].sent,
            Unit::Mover => self.m.moves_sent,
        }
    }

    fn done(&self, u: Unit) -> u64 {
        match u {
            Unit::Vc(i) => self.m.vc[i].done,
            Unit::Mg(i) => self.m.mg[i].done,
            Unit::Mover => self.m.moves_done,
        }
    }

    /// Whether `st` can run now; a statement that waits runs once it can.
    fn ready(&self, st: &Stmt) -> R<bool> {
        if self.m.moves_done < self.p.moving {
            return Ok(false);
        }
        let pos = st.positional();
        match st.op.as_str() {
            "wait" if pos[0].as_name() == Some("slot") => {
                let slot = int(pos[1], &self.env)?;
                let n = st.kw("n").map_or(Ok(1), |t| int(t, &self.env))?;
                Ok(self.rings.get(&(self.p.mesh, slot)).copied().unwrap_or(0) >= n)
            }
            "wait" => {
                let (u, upto) = self.awaited(st, &pos)?;
                Ok(self.done(u) >= upto)
            }
            "barrier" => Ok(self.m.mesh_idle(self.p.mesh)),
            "move" if pos.is_empty() => Ok(self.m.mesh_idle(self.p.mesh)),
            _ => Ok(true),
        }
    }

    fn statement(&mut self, st: &Stmt) -> R<()> {
        let pos = st.positional();
        match st.op.as_str() {
            "send" => self.send(st, unit(pos[0])?),
            "mark" => {
                let u = self.local(pos[0])?;
                let at = self.sent(u);
                let name = st.target_name().ok_or("t = mark UNIT")?.to_string();
                self.p.marks.insert(name, (u, at));
                Ok(())
            }
            "wait" => {
                if pos[0].as_name() == Some("slot")
                    && pos.iter().any(|p| p.as_name() == Some("consume"))
                {
                    let slot = int(pos[1], &self.env)?;
                    let n = st.kw("n").map_or(Ok(1), |t| int(t, &self.env))?;
                    *self.rings.entry((self.p.mesh, slot)).or_default() -= n;
                }
                Ok(())
            }
            "ring" => {
                let mesh = int(st.kw("mesh").ok_or("ring mesh=")?, &self.env)? as usize;
                let slot = int(st.kw("slot").ok_or("ring slot=")?, &self.env)?;
                let amount = st.kw("amount").map_or(Ok(1), |t| int(t, &self.env))?;
                *self.rings.entry((mesh, slot)).or_default() += amount;
                Ok(())
            }
            "barrier" => Ok(()),
            "move" => self.move_(st, &pos),
            "load" => {
                let v = self.m.mem.u64(int(pos[0], &self.env)?)?;
                let name = st.target_name().ok_or("rN = load ADDR")?.to_string();
                self.p.regs.insert(name, v);
                Ok(())
            }
            "assert_zero" => {
                let r = pos[0].as_name().ok_or("assert_zero rN")?;
                match self.p.regs.get(r) {
                    Some(0) => Ok(()),
                    v => Err(format!("{r} = {v:?} fails its assertion")),
                }
            }
            op => Err(format!("no node statement {op} in this simulator")),
        }
    }

    /// The unit and count a `wait` holds for.
    fn awaited(&self, st: &Stmt, pos: &[&Term]) -> R<(Unit, u64)> {
        if pos[0].as_name() == Some("moves") {
            let name = pos[1].as_name().ok_or("wait moves T")?;
            let upto = *self
                .p
                .moves
                .get(name)
                .ok_or(format!("no posted move {name}"))?;
            return Ok((Unit::Mover, upto));
        }
        let u = self.local(pos[0])?;
        let upto = match st.kw("upto") {
            None => self.sent(u),
            Some(t) => {
                let name = t.as_name().ok_or("upto=T")?;
                let (of, at) = *self.p.marks.get(name).ok_or(format!("no mark {name}"))?;
                if of != u {
                    return Err(format!("{name} marks {of:?}, not {u:?}"));
                }
                at
            }
        };
        Ok((u, upto))
    }

    fn image(&mut self, name: &str, args: Vec<i64>) -> R<Image> {
        let key = (name.to_string(), args);
        if let Some(i) = self.images.get(&key) {
            return Ok(i.clone());
        }
        let def = self
            .module
            .images
            .get(name)
            .ok_or(format!("no image {name}"))?;
        let (descs, insts) = image::read(def, &key.1).map_err(|e| format!("{name}: {e}"))?;
        let got = (descs, Rc::new(insts));
        self.images.insert(key, got.clone());
        Ok(got)
    }

    fn send(&mut self, st: &Stmt, u: Unit) -> R<()> {
        match self.m.in_mesh(self.p.mesh, u) {
            Unit::Vc(i) => self.send_vc(st, i),
            Unit::Mg(i) => st.body.iter().try_for_each(|s| self.send_mg(s, i)),
            Unit::Mover => unreachable!("no send names the mover"),
        }
    }

    /// A send's words, `repeats` times with each `advances` descriptor base
    /// stepped each repetition (L1.VC.010).
    fn send_vc(&mut self, st: &Stmt, i: usize) -> R<()> {
        if st.kw("increments").is_some() {
            return Err("send increments= in this simulator".into());
        }
        let repeats = st.kw("repeats").map_or(Ok(1), |t| int(t, &self.env))?;
        let mut steps: HashMap<u32, i64> = HashMap::new();
        if let Some(Term::Tuple(items)) = st.kw("advances") {
            for t in items {
                let (ad, s) = super::term::pair(t, &self.env)?;
                steps.insert(ad as u32, s);
            }
        }
        for r in 0..repeats {
            for s in &st.body {
                match s.op.as_str() {
                    "desc" => {
                        let (ad, mut d) = image::desc(s, &self.env)?;
                        d.base += r * steps.get(&ad).copied().unwrap_or(0);
                        self.m.send_vc(i, VcWord::Desc(ad, d))?;
                    }
                    "run" => {
                        let Some(Term::Call(name, args)) = s.positional().first().copied() else {
                            return Err("run IMAGE(args)".into());
                        };
                        let args: Vec<i64> =
                            args.iter().map(|a| int(a, &self.env)).collect::<R<_>>()?;
                        let (descs, prog) = self.image(name, args)?;
                        for (ad, mut d) in descs {
                            d.base += r * steps.get(&ad).copied().unwrap_or(0);
                            self.m.send_vc(i, VcWord::Desc(ad, d))?;
                        }
                        self.m.send_vc(i, VcWord::Run(prog))?;
                    }
                    op => return Err(format!("a core's send holds desc and run, not {op}")),
                }
            }
        }
        Ok(())
    }

    fn send_mg(&mut self, s: &Stmt, i: usize) -> R<()> {
        let pos = s.positional();
        let opt = |k: &str, default: i64| s.kw(k).map_or(Ok(default), |t| int(t, &self.env));
        let flag = |n: &str| pos.iter().any(|p| p.as_name() == Some(n));
        let word = match s.op.as_str() {
            "fill" => {
                let bank = pos[0].as_name().ok_or("fill SIDE")?;
                let (b, b_) = match bank {
                    "A0" => (false, 0),
                    "A1" => (false, 1),
                    "B0" => (true, 0),
                    "B1" => (true, 1),
                    _ => return Err(format!("bank {bank}")),
                };
                if flag("recv") {
                    let n = opt("n", 0)? as usize;
                    return self.m.send_mg(i, cluster::Word::FillRecv { b, n });
                }
                cluster::Word::Fill {
                    b,
                    bank: b_,
                    eoff: opt("at", 0)? as usize,
                    addr: int(pos[2], &self.env)?,
                    n: opt("n", 0)? as usize,
                }
            }
            "gemm" => {
                let Term::Dims(d) = pos[0] else {
                    return Err("gemm GMxGNxNK".into());
                };
                let bank = |k: &str, side: &str| match s.kw(k).and_then(Term::as_name) {
                    None => Ok(0),
                    Some(n) if n == format!("{side}0") => Ok(0),
                    Some(n) if n == format!("{side}1") => Ok(1),
                    Some(n) => Err(format!("bank {n}")),
                };
                if s.kw("to").is_some() {
                    return Err("an emit to a unit in this simulator".into());
                }
                cluster::Word::Gemm {
                    gm: d[0] as usize,
                    gn: d[1] as usize,
                    nk: d[2] as usize,
                    abank: bank("a", "A")?,
                    bbank: bank("b", "B")?,
                    aoff: opt("aoff", 0)? as usize,
                    boff: opt("boff", 0)? as usize,
                    acc: opt("acc", 0)? != 0,
                    anchor: opt("anchor", 40)?,
                    emit: s.kw("emit").map(|t| int(t, &self.env)).transpose()?,
                }
            }
            "drain" if s.kw("to").is_some() => cluster::Word::DrainTo {
                to: image::target(s.kw("to").ok_or("drain to=")?)?,
                buf: opt("buf", 0)? as u32,
                at: opt("l1", 0)? as usize,
                n: opt("n", 0)? as usize,
                count: flag("count"),
            },
            "drain" if flag("fused") => cluster::Word::Fused,
            "drain" => cluster::Word::Drain {
                addr: int(pos[0], &self.env)?,
                n: opt("n", 0)? as usize,
            },
            op => return Err(format!("a cluster word {op} in this simulator")),
        };
        self.m.send_mg(i, word)
    }

    fn walk(&self, t: &Term) -> R<Walk> {
        let Term::Call(n, args) = t else {
            return Err(format!("wanted a walk, got {}", fmt(t)));
        };
        if n != "w" {
            return Err(format!("wanted a walk, got {}", fmt(t)));
        }
        let mut plain = args.iter().filter(|a| !matches!(a, Term::Kw(..)));
        let base = int(plain.next().ok_or("a walk's base")?, &self.env)?;
        let mut levels = Vec::new();
        for d in plain {
            let Term::Tuple(items) = d else {
                return Err(format!("level {}", fmt(d)));
            };
            let v: Vec<i64> = items.iter().map(|x| int(x, &self.env)).collect::<R<_>>()?;
            levels.push(Level {
                count: v[0],
                stride: v[1],
                axis: v.get(2).copied().unwrap_or(0),
                astep: v.get(3).copied().unwrap_or(0),
            });
        }
        let mut windows = [(0, 0); 2];
        for a in args {
            if let Term::Kw(k, v) = a {
                let slot = usize::from(k == "win2");
                windows[slot] = super::term::pair(v, &self.env)?;
            }
        }
        Ok(Walk {
            base,
            levels,
            windows,
        })
    }

    fn mover_op(&self, s: &Stmt) -> R<Move> {
        let all = s.positional();
        let flag = |n: &str| all.iter().any(|p| p.as_name() == Some(n));
        let pos: Vec<&Term> = all
            .iter()
            .copied()
            .filter(|p| p.as_name().is_none())
            .collect();
        let e = &self.env;
        let ew = s.kw("ew").map_or(Ok(32), |t| int(t, e))? as u32;
        Ok(match s.op.as_str() {
            "copy" => Move::Copy {
                src: self.walk(pos[0])?,
                dst: self.walk(pos[2])?,
                pad: s.kw("pad").map_or(Ok(0), |t| int(t, e))? as u64,
                ew,
            },
            "fill" => Move::Fill {
                dst: self.walk(pos[0])?,
                value: s.kw("value").map_or(Ok(0), |t| int(t, e))? as u64,
                ew,
            },
            "gen" => Move::Gen {
                dst: self.walk(pos[0])?,
                seed: s.kw("seed").map_or(Ok(0), |t| int(t, e))? as u64,
            },
            "quant" => Move::Quant {
                src: self.walk(pos[0])?,
                dst: self.walk(pos[2])?,
                b: flag("bpack"),
            },
            "gt4" => Move::Gt4 {
                src: self.walk(pos[0])?,
                dst: self.walk(pos[2])?,
            },
            "gather" | "scatter" => {
                let index = Index {
                    at: int(s.kw("index").ok_or("index=")?, e)?,
                    n: int(s.kw("n").ok_or("n=")?, e)?,
                    pitch: int(s.kw("pitch").ok_or("pitch=")?, e)?,
                    words: int(s.kw("words").ok_or("words=")?, e)?,
                    fp: flag("fp"),
                };
                if s.op == "gather" {
                    Move::Gather {
                        src: int(pos[0], e)?,
                        dst: self.walk(pos[2])?,
                        index,
                    }
                } else {
                    Move::Scatter {
                        src: self.walk(pos[0])?,
                        dst: int(pos[2], e)?,
                        index,
                    }
                }
            }
            op => return Err(format!("a mover op {op} in this simulator")),
        })
    }

    /// A blocking move waits with every unit (L1.NODE.019), a posted one
    /// names its last move for `wait moves` (L1.NODE.020).
    fn move_(&mut self, st: &Stmt, pos: &[&Term]) -> R<()> {
        for s in &st.body {
            let m = self.mover_op(s)?;
            self.m.post_move(m);
        }
        if pos.is_empty() {
            self.p.moving = self.m.moves_sent;
            return Ok(());
        }
        let name = st.target_name().ok_or("t = move posted")?.to_string();
        self.p.moves.insert(name, self.m.moves_sent);
        Ok(())
    }
}
