//! A vector-core instance's L2 ops to a V2 image (L2.VC, L2.OP, L2.STAT,
//! L2.SEARCH).
//!
//! A loop's loads and stores get two L1 buffers and the loop body is two
//! iterations; a view affine in the loop variable is a descriptor with
//! `ainc`. Rows go in groups (8 with a reduction, else 4): each op runs over
//! the group's rows before the next op. A result takes the register of a
//! source read here for the last time.

pub mod banks;
pub mod ctx;
pub mod encode;
pub mod gen;
pub mod modulo;
#[cfg(feature = "witness")]
pub mod pat;
pub mod range;
pub mod search;
mod transfers;
pub mod tree;
mod views;

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;
pub use ktpu_l1::layout::tensor::zero_lift_value;
use ktpu_l1::text::{py_float, Env, Term};

use super::links::{Links, Transfer};
use super::memory as M;
use crate::ir::read::bind;
use crate::ir::{span, value, Body, For, If, Node, Op, Par, Result};
use crate::target::Target;
#[cfg(feature = "witness")]
use gen::Code;

pub use range::{Guards, Ranges, Risk};
pub use search::Search;
pub use transfers::transfers;
pub use views::{contiguous, mx7, tiled, view_offset, Walk};

pub const UNARY: [(&str, &str); 7] = [
    ("exp2", "vexp2"),
    ("inv", "vinv"),
    ("rsqrt", "vrsqrt"),
    ("log2", "vlog2"),
    ("neg", "vneg"),
    ("abs", "vabs"),
    ("mov", "vmov"),
];
pub const BINARY: [(&str, &str); 6] = [
    ("add", "vadd"),
    ("sub", "vsub"),
    ("mul", "vmul"),
    ("max", "vmax"),
    ("min", "vmin"),
    ("exp2d", "vexp2d"),
];
pub const TERNARY: [(&str, &str); 2] = [("fma", "vfma"), ("fnma", "vfnma")];
/// `m = cmplt a, b`: 1.0 where a < b, else 0.0.
pub const CMP: [(&str, &str); 3] = [
    ("cmplt", "vcmplt"),
    ("cmpgt", "vcmpgt"),
    ("cmpeq", "vcmpeq"),
];
/// The RUL.030 refusal of a statistic stored as `[R, 32]` rows.
pub const RUL_030: &str = "a statistic packs 16 rows a word as f16[R] @l1, not as [R, 32] rows";
pub const REDUCE: [(&str, &str); 2] = [("rmax", "vmax"), ("rsum", "vadd")];
/// Lane crossbar ops: their crossbar mode.
pub const XLANE: [(&str, &str); 2] = [("xlane", "xor"), ("xrot", "rot")];

