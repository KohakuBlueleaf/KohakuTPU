//! An `fn NAME.l1` body to L1 programs (L1.BODY.*, L1.NODE.*, L1.MG.*,
//! L1.VC.*, L1.MOV.*): the node's statements with each unit's words.
//!
//! Parameters are buffers bound to addresses, read as constants. A field
//! written `rN` or `C + rN` takes its value from register N when the node
//! reaches the word (a PATCH); `node N` blocks are one program per node.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use indexmap::IndexMap;
use ktpu_isa::unit::cluster::{
    ClusterInst, Drain, Fill, Gemm, Post, BUF_L1A, DFLAG_COUNT, DFLAG_SIGNAL,
};
use ktpu_isa::unit::mover::{
    xform_id, Level, Move, MoveMode, MoverOp, Walk, R_DIM, R_HDR, R_IMM, R_INDEX, R_ROW, R_SEED,
    W16, W32, W8, XF_GT4, XF_QUANT,
};
use ktpu_isa::unit::vector::{VecCu, VecIsa};
use ktpu_isa::{Coord, Isa, UnitKind};
use ktpu_l0::{Alu, Cond, Patch, Program, Stmt as PStmt, Word, WordOps};

use super::image::{self, Descs, ImageWords, Images, State};
use super::sru::RowReader;
use super::target::{MeshUnits, Target, Xy};
use crate::error::{Error, Result};
use crate::layout::buffer::Buffer;
use crate::mutant::on;
use crate::text::{fmt, Env, Expander, Module, Reader, Stmt, Term, Val};

mod cluster;
mod control;
mod mover;
mod programs;
mod send;

pub use programs::*;

/// The node processor's coordinate, a mesh corner.
pub const NODE: Xy = (0, 0);
/// Flit types a node sends raw.
pub const CU_CTRL: u64 = 0x7;
/// CU_DATA descriptor fields, as bit positions.
const CD_BUF: u32 = 248;
const CD_OFF: u32 = 232;
const CD_LEN: u32 = 224;
const CD_FLAGS: u32 = 216;
const CD_ACK_Y: u32 = 212;
const CD_ACK_X: u32 = 208;
/// A CU_CTRL request's index.
const CTRL_INDEX: u32 = 240;

const ALU: [(&str, Alu); 13] = [
    ("set", Alu::Mov),
    ("add", Alu::Add),
    ("sub", Alu::Sub),
    ("mul", Alu::Mul),
    ("and", Alu::And),
    ("or", Alu::Or),
    ("xor", Alu::Xor),
    ("shl", Alu::Shl),
    ("shr", Alu::Shr),
    ("min", Alu::Min),
    ("max", Alu::Max),
    ("divu", Alu::Divu),
    ("remu", Alu::Remu),
];

/// Control-region offsets by name.
const CTRL: [(&str, u64); 13] = [
    ("dbell", 0x10),
    ("mvstat", 0x20),
    ("dbcnt", 0x28),
    ("irq", 0x38),
    ("il_ctl", 0xC0),
    ("il_mesh", 0xC8),
    ("il_ring", 0xD0),
    ("il_consume", 0xE0),
    ("sig", 0xE0),
    ("il_select", 0xE8),
    ("dcache", 0x1C8),
    ("xf_sel", 0x1D0),
    ("xf_data", 0x1D8),
];

const MASK64: i128 = (1i128 << 64) - 1;

fn alu_of(op: &str) -> Option<Alu> {
    if on("l1.node/alu-table") {
        match op {
            "add" => return Some(Alu::Sub),
            "mul" => return Some(Alu::Add),
            _ => {}
        }
    }
    ALU.iter().find(|(n, _)| *n == op).map(|(_, a)| *a)
}

/// The node processor's coordinate as the reader uses it.
fn node_xy() -> Xy {
    if on("l1.mg/node-coord") {
        (0, 1)
    } else {
        NODE
    }
}

