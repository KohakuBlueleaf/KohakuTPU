//! The band lowering of one vector instance (L2.VC.003-.016, L2.OP,
//! L2.STAT): descriptors and L1 buffers, constants, the prologue, and each
//! stream-loop iteration's unpacks, math, reductions and packs.
//!
//! `ctx` holds the instance's state and its memory views; `prepare` makes
//! the descriptors, `emit` the constants, prologue and iteration order,
//! `unpack`, `math`, `reduce` and `pack` a band's ops, `band` the whole
//! image.

pub(super) mod band;
mod emit;
mod math;
mod pack;
mod prepare;
mod reduce;
mod unpack;

mod modulo_typed;

#[cfg(feature = "witness")]
use super::indent;
#[cfg(feature = "witness")]
pub use band::image_text;
#[cfg(feature = "witness")]
pub use band::lower_band;

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;
use ktpu_l1::text::{Env, Stmt, Term};

#[cfg(feature = "witness")]
use super::gen::{Code, Gen};
#[cfg(feature = "witness")]
use super::tree::{compact, interleave, quantiser_order, tree, tree4, Steps};
use super::{
    banks, code_of, count_of, domain_constants, is_in, kreg, mode_of, number, numbers, ops_of,
    value_name, Desc, FKey, Plan, PlanX, RPar, VErr, VResult, Val, Walk, BINARY, CMP, EXP2_FLOOR,
    EXP2_RANGE, REDUCE, TERNARY, UNARY, XLANE,
};
use crate::ir::read::bind;
use crate::ir::repr::repr;
use crate::ir::{span, value, Body, For, Node, Op, Type};
use crate::lower::memory as M;
use crate::lower::sync::{mem, Access};
use crate::mutant::on;
use crate::target::Target;

/// How a view lies in memory.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    /// a row-major run of whole rows
    Plain,
    /// bands of 4 x 4 sub-tiles of a `tile=` buffer
    Tile,
    /// an MXFP7 buffer
    Mx7,
    /// a band of a row-major f16 buffer unpacked as GT4 registers
    Rows,
}

#[derive(Clone)]
pub struct Ctx<'a> {
    pub body: &'a Body,
    pub t: &'a Target,
    pub env: Env,
    pub lp: Option<For>,
    pub trips: Vec<i64>,
    pub classed: bool,
    pub carries: IndexMap<String, (String, bool)>,
    pub descs: Vec<Desc>,
    pub loads: Vec<usize>,
    pub of: HashMap<usize, usize>,
    pub top: i64,
    pub vals: IndexMap<String, Val>,
    pub scalars: HashMap<FKey, String>,
    /// constants past the scalar file -> their pinned registers (`hold`)
    pub held: HashMap<FKey, String>,
    pub hold: bool,
    /// row values unpacked before the loop held in pinned registers, else
    /// unpacked again from L1 where read
    pub pin: bool,
    /// staging register -> (the constant it holds, the code index after its seti)
    pub staged: IndexMap<String, (FKey, usize)>,
    pub pinned: HashSet<String>,
    pub ahead: bool,
    pub band: usize,
    pub remat: bool,
    pub down: i64,
    pub land: i64,
    pub plan: Plan,
    pub unit: String,
    pub pre: Vec<Arc<Node>>,
    /// the loop's banded unpack: (op uid, value, first register, registers a trip)
    pub rel: Option<(usize, String, i64, i64)>,
    pub inplace: bool,
    /// GT4 rows: the registers of a 4-row band (0: rows are registers)
    pub per_band: i64,
    pub tile: i64,
}

fn op(n: &Node) -> &Op {
    super::op_of(n)
}

fn ty(o: &Op) -> &Type {
    o.ty.as_ref().unwrap()
}

/// The stage registers a constant past the scalar file is set into.
fn stage_regs(t: &Target) -> [String; 2] {
    [format!("s{}", t.stage_reg()), "k3".to_string()]
}