pub fn lookup(table: &[(&str, &'static str)], op: &str) -> Option<&'static str> {
    table.iter().find(|(k, _)| *k == op).map(|(_, v)| *v)
}

/// An op's mnemonic (or crossbar mode) as the active mutant has the tables.
pub fn code_of(op: &str) -> Option<&'static str> {
    let swap: &[(&str, &str, &'static str)] = &[
        ("l2.vector/unary-swapped", "exp2", "vlog2"),
        ("l2.vector/unary-swapped", "log2", "vexp2"),
        ("l2.vector/unary-swapped", "inv", "vrsqrt"),
        ("l2.vector/binary-swapped", "add", "vsub"),
        ("l2.vector/binary-swapped", "sub", "vadd"),
        ("l2.vector/binary-swapped", "max", "vmin"),
        ("l2.vector/ternary-swapped", "fma", "vfnma"),
        ("l2.vector/ternary-swapped", "fnma", "vfma"),
        ("l2.vector/cmp-swapped", "cmpgt", "vcmplt"),
        ("l2.vector/cmp-swapped", "cmplt", "vcmpgt"),
        ("l2.vector/reduce-swapped", "rmax", "vadd"),
        ("l2.vector/reduce-swapped", "rsum", "vmax"),
        ("l2.vector/xlane-rot", "xlane", "rot"),
        ("l2.vector/xlane-rot", "xrot", "xor"),
    ];
    if let Some((_, _, c)) = swap
        .iter()
        .find(|(m, o, _)| *o == op && crate::mutant::on(m))
    {
        return Some(c);
    }
    [
        &UNARY[..],
        &BINARY[..],
        &TERNARY[..],
        &CMP[..],
        &REDUCE[..],
        &XLANE[..],
    ]
    .iter()
    .find_map(|t| lookup(t, op))
}

pub fn is_in(table: &[(&str, &str)], op: &str) -> bool {
    table.iter().any(|(k, _)| *k == op)
}

/// EXP2 and EXP2D reduce `a` to signed s8.17 fixed point, `[-128, 128)`, and
/// decode specials only from `|a| >= 256` (v2_alu.v:374, 420-471): `a` in
/// `[128, 256)` wraps negative, `(-256, -128)` wraps positive. Doubling such
/// an `a` lands it on the special decode (+inf, or 0 below zero): the ALU as
/// designed. For EXP2D that special holds whatever `b` is, so EXP2D's value
/// at `|a| >= 128` is not `2^(a - floor b)` where `b` brings it back in range.
pub const EXP2_RANGE: f64 = 128.0;
/// EXP2's floor: `2^a` flushes to zero at and below it in E8M15, so a clamp
/// there is exact (NaN passes `vmax` as its `a`, v2_alu.v:224; -0 survives).
/// EXP2D's `a - floor(b)` can lift `a` back above it: EXP2D takes no clamp.
pub const EXP2_FLOOR: f64 = -127.0;

/// EXP2D's bound below: `a > -128` keeps `a`.
pub const EXP2D_LOW: f64 = -EXP2_RANGE;

/// The mutant that drops every domain guard: the frozen ALU's wrapped domain.
pub const DOMAIN_MUTANT: &str = "l2.vector/exp2-wrapped-domain";

/// The instructions guarding an EXP2 / EXP2D argument `a` into `t`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Guard {
    None,
    /// EXP2 at risk below only: `vmax t, a, -127`.
    Floor,
    /// EXP2 at risk above: the floor, `vcmplt p1, t, 128`,
    /// `vadd t, t, t unless=p1`.
    Clamp,
    /// EXP2D at risk both ways: `vabs t, a`, `vcmplt p1, t, 128`,
    /// `vadd t, a, a`, `vmov t, a if=p1`.
    Both,
    /// EXP2D at risk above only: `vcmplt p1, a, 128`, then as [`Guard::Both`].
    High,
    /// EXP2D at risk below only: `vcmpgt p1, a, -128`, then as [`Guard::Both`].
    Low,
}

/// The guard of op `op` (`ctx` math) at risk `r`.
pub fn guard(op: &str, r: Risk) -> Guard {
    if crate::mutant::on(DOMAIN_MUTANT) {
        return Guard::None;
    }
    match (op, r.high, r.low) {
        ("exp2", true, _) => Guard::Clamp,
        ("exp2", false, true) => Guard::Floor,
        ("exp2d", true, true) => Guard::Both,
        ("exp2d", true, false) => Guard::High,
        ("exp2d", false, true) => Guard::Low,
        _ => Guard::None,
    }
}

/// The risk `guards` gives op `o` (every risk when unproven).
pub fn risk_of(guards: &Guards, o: &Op) -> Risk {
    guards.get(&o.uid).copied().unwrap_or(Risk::ALL)
}

/// The constants op `op`'s guard at risk `r` reads, in its order.
pub fn domain_constants(op: &str, r: Risk) -> &'static [f64] {
    match guard(op, r) {
        Guard::None => &[],
        Guard::Floor => &[EXP2_FLOOR],
        Guard::Clamp => &[EXP2_FLOOR, EXP2_RANGE],
        Guard::Both | Guard::High => &[EXP2_RANGE],
        Guard::Low => &[EXP2D_LOW],
    }
}

/// 0.0, 1.0 and -1.0 read the constant registers k0, k1, k2.
pub fn kreg(f: f64) -> Option<&'static str> {
    if crate::mutant::on("l2.vector/no-kreg") {
        None
    } else if f == 0.0 {
        Some("k0")
    } else if f == 1.0 {
        Some("k1")
    } else if f == -1.0 {
        Some("k2")
    } else {
        None
    }
}