/// `op` as the cluster encoder is called with it: a mutant pins a field.
fn encoded_as(op: &ClusterInst) -> ClusterInst {
    let mut op = op.clone();
    match &mut op {
        ClusterInst::Gemm(g) if on("l1.mg/gemm-no-acc") => g.acc = false,
        ClusterInst::Gemm(g) if on("l1.mg/gemm-anchor") => g.anchor = 0,
        ClusterInst::Drain(d) if on("l1.mg/no-fuse") => d.fuse = false,
        ClusterInst::Fill(f) if on("l1.mg/fill-no-recv") => f.recv = false,
        _ => {}
    }
    op
}

fn coord(xy: Xy) -> Coord {
    Coord(xy.0, xy.1)
}

fn word(w: ktpu_isa::W256) -> Word {
    Word(w.0)
}

/// A token a statement binds.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Token {
    Mark(usize),
    Moves(usize),
}

/// A fused drain's open emit: address, sub-tiles, sink fields, the gemm.
type Emit = (u64, u32, Vec<(&'static str, i64)>, Stmt);
/// Mover register writes.
type Writes = Vec<(u64, u64)>;
/// Mover fields set from registers: `(field, register, constant)`.
type MoverFix = Vec<(String, u64, i128)>;
/// Descriptor bases written with a register, by `(descriptor, field)`.
type HeldPatches = BTreeMap<(u32, u32), Patch>;

pub struct NodeReader<'a> {
    pub module: &'a Module,
    pub r: Reader,
    pub p: Program,
    pub target: &'a Target<'a>,
    pub isa: &'a Isa,
    images: Images<'a>,
    tokens: HashMap<String, Token>,
    /// cluster -> emits not yet collected.
    emits: IndexMap<Xy, Vec<Emit>>,
    /// core -> the encoded image its IMEM holds, along straight-line code.
    resident: HashMap<Xy, ImageWords>,
    /// row unit -> the configuration words this program wrote it.
    pub configs: HashMap<Xy, BTreeMap<u32, u32>>,
    /// The scratch register patches compute in; a program names those below.
    pub tmp: u64,
}

impl<'a> NodeReader<'a> {
    pub fn new(
        module: &'a Module,
        target: &'a Target<'a>,
        units: &'a MeshUnits<'a>,
        isa: &'a Isa,
        entry: State,
        spans: BTreeMap<u64, u64>,
    ) -> Result<NodeReader<'a>> {
        let mut p = Program::new();
        p.spans = spans;
        Ok(NodeReader {
            module,
            r: module.reader(),
            p,
            target,
            isa,
            images: Images::new(module, target.machine, entry, Some(units))?,
            tokens: HashMap::new(),
            emits: IndexMap::new(),
            resident: HashMap::new(),
            configs: HashMap::new(),
            tmp: target.machine.fw.package.regs as u64 - 1,
        })
    }

    pub fn fail(&self, st: &Stmt, clause: &str, msg: impl Into<String>) -> Error {
        self.r.fail(st, clause, msg)
    }

    fn at(&self, st: &Stmt, e: impl Into<Error>) -> Error {
        let e: Error = e.into();
        if e.line > 0 {
            return e;
        }
        self.r.fail(st, &e.clause, e.message)
    }

    pub fn stmts(&mut self, stmts: &[Stmt]) -> Result<()> {
        for st in stmts {
            self.stmt(st)?;
        }
        Ok(())
    }

    pub fn stmt(&mut self, st: &Stmt) -> Result<()> {
        let op = st.op.as_str();
        let binds = matches!(op, "mark" | "move" | "load" | "ctrl") || alu_of(op).is_some();
        if st.target.is_some() && !binds {
            return Err(self.fail(st, "L1.NODE.021", format!("`{op}` binds no name")));
        }
        if !st.body.is_empty() && op != "send" && op != "move" {
            return Err(self.fail(st, "L1.NODE.021", format!("`{op}` takes no block")));
        }
        if st.target.is_some() && (alu_of(op).is_some() || op == "load" || op == "ctrl") {
            return self.register_op(st);
        }
        match op {
            "send" => self.send(st),
            "move" => self.move_(st),
            "mark" => {
                let Some(name) = st.target_name() else {
                    return Err(self.fail(st, "L1.NODE.034", "t = mark UNIT"));
                };
                let t = self.one(st)?;
                let c = self.unit(st, t)?;
                let token = self.p.mark(c) + on("l1.node/mark-token") as usize;
                self.tokens.insert(name.to_string(), Token::Mark(token));
                Ok(())
            }
            "wait" => self.wait(st),
            "barrier" => {
                if !st.args.is_empty() {
                    return Err(self.fail(st, "L1.NODE.018", "barrier takes nothing"));
                }
                if !on("l1.node/no-barrier") {
                    self.p.barrier();
                }
                Ok(())
            }
            "fetch" => self.fetch(st),
            "ring" => self.ring(st),
            "store" | "ctrl" => self.write(st),
            "assert_zero" => {
                let pos = st.positional();
                if pos.len() != 1 || st.kw("code").is_none() {
                    return Err(self.fail(st, "L1.NODE.040", "assert_zero rN code=U32"));
                }
                let reg = self.reg(st, pos[0])?;
                let code = self.const_(st, st.kw("code").unwrap())?;
                if !(0..=u32::MAX as i128).contains(&code) {
                    return Err(self.fail(st, "L1.NODE.040", "assert_zero code is U32"));
                }
                self.p.steps.push(PStmt::AssertZero {
                    reg,
                    code: code as u64,
                });
                Ok(())
            }
            "signal" => {
                let pos = st.positional();
                if !(1..=2).contains(&pos.len()) {
                    return Err(self.fail(st, "L1.NODE.031", "signal V32 [V64]"));
                }
                let v32 = self.const_(st, pos[0])? as u64;
                let v64 = match pos.get(1) {
                    Some(t) => self.const_(st, t)? as u64,
                    None => 0,
                };
                if on("l1.node/signal-swapped") {
                    self.p.signal(v64, v32);
                } else {
                    self.p.signal(v32, v64);
                }
                Ok(())
            }
            "settle" => {
                let t = self.one(st)?;
                let n = self.const_(st, t)? as u64;
                self.p.settle(n);
                Ok(())
            }
            "label" => {
                self.resident.clear();
                self.configs.clear();
                let t = self.one(st)?;
                let name = self.name(st, t)?.to_string();
                let got = self.p.label(&name).map(|_| ());
                got.map_err(|e| self.at(st, ref_err(e)))
            }
            "goto" => {
                self.resident.clear();
                self.configs.clear();
                self.goto(st)
            }
            "push" => self.push(st),
            "recv" => self.recv(st),
            "query" => self.query(st),
            _ => Err(self.fail(st, "L1.NODE.022", format!("no L1 node statement '{op}'"))),
        }
    }

    // ------------------------------------------------------------- values
    fn is_reg(t: &Term) -> bool {
        matches!(t, Term::Name(n) if n.len() > 1 && n.starts_with('r') && n[1..].chars().all(|c| c.is_ascii_digit()))
    }

    /// `rN` -> N; the scratch register is not the program's.
    pub fn reg(&self, st: &Stmt, t: &Term) -> Result<u64> {
        if Self::is_reg(t) {
            let n: u64 = t.as_name().unwrap()[1..].parse().unwrap_or(u64::MAX);
            if n < self.tmp {
                return Ok(n ^ on("l1.node/reg-off-by-one") as u64);
            }
        }
        Err(self.fail(
            st,
            "L1.NODE.023",
            format!("wanted a register r0..r{}, got {}", self.tmp - 1, fmt(t)),
        ))
    }

    /// `(constant, register)` of a field written `C`, `rN` or `C + rN`.
    pub fn val(&self, st: &Stmt, t: &Term) -> Result<(i128, Option<u64>)> {
        if let (Term::Offset(_, off), true) = (t, on("l1.node/val-const-only")) {
            if let Term::Int(c, _) = off.as_ref() {
                return Ok((*c, None));
            }
        }
        if let Term::Int(v, _) = t {
            return Ok((*v, None));
        }
        if Self::is_reg(t) {
            return Ok((0, Some(self.reg(st, t)?)));
        }
        if let Term::Offset(base, off) = t {
            if let (true, Term::Int(c, _)) = (Self::is_reg(base), off.as_ref()) {
                return Ok((*c, Some(self.reg(st, base)?)));
            }
        }
        Err(self.fail(
            st,
            "L1.NODE.024",
            format!(
                "wanted a constant, rN or C + rN (compute others with ALU ops), got {}",
                fmt(t)
            ),
        ))
    }

    fn name<'t>(&self, st: &Stmt, t: &'t Term) -> Result<&'t str> {
        t.as_name()
            .ok_or_else(|| self.fail(st, "L1.NODE.008", format!("wanted a name, got {}", fmt(t))))
    }

    pub fn const_(&self, st: &Stmt, t: &Term) -> Result<i128> {
        t.as_int().ok_or_else(|| {
            self.fail(
                st,
                "L1.BODY.001",
                format!("wanted a constant (parameters are bound), got {}", fmt(t)),
            )
        })
    }

    fn kconst(&self, st: &Stmt, key: &str, default: i128) -> Result<i128> {
        match st.kw(key) {
            Some(t) => self.const_(st, t),
            None => Ok(default),
        }
    }

    fn one<'s>(&self, st: &'s Stmt) -> Result<&'s Term> {
        let pos = st.positional();
        if pos.len() != 1 {
            return Err(self.fail(st, "L1.NODE.002", format!("`{}` takes one operand", st.op)));
        }
        Ok(pos[0])
    }

    // ---------------------------------------------------------- registers
    fn port(&self, st: &Stmt, mesh: u32) -> Result<Xy> {
        if on("l1.mg/port") {
            return Ok((0, 2));
        }
        let ports = self.target.mem_ports(mesh).map_err(|e| self.at(st, e))?;
        ports.first().copied().ok_or_else(|| {
            self.fail(
                st,
                "L1.MG.019",
                format!("mesh {mesh} names no memory port to cross by"),
            )
        })
    }

    /// Every emitting gemm was collected (L1.MG.021).
    pub fn finish(&self) -> Result<()> {
        for pending in self.emits.values() {
            if let Some((_, _, _, st)) = pending.first() {
                return Err(self.fail(st, "L1.MG.021", "an emitting gemm no fused drain collects"));
            }
        }
        Ok(())
    }

    fn side(&self, st: &Stmt, t: &Term) -> Result<(u32, u32)> {
        if let Some(n) = t.as_name() {
            let b = n.as_bytes();
            if b.len() == 2 && (b[0] == b'A' || b[0] == b'B') && (b[1] == b'0' || b[1] == b'1') {
                let bank = (b[1] - b'0') as u32 ^ on("l1.mg/side") as u32;
                return Ok(((b[0] == b'B') as u32, bank));
            }
        }
        Err(self.fail(
            st,
            "L1.MG.015",
            format!("wanted an L1 side and bank A0 A1 B0 B1, got {}", fmt(t)),
        ))
    }

    fn coord(&self, st: &Stmt, t: &Term) -> Result<Xy> {
        if let Term::Tuple(items) = t {
            if items.len() == 2 {
                return Ok((
                    self.const_(st, &items[0])? as u32,
                    self.const_(st, &items[1])? as u32,
                ));
            }
        }
        Ok(self.resolve(st, t)?.1)
    }

    /// `(mesh, coord)` of a unit the node text names.
    pub(super) fn resolve(&self, st: &Stmt, t: &Term) -> Result<(u32, Xy)> {
        let (mesh, (x, y)) = self.target.resolve(&self.r, st, t)?;
        Ok(match on("isa.cluster/units") {
            true => (mesh, (y, x)),
            false => (mesh, (x, y)),
        })
    }

    // ------------------------------------------------------------- mover
    /// A unit of this program's mesh (L1.UNIT.004).
    pub fn unit(&self, st: &Stmt, t: &Term) -> Result<Xy> {
        let (mesh, c) = self.resolve(st, t)?;
        if mesh != self.target.mesh {
            return Err(self.fail(
                st,
                "L1.UNIT.004",
                format!(
                    "{} is in mesh {mesh}: a node sends only into its own (H1)",
                    fmt(t)
                ),
            ));
        }
        Ok(c)
    }
}

fn ref_err(e: ktpu_l0::Refusal) -> Error {
    Error::new(e.clause, e.msg)
}