impl<'a> Ctx<'a> {
    pub fn new(body: &'a Body, t: &'a Target, env: &Env, lp: Option<For>) -> VResult<Ctx<'a>> {
        let trips: Vec<i64> = match &lp {
            Some(f) => span(body, &f.st, &f.lo, &f.hi, env)?,
            None => vec![0],
        };
        Ok(Ctx {
            body,
            t,
            env: env.clone(),
            lp,
            trips,
            classed: false,
            carries: IndexMap::new(),
            descs: Vec::new(),
            loads: Vec::new(),
            of: HashMap::new(),
            top: 0,
            vals: IndexMap::new(),
            scalars: HashMap::new(),
            held: HashMap::new(),
            hold: true,
            pin: true,
            staged: IndexMap::new(),
            pinned: HashSet::new(),
            ahead: true,
            band: 1,
            remat: false,
            down: t.l1_words,
            land: t.l1_words,
            plan: Plan::new(),
            unit: String::new(),
            pre: Vec::new(),
            rel: None,
            inplace: false,
            per_band: 0,
            tile: 1,
        })
    }

    fn fail(&self, st: &Stmt, clause: &'static str, msg: impl Into<String>) -> VErr {
        VErr::Fail(self.body.reader.fail(st, clause, msg))
    }

    /// Which of `v`'s registers row register `r` of the loop reads.
    pub fn index(&self, v: &Val, r: i64) -> VResult<i64> {
        if v.rows >= self.tile {
            return Ok(r);
        }
        if self.per_band != 0 && v.cols == 32 * self.per_band && v.rows == self.per_band {
            return Ok(r % self.per_band);
        }
        if self.per_band != 0 && v.rows * self.per_band == self.tile {
            return Ok(r / self.per_band);
        }
        if v.rows == 1 {
            return Ok(0);
        }
        if !on("l2.vector/no-line-mask-cycle")
            && v.cols > self.t.vlmax
            && v.rows * self.t.vlmax == v.cols
            && self.tile % v.rows == 0
        {
            return Ok(r % v.rows);
        }
        Err(VErr::Value(format!(
            "a {}-register value in a {}-register loop",
            v.rows, self.tile
        )))
    }

    /// `[(row, register)]`: each of `v`'s registers `rows` covers, at the
    /// first row reading it.
    pub fn once(&self, v: &Val, rows: &[i64]) -> VResult<Vec<(i64, i64)>> {
        let mut out = Vec::new();
        let mut seen = HashSet::new();
        for &r in rows {
            let i = self.index(v, r)?;
            if seen.insert(i) {
                out.push((r, i));
            }
        }
        Ok(out)
    }

    pub fn accesses(&self) -> Vec<Access> {
        let u = self.unit.as_str();
        let trips = self.trips.len() as i64;
        if self.classed {
            return self
                .descs
                .iter()
                .filter_map(|d| {
                    d.buffer
                        .as_ref()
                        .map(|name| mem(u, d.kind == "store", name, 0, i64::MAX))
                })
                .collect();
        }
        let mut out = Vec::new();
        for d in &self.descs {
            if !d.to.is_empty() {
                let PlanX::T(x) = &self.plan[&d.kind_op] else {
                    continue;
                };
                let side = if d.side == 0 { "A" } else { "B" };
                if x.flow {
                    out.push(
                        Access::new(u, true, (&d.to, side), x.at, x.at + x.size).flowing(true),
                    );
                    continue;
                }
                out.push(Access::new(u, true, (&d.to, side), x.at, x.at + x.size));
                out.push(Access::new(u, true, (&d.to, "rx"), 0, 1));
                continue;
            }
            let (lo, hi) = d.span(if d.inc != 0 { trips } else { 1 });
            out.push(mem(
                u,
                d.kind == "store",
                d.buffer.as_deref().unwrap(),
                lo,
                hi,
            ));
        }
        for x in self.plan.values() {
            match x {
                PlanX::S(s) => {
                    let lo = *s.slots.iter().min().unwrap();
                    let hi = *s.slots.iter().max().unwrap() + s.size;
                    out.push(Access::new(u, false, (u, "l1"), lo, hi).flowing(true));
                }
                PlanX::T(x) if x.dst == u => {
                    out.push(Access::new(u, false, (u, "l1"), x.at, x.at + x.size));
                }
                _ => {}
            }
        }
        out.push(Access::new(u, true, (u, "l1"), 0, self.top.max(1)));
        if self.down < self.land {
            out.push(Access::new(u, true, (u, "l1"), self.down, self.land));
        }
        out
    }

    // ---------------------------------------------------------------- memory

    /// `(buffer, byte offset, walk dimensions, words, kind, rows_w, view rows)`.
    fn walk(
        &self,
        o: &Op,
        view: &Term,
        at: Option<i64>,
        rows: bool,
    ) -> VResult<(String, i64, Walk, i64, Kind, i64, i64)> {
        let mut env = self.env.clone();
        if let (Some(lp), Some(at)) = (&self.lp, at) {
            env = bind(&env, &lp.var, at);
        }
        let v = match value(self.body, &o.st, view, &env)? {
            Term::Name(n) => Term::View(n, vec![]),
            other => other,
        };
        let name = value_name(&v).unwrap_or("").to_string();
        let Some(buf) = self.body.buffers.get(&name) else {
            return Err(self.fail(&o.st, "L2.VC.003", format!("no buffer {name}")));
        };
        let vf = |e: String| self.fail(&o.st, "L2.VC.003", e);
        let ax = M::axes_of(&v, &buf.shape).map_err(vf)?;
        if buf.has("tile") {
            let (off, walk, words) = super::tiled(buf, &ax, self.t).map_err(vf)?;
            return Ok((name, off, walk, words, Kind::Tile, 0, 0));
        }
        if buf.is_mx7() {
            let (off, walk, words) = super::mx7(buf, &ax, self.t).map_err(vf)?;
            return Ok((name, off, walk, words, Kind::Mx7, 0, 0));
        }
        let (off, dims, item) = M::rowmajor_walk(buf, &ax).map_err(vf)?;
        if rows {
            let (walk, words, rows_w, r) = self.rows_walk(o, &ax, &dims, item)?;
            return Ok((name, off, walk, words, Kind::Rows, rows_w, r));
        }
        let nbytes = item * dims.iter().map(|d| d.0).product::<i64>();
        if (item != 2 && item != 4) || !super::contiguous(&dims, item) || nbytes % 32 != 0 {
            return Err(self.fail(
                &o.st,
                "L2.VC.003",
                "a row-major load or store view is contiguous f16 or f32, whole words",
            ));
        }
        Ok((
            name,
            off,
            Walk::flat(32, nbytes / 32),
            nbytes / 32,
            Kind::Plain,
            0,
            0,
        ))
    }

    /// `(walk, words, rows_w, view rows)` of a band of a row-major f16 buffer.
    fn rows_walk(
        &self,
        o: &Op,
        ax: &[M::Ax],
        dims: &[(i64, i64)],
        item: i64,
    ) -> VResult<(Walk, i64, i64, i64)> {
        let shape = M::shape_of(ax);
        if item != 2 {
            return Err(self.fail(
                &o.st,
                "L2.VC.003",
                "a GT4 band of a row-major buffer is f16",
            ));
        }
        let w = *shape.last().unwrap();
        let r = if shape.len() == 2 { shape[0] } else { 1 };
        if w % 32 != 0 {
            return Err(self.fail(
                &o.st,
                "L2.VC.003",
                format!("a GT4 band is whole 32-column blocks; this view is {w} wide"),
            ));
        }
        let inner: Vec<&(i64, i64)> = dims.iter().filter(|d| d.1 == item).collect();
        let outer: Vec<&(i64, i64)> = dims.iter().filter(|d| d.1 != item).collect();
        if (!inner.is_empty() && inner[0].0 != w) || outer.len() > 1 {
            return Err(self.fail(
                &o.st,
                "L2.VC.003",
                "a GT4 band of a row-major buffer is rows of contiguous columns",
            ));
        }
        let row_words = w * 2 / 32;
        if r == 1 || outer.is_empty() {
            return Ok((Walk::flat(32, row_words * r), row_words * r, row_words, r));
        }
        let pitch = outer[0].1;
        if pitch % 32 != 0 {
            return Err(self.fail(
                &o.st,
                "L2.VC.003",
                format!("rows {pitch} bytes apart: a DMA walk steps whole words"),
            ));
        }
        if pitch == w * 2 {
            return Ok((Walk::flat(32, row_words * r), row_words * r, row_words, r));
        }
        Ok((
            Walk(vec![(32, row_words), (pitch, r)]),
            row_words * r,
            row_words,
            r,
        ))
    }

    /// The `pack` op that made `name`, in the loop or before it.
    fn packed(&self, name: &str) -> Option<Arc<Node>> {
        let lp: Vec<Arc<Node>> = self
            .lp
            .as_ref()
            .map(|f| ops_of(&f.body))
            .unwrap_or_default();
        self.pre
            .iter()
            .chain(lp.iter())
            .find(|o| matches!(o.as_ref(), Node::Op(x) if x.target.as_deref() == Some(name)))
            .cloned()
    }

    fn alloc(&mut self, words: i64, o: &Op) -> VResult<i64> {
        let at = self.top;
        self.top += words;
        if self.top > self.land {
            return Err(self.fail(
                &o.st,
                "L2.VC.022",
                format!(
                    "L1 holds {} words; the buffers need {}",
                    self.land, self.top
                ),
            ));
        }
        Ok(at)
    }
}