/// A float as a map key: -0.0 and 0.0 one key, as Python's dict has them.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct FKey(u64);

impl FKey {
    pub fn of(f: f64) -> FKey {
        FKey(if f == 0.0 {
            0f64.to_bits()
        } else {
            f.to_bits()
        })
    }
}

/// Why a candidate does not lower: a refusal, more registers than the core
/// has (the candidate is dropped), or a value the shapes do not admit.
#[derive(Clone, Debug)]
pub enum VErr {
    Fail(ktpu_l1::Error),
    Pressure,
    Value(String),
}

impl From<ktpu_l1::Error> for VErr {
    fn from(e: ktpu_l1::Error) -> VErr {
        VErr::Fail(e)
    }
}

pub type VResult<T> = std::result::Result<T, VErr>;

/// A value of the instance.
#[derive(Clone, Debug)]
pub struct Val {
    /// "l1" | "row" | "stat"
    pub kind: &'static str,
    /// registers a row value takes (a GT4 register is 4 x 32)
    pub rows: i64,
    /// l1: word offset
    pub buf: i64,
    /// row: register -> symbol; stat: group -> symbol
    pub regs: IndexMap<i64, String>,
    /// "f16" (one row a register), "gt4", or a pack's dtype
    pub mode: String,
    /// l1: words a register's unpack reads, and in what mode ("" the value's)
    pub per: i64,
    pub unpk: String,
    /// l1, rows mode: words an L1 row holds; every row reading the one row
    pub rows_w: i64,
    pub bcast: bool,
    /// row: the value's columns
    pub cols: i64,
}

impl Val {
    pub fn new(kind: &'static str, rows: i64) -> Val {
        Val {
            kind,
            rows,
            buf: 0,
            regs: IndexMap::new(),
            mode: "f16".into(),
            per: 8,
            unpk: String::new(),
            rows_w: 0,
            bcast: false,
            cols: 0,
        }
    }

    pub fn with_mode(mut self, mode: &str) -> Val {
        self.mode = mode.to_string();
        self
    }
}

/// A memory descriptor (or a peer drain into a cluster).
#[derive(Clone, Debug, Default)]
pub struct Desc {
    pub ad: usize,
    /// None: a peer drain into a cluster
    pub buffer: Option<String>,
    /// bytes into the buffer; a peer drain's first granule
    pub off: i64,
    pub words: i64,
    pub inc: i64,
    /// L1 word offset per parity
    pub buf: Vec<i64>,
    pub walk: Walk,
    /// a peer drain's cluster
    pub to: String,
    pub side: i64,
    /// "load" | "store" | "send"
    pub kind: String,
    /// a send: its op
    pub kind_op: usize,
    pub count: bool,
    pub credit: bool,
    /// a streamed send: the odd trips' descriptor (an index into descs)
    pub alt: Option<usize>,
    pub rows_w: i64,
    pub bcast: bool,
    /// a store packed over this load's buffers (an index into descs)
    pub shared: Option<usize>,
    /// a store: the packed value it drains
    pub value: String,
    /// a store reading an earlier store's buffers
    pub twin: Option<usize>,
}

impl Desc {
    /// Bytes one walk spans.
    pub fn extent(&self) -> i64 {
        32 + self.walk.0.iter().map(|(s, n)| (n - 1) * s).sum::<i64>()
    }

    pub fn span(&self, trips: i64) -> (i64, i64) {
        let (lo, hi) = (self.off, self.off + self.extent());
        let step = self.inc * (trips - 1);
        (lo + step.min(0), hi + step.max(0))
    }
}

/// A stream into this core: the trip's sub-tiles land in `slots` by parity.
#[derive(Clone, Debug)]
pub struct StreamIn {
    pub src: String,
    pub dst: String,
    pub slots: Vec<i64>,
    pub size: i64,
    pub shape: Vec<i64>,
}

/// What a `recv` or `send` of the instance is paired with.
#[derive(Clone, Debug)]
pub enum PlanX {
    T(Transfer),
    S(StreamIn),
}

/// By op uid, in the order the instance's ops pair.
pub type Plan = IndexMap<usize, PlanX>;

/// A `par` resolved for one instance: its `when`s kept or dropped, and its
/// EXP2 / EXP2D ops' risks (none proven until a search sets them).
#[derive(Clone, Debug)]
pub struct RPar {
    pub st: ktpu_l1::text::Stmt,
    pub index: Option<Term>,
    pub body: Vec<Arc<Node>>,
    pub guards: Guards,
}

pub fn op_of(n: &Node) -> &Op {
    match n {
        Node::Op(o) => o,
        _ => unreachable!("an op"),
    }
}

/// The ops of a stream loop's body, an `if sticky` block's included.
pub fn ops_of(body: &[Arc<Node>]) -> Vec<Arc<Node>> {
    let mut out = Vec::new();
    for o in body {
        match o.as_ref() {
            Node::If(i) => out.extend(ops_of(&i.body)),
            _ => out.push(o.clone()),
        }
    }
    out
}

pub fn mode_of(t: Option<&crate::ir::Type>) -> &'static str {
    match t {
        Some(t)
            if t.mode.as_deref() == Some("gt4")
                && !crate::mutant::on("l2.vector/tile-loads-rows") =>
        {
            "gt4"
        }
        _ => "f16",
    }
}

/// Registers (or row-register slots) a value of type `t` takes.
pub fn count_of(t: &crate::ir::Type, row: i64) -> i64 {
    if t.shape.len() == 2
        && (mode_of(Some(t)) == "gt4"
            || t.dtype == "mx7a"
            || t.dtype == "mx7b"
            || (t.shape[1] > row
                && t.shape[1] % row == 0
                && !crate::mutant::on("l2.vector/word-row-count")))
    {
        return t.shape[0] * t.shape[1] / row;
    }
    t.shape[0]
}

/// The value an operand reads (a name, or a view's).
pub fn value_name(a: &Term) -> Option<&str> {
    match a {
        Term::View(n, _) => Some(n),
        Term::Name(n) => Some(n),
        _ => None,
    }
}

pub fn number(a: &Term) -> Option<f64> {
    match a {
        Term::Float(f) => Some(*f),
        Term::Int(v, _) => Some(*v as f64),
        _ => None,
    }
}

/// The numbers `ops` read as operands (not K registers), domain guards' (at
/// their risk in `guards`) and a paired pack's lift included, the most read
/// first.
pub fn numbers(ops: &[Arc<Node>], body: &Body, guards: &Guards) -> Vec<f64> {
    let mut uses: IndexMap<FKey, (f64, i64)> = IndexMap::new();
    for n in ops {
        let Node::Op(op) = n.as_ref() else { continue };
        if is_in(&XLANE, &op.op) {
            continue;
        }
        let lift = mx7_pack(op, ops, body).then(zero_lift_value);
        let read = op.st.positional().into_iter().filter_map(number);
        let guard = domain_constants(&op.op, risk_of(guards, op));
        for f in read.chain(guard.iter().copied()).chain(lift) {
            if kreg(f).is_none() {
                uses.entry(FKey::of(f)).or_insert((f, 0)).1 += 1;
            }
        }
    }
    let mut v: Vec<(f64, i64)> = uses.into_values().collect();
    v.sort_by_key(|(_, c)| -c);
    v.into_iter().map(|(f, _)| f).collect()
}