fn as_view(t: &Term) -> Term {
    match t {
        Term::Name(n) => Term::View(n.clone(), vec![]),
        other => other.clone(),
    }
}

fn flat(groups: &[Vec<i64>]) -> Vec<i64> {
    groups.iter().flatten().copied().collect()
}

/// The @l1 value a load or recv of type `t` makes, through `d`.
pub fn l1val(d: Option<&Desc>, t: &Type, buf: i64, tg: &Target) -> Val {
    let f32 = t.dtype == "f32";
    let mut v = Val::new("l1", count_of(t, tg.vlmax)).with_mode(mode_of(Some(t)));
    v.buf = buf;
    v.per = if f32 { 16 } else { 8 };
    v.unpk = if f32 { "f32".into() } else { String::new() };
    if let Some(d) = d {
        if d.rows_w != 0 {
            v.rows_w = d.rows_w;
            v.bcast = d.bcast;
            v.unpk = "f16".into();
        }
    }
    v
}

/// The pack of register `i` for a store through `d`.
#[cfg(feature = "witness")]
pub fn pack_line(d: &Desc, buf: i64, i: i64, mode: &str, dtype: &str) -> String {
    if d.rows_w != 0 && !on("l2.vector/pack-walk-plain") {
        let nb = d.rows_w / 2;
        let (b, j) = (i / nb, i % nb);
        return format!(
            "vpack {{0}} -> l1[{}] f16 walk=({}, 1)",
            buf + 4 * b * d.rows_w + 2 * j,
            d.rows_w
        );
    }
    if dtype == "f32" {
        return format!("vpack {{0}} -> l1[{}] f32", buf + i * 16);
    }
    format!("vpack {{0}} -> l1[{}] {mode}", buf + i * 8)
}