/// A `pack` into MXFP7 that a K pair can read (stored into a buffer of
/// `nk` >= 2, or sent to a cluster): its zeros are selected to
/// `tensor::ZERO_LIFT` first, so an all-zero block packs E=0 (RUL.099;
/// `zero_lift_value`, a constant it reads).
pub fn mx7_pack(op: &Op, ops: &[Arc<Node>], body: &Body) -> bool {
    let target = op.target.as_deref();
    let paired = |n: &Arc<Node>| {
        let Node::Op(s) = n.as_ref() else {
            return false;
        };
        let pos = s.st.positional();
        match s.op.as_str() {
            "send" => pos.first().and_then(|p| p.as_name()) == target,
            "store" => {
                pos.get(2).and_then(|p| p.as_name()) == target
                    && pos
                        .first()
                        .and_then(|p| match p {
                            Term::View(name, _) | Term::Name(name) => body.buffers.get(name),
                            _ => None,
                        })
                        .is_some_and(|b| b.get("nk") >= 2)
            }
            _ => false,
        }
    };
    op.op == "pack"
        && op
            .ty
            .as_ref()
            .is_some_and(|t| t.dtype == "mx7a" || t.dtype == "mx7b")
        && !crate::mutant::on("l2.vector/unit-scale-zero")
        && !crate::mutant::before_rul099()
        && ops.iter().any(paired)
}

pub fn fmt_float(f: f64) -> String {
    py_float(f)
}

/// `par` with each `when` kept or dropped for this instance.
pub fn resolve(body: &Body, par: &Par, env: &Env) -> Result<RPar> {
    fn go(body: &Body, stmts: &[Arc<Node>], env: &Env) -> Result<Vec<Arc<Node>>> {
        let mut out = Vec::new();
        for s in stmts {
            match s.as_ref() {
                Node::When(w) => {
                    let keep = match crate::ir::const_of(body, &w.st, &w.cond, env) {
                        Ok(v) => v,
                        Err(_) => {
                            return Err(body.reader.fail(
                                &w.st,
                                "L2.VC.019",
                                "a when in a vector instance is constant per instance",
                            ))
                        }
                    };
                    if keep != 0 {
                        out.extend(go(body, &w.body, env)?);
                    }
                }
                Node::For(f) => {
                    let mut f2 = f.clone();
                    f2.body = go(body, &f.body, env)?;
                    out.push(Arc::new(Node::For(f2)));
                }
                Node::If(i) => {
                    let mut i2: If = i.clone();
                    i2.body = go(body, &i.body, env)?;
                    out.push(Arc::new(Node::If(i2)));
                }
                _ => out.push(s.clone()),
            }
        }
        Ok(out)
    }
    Ok(RPar {
        st: par.st.clone(),
        index: par.index.clone(),
        body: go(body, &par.body, env)?,
        guards: Guards::new(),
    })
}

/// This instance does nothing: every statement in a dropped `when`.
pub fn idle(body: &Body, par: &Par, env: &Env) -> Result<bool> {
    Ok(resolve(body, par, env)?.body.is_empty())
}

/// `(statements before the loop, the stream loop)`.
pub fn split(body: &Body, par: &RPar) -> Result<(Vec<Arc<Node>>, Option<For>)> {
    let mut pre = Vec::new();
    let mut lp: Option<For> = None;
    for s in &par.body {
        match s.as_ref() {
            Node::For(f) => {
                if lp.is_some() {
                    return Err(body.reader.fail(
                        &f.st,
                        "L2.VC.018",
                        "one stream loop a vector instance",
                    ));
                }
                lp = Some(f.clone());
            }
            other if lp.is_some() => {
                return Err(body.reader.fail(
                    other.st(),
                    "L2.VC.018",
                    "nothing after the stream loop",
                ))
            }
            _ => pre.push(s.clone()),
        }
    }
    Ok((pre, lp))
}

pub fn unit_of(body: &Body, par: &RPar, env: &Env) -> Result<String> {
    let v = value(body, &par.st, par.index.as_ref().unwrap(), env)?;
    Ok(format!("vc[{}]", v.as_int().unwrap_or(0)))
}

/// `(image text, its parameters, accesses)` for one vector instance of `par`:
/// the candidate the core's timing gives the fewest cycles (L2.SEARCH.001).
#[allow(clippy::too_many_arguments)]
pub fn lower(
    body: &Body,
    par: &Par,
    env: &Env,
    name: &str,
    links: &mut Links,
    land: Option<i64>,
    t: &Target,
    search: &Search,
) -> Result<(String, Vec<String>, Vec<super::sync::Access>)> {
    let misfit = t.shape_misfit();
    if !misfit.is_empty() {
        return Err(body.reader.fail(
            &par.st,
            "L2.VC.025",
            format!(
                "the vector lowering is written for another register shape: {}",
                misfit.join(", ")
            ),
        ));
    }
    let rp = resolve(body, par, env)?;
    let plan = transfers(body, &rp, env, links, t)?;
    let best = search.best(body, &rp, env, name, &plan, land, t)?;
    let text = best.text;
    #[cfg(feature = "mutants")]
    let text = {
        let mut text = text;
        if crate::mutant::on("l2.vector/sel-arms-swapped") {
            text = text.lines().map(sel_swapped).collect::<Vec<_>>().join("\n") + "\n";
        }
        if crate::mutant::on("l2.vector/send-uncounted") {
            text = text.replace(", count=1)", ")");
        }
        text
    };
    Ok((text, best.params, best.accesses))
}

/// `vsel D, A, B, ...` with A and B traded.
#[cfg(feature = "mutants")]
fn sel_swapped(line: &str) -> String {
    let lead = line.len() - line.trim_start().len();
    let rest = &line[lead..];
    let Some(args) = rest.strip_prefix("vsel ") else {
        return line.to_string();
    };
    let parts: Vec<&str> = args.splitn(4, ", ").collect();
    if parts.len() < 4
        || parts[..3]
            .iter()
            .any(|p| p.is_empty() || p.contains(char::is_whitespace))
    {
        return line.to_string();
    }
    format!(
        "{}vsel {}, {}, {}, {}",
        &line[..lead],
        parts[0],
        parts[2],
        parts[1],
        parts[3]
    )
}

/// The core's timing of the image `lower` picks, with transfers landing
/// where no other unit's code decides (the retile pass's cost).
pub fn total(body: &Body, par: &Par, env: &Env, t: &Target, search: &Search) -> Result<i64> {
    let rp = resolve(body, par, env)?;
    let mut alone = Links::alone(t);
    let plan = transfers(body, &rp, env, &mut alone, t)?;
    let me = unit_of(body, &rp, env)?;
    let lands_here = plan.values().any(|x| match x {
        PlanX::T(x) => x.dst == me,
        PlanX::S(s) => s.dst == me,
    });
    let land = if lands_here {
        Some(t.l1_words - crate::target::POOL)
    } else {
        None
    };
    Ok(search.best(body, &rp, env, "k", &plan, land, t)?.cycles)
}