/// The unpack of register `i` of an @l1 value.
#[cfg(feature = "witness")]
pub fn unpack_line(src: &Val, i: i64) -> String {
    if src.rows_w == 0 {
        let mode = if src.unpk.is_empty() {
            &src.mode
        } else {
            &src.unpk
        };
        return format!("vunpk {{0}} <- l1[{}] {mode}", src.buf + i * src.per);
    }
    let nb = src.rows_w / 2;
    let (b, j) = (i / nb, i % nb);
    let base = src.buf + if src.bcast { 0 } else { 4 * b * src.rows_w } + 2 * j;
    let s0 = if src.bcast { 0 } else { src.rows_w };
    format!("vunpk {{0}} <- l1[{base}] f16 walk=({s0}, 1)")
}

mod walks_typed;
use walks_typed::{pack_line_typed, unpack_line_typed};

fn xlane_k(c: &Ctx, o: &Op) -> VResult<i64> {
    let pos = o.st.positional();
    let k = if pos.len() == 2 { number(pos[1]) } else { None };
    match k {
        Some(k) if k == k.trunc() && 0.0 < k && k < 16.0 => Ok(k as i64),
        _ => Err(c.fail(
            &o.st,
            "L2.OP.022",
            format!(
                "{} X, K: a lane xor / rotation K of a chunk's 16 (0 < K < 16)",
                o.op
            ),
        )),
    }
}

/// Whether `pack` is the only op reading its source value.
fn read_once(ops: &[Arc<Node>], pack: &Op) -> bool {
    let Some(name) = pack.st.positional()[0].as_name() else {
        return false;
    };
    let readers: Vec<usize> = ops
        .iter()
        .map(|u| op(u))
        .filter(|u| {
            u.st.positional()
                .iter()
                .any(|a| value_name(a) == Some(name) && matches!(a, Term::Name(_) | Term::View(..)))
        })
        .map(|u| u.uid)
        .collect();
    readers == vec![pack.uid]
}

/// `{product: (a, b)}` for `p = mul a, b` read only by one `rsum`.
fn fusions(ops: &[Arc<Node>]) -> IndexMap<String, (Term, Term)> {
    let mut out = IndexMap::new();
    if on("l2.vector/no-fusion") {
        return out;
    }
    for n in ops {
        let o = op(n);
        let Some(target) = &o.target else { continue };
        if o.op != "mul" || o.ty.as_ref().map(|t| t.shape.len() != 2).unwrap_or(true) {
            continue;
        }
        let readers: Vec<&Op> = ops
            .iter()
            .map(|u| op(u))
            .filter(|u| {
                u.st.positional()
                    .iter()
                    .any(|a| value_name(a) == Some(target.as_str()))
            })
            .collect();
        let pos = o.st.positional();
        if pos.len() != 2 {
            continue;
        }
        let (a, b) = (pos[0], pos[1]);
        if readers.len() == 1
            && readers[0].op == "rsum"
            && readers[0].st.kw("valid_cols").is_none()
            && readers[0].st.kw("carry").is_none()
            && readers[0].st.positional() == vec![&Term::Name(target.clone())]
            && matches!(a, Term::Name(_))
            && matches!(b, Term::Name(_))
        {
            out.insert(target.clone(), (a.clone(), b.clone()));
        }
    }
    out
}