/// The largest byte step from one trip to the next of a memory view in the
/// instance's stream loop (0: no loop, or one trip).
pub fn max_step(body: &Body, par: &Par, env: &Env, t: &Target) -> VResult<i64> {
    let rp = resolve(body, par, env)?;
    let (_, lp) = split(body, &rp)?;
    let Some(lp) = lp else { return Ok(0) };
    let trips: Vec<i64> = span(body, &lp.st, &lp.lo, &lp.hi, env)?;
    if trips.len() < 2 {
        return Ok(0);
    }
    let mut out = 0;
    for n in ops_of(&lp.body) {
        let Node::Op(op) = n.as_ref() else { continue };
        if op.op != "load" && op.op != "store" {
            continue;
        }
        let pos = op.st.positional();
        let Some(view) = pos.first() else { continue };
        if !matches!(view, Term::View(..) | Term::Name(_)) {
            continue;
        }
        let a = view_offset(body, &op.st, view, &bind(env, &lp.var, trips[0]), t)?;
        let b = view_offset(body, &op.st, view, &bind(env, &lp.var, trips[1]), t)?;
        out = out.max((b - a).abs());
    }
    Ok(out)
}

mod assign_typed;

/// Physical registers v0.. over the emitted order: a symbol holds its
/// register from its first to its last appearance; a result takes the
/// register of a source whose last appearance is that instruction.
#[cfg(feature = "witness")]
pub fn assign(code: &[Code], pinned: &HashSet<String>, regs: u32) -> VResult<Vec<String>> {
    let mut last: HashMap<&str, usize> = HashMap::new();
    for (i, c) in code.iter().enumerate() {
        if let Code::Op(_, syms) = c {
            for s in syms {
                last.insert(s, i);
            }
        }
    }
    let mut reg: HashMap<&str, u32> = HashMap::new();
    let mut free: Vec<u32> = (0..regs).collect();
    let mut out = Vec::new();
    for (i, c) in code.iter().enumerate() {
        let (tmpl, syms) = match c {
            Code::Raw(s) => {
                out.push(s.clone());
                continue;
            }
            Code::Op(t, s) => (t, s),
        };
        let mut dying: Vec<&str> = syms[1..]
            .iter()
            .map(|s| s.as_str())
            .filter(|s| last[s] == i && reg.contains_key(s) && !pinned.contains(*s))
            .collect();
        for (j, s) in syms.iter().enumerate() {
            if reg.contains_key(s.as_str()) {
                continue;
            }
            if j == 0 && !dying.is_empty() {
                let d = dying.remove(0);
                let r = reg[d];
                reg.insert(s, r);
                #[cfg(test)]
                assign_typed::record(s, r);
                continue;
            }
            if free.is_empty() {
                return Err(VErr::Pressure);
            }
            let r = free.remove(0);
            reg.insert(s, r);
            #[cfg(test)]
            assign_typed::record(s, r);
        }
        let names: Vec<String> = syms
            .iter()
            .map(|s| format!("v{}", reg[s.as_str()]))
            .collect();
        out.push(pat::format(tmpl, &names));
        let held: HashSet<u32> = syms
            .iter()
            .filter(|s| last[s.as_str()] > i || pinned.contains(*s))
            .map(|s| reg[s.as_str()])
            .collect();
        let mut seen: HashSet<&str> = HashSet::new();
        for s in syms {
            if !seen.insert(s) {
                continue;
            }
            let r = reg[s.as_str()];
            if last[s.as_str()] == i
                && !pinned.contains(s)
                && !held.contains(&r)
                && !free.contains(&r)
            {
                free.push(r);
            }
        }
        free.sort();
    }
    Ok(out)
}

#[cfg(feature = "witness")]
pub fn indent(lines: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    let mut depth = 0usize;
    for ln in lines {
        if ln == "{" {
            depth += 1;
        } else if ln == "}" {
            depth -= 1;
        } else {
            out.push(format!("{}{ln}", "    ".repeat(depth)));
        }
    }
    out
}

/// An image text with its header's name `name`.
pub fn renamed(text: &str, name: &str) -> String {
    let (head, rest) = text.split_once('\n').unwrap_or((text, ""));
    let args = &head[head.find('(').unwrap()..];
    format!("image {name}{args}\n{rest}")
}
