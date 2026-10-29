//! Range analysis of a vector instance's values: which EXP2 / EXP2D
//! arguments are proven inside the ALU's s8.17 domain, so their guard
//! ([`super::guard`]) keeps only the checks no proof covers.
//!
//! A fact bounds every non-NaN value a name holds, `[lo, hi]`, and says
//! whether it may be NaN. NaN reaches EXP2 / EXP2D with or without a guard
//! and leaves as NaN, so a proof is about the non-NaN values alone. Loads,
//! unpacks and receives are unbounded; constants, clamps (`max` / `min`
//! with a bound operand), compares, `sel`, and the arithmetic and
//! transcendental ops carry bounds through, widened for E8M15 rounding and
//! the tables' error. One pass in program order: a name the stream loop
//! assigns is unbounded at the loop's entry (an earlier trip's value), and
//! after an `if sticky` block a name it assigns holds either binding.

use std::collections::{BTreeSet, HashMap, HashSet};

use ktpu_l1::text::Term;

use super::{number, value_name, RPar};
use crate::ir::{Node, Op};
use crate::mutant::on;

/// What the analysis may prove (the search's `ranges`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Ranges {
    /// Every argument guarded in full.
    Off,
    /// Interval proofs: sound for every input.
    Intervals,
    /// Intervals, and `x - rmax x <= 0` row by row where every output
    /// derived from it is NaN across a row holding a NaN ([`guards`]). Such
    /// a row breaks the bound itself: `vmax` keeps its first operand when the
    /// second is NaN, so the lane tree can drop a subtree and its maximum.
    RowMax,
}

impl Ranges {
    /// The search's default: the fastest setting, sound at every output
    /// for every input (the cycle measurements,
    /// `scripts/py/measure/exp2_model.py`).
    pub const DEFAULT: Ranges = Ranges::RowMax;

    pub fn parse(s: &str) -> Option<Ranges> {
        match s {
            "off" => Some(Ranges::Off),
            "intervals" => Some(Ranges::Intervals),
            "rowmax" => Some(Ranges::RowMax),
            _ => None,
        }
    }
}

/// What may lie outside the domain of an EXP2 / EXP2D argument: `high`, a
/// value at or above 128; `low`, a value below EXP2's -128 or at or below
/// EXP2D's -128 (EXP2D's designed special starts at `|a| >= 128`; EXP2's at
/// -128 is the flush to zero the frozen ALU also gives).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Risk {
    pub high: bool,
    pub low: bool,
}

impl Risk {
    pub const ALL: Risk = Risk {
        high: true,
        low: true,
    };
    pub const NONE: Risk = Risk {
        high: false,
        low: false,
    };
}

/// Each EXP2 / EXP2D op's risk by op uid; an op not in it is at [`Risk::ALL`].
pub type Guards = HashMap<usize, Risk>;

/// Mutant: range analysis off whatever the search's setting (the guards
/// before it, which the regolded witnesses reproduce under).
pub const OFF_MUTANT: &str = "l2.vector/exp2-ranges-off";
/// Mutant: every argument taken as proven in range.
pub const ASSUMED_MUTANT: &str = "l2.vector/exp2-range-assumed";
/// Mutant: `max` / `min` bound by both operands though the second may be NaN.
pub const NAN_BLIND_MUTANT: &str = "l2.vector/exp2-range-nan-blind";
/// Mutant: facts flow straight into the stream loop and out of `if sticky`.
pub const STALE_MUTANT: &str = "l2.vector/exp2-range-stale";
/// Mutant: `x - rmax x` bounded though the maximum masks columns.
pub const MASKED_MUTANT: &str = "l2.vector/exp2-rowmax-masked";
/// Mutant: `x - rmax x` bounded though an output derived from it is not NaN
/// across a row holding a NaN.
pub const UNPOISONED_MUTANT: &str = "l2.vector/exp2-rowmax-unpoisoned";
/// Mutant: every EXP2 argument taken as below 128, guarded by the floor alone.
pub const FLOOR_MUTANT: &str = "l2.vector/exp2-floor-everywhere";

/// E8M15's relative rounding (2^-15), with room: an add, a multiply or a
/// constant's register value.
const ROUND: f64 = 1.0 / 4096.0;
/// The transcendental tables' relative error, with room.
const TABLE: f64 = 1.0 / 256.0;
/// Below E8's smallest normal, a value flushes to zero.
const FLUSH: f64 = 1.0e-36;
/// Past this a rounded E8M15 value may be infinite (its largest finite is
/// 3.4e38).
const HUGE: f64 = 3.0e38;

/// `x`, flushed to a zero of its sign below E8's normals.
fn flushed(x: f64) -> f64 {
    if x.abs() < 2f64.powi(-126) {
        0f64.copysign(x)
    } else {
        x
    }
}

/// The non-NaN values a name may hold, and whether it may be NaN.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Fact {
    pub lo: f64,
    pub hi: f64,
    pub nan: bool,
}

impl Fact {
    pub const TOP: Fact = Fact {
        lo: f64::NEG_INFINITY,
        hi: f64::INFINITY,
        nan: true,
    };

    fn of(lo: f64, hi: f64, nan: bool) -> Fact {
        Fact {
            lo: if lo.is_nan() || lo < -HUGE {
                f64::NEG_INFINITY
            } else {
                lo
            },
            hi: if hi.is_nan() || hi > HUGE {
                f64::INFINITY
            } else {
                hi
            },
            nan,
        }
    }

    fn constant(f: f64) -> Fact {
        if f.is_nan() {
            return Fact::TOP;
        }
        Fact::of(f, f, false).widened(ROUND)
    }

    /// Wider by `rel` of each bound, and by a flushed value's size.
    fn widened(self, rel: f64) -> Fact {
        let out = |x: f64, s: f64| {
            if x.is_infinite() {
                x
            } else {
                x + s * (x.abs() * rel + FLUSH)
            }
        };
        Fact::of(out(self.lo, -1.0), out(self.hi, 1.0), self.nan)
    }

    fn hull(self, b: Fact) -> Fact {
        Fact::of(self.lo.min(b.lo), self.hi.max(b.hi), self.nan || b.nan)
    }

    fn infinite(self) -> bool {
        self.lo.is_infinite() || self.hi.is_infinite()
    }

    fn holds_zero(self) -> bool {
        self.lo <= 0.0 && 0.0 <= self.hi
    }

    fn neg(self) -> Fact {
        Fact::of(-self.hi, -self.lo, self.nan)
    }

    fn add(self, b: Fact) -> Fact {
        let opposed = (self.lo == f64::NEG_INFINITY && b.hi == f64::INFINITY)
            || (self.hi == f64::INFINITY && b.lo == f64::NEG_INFINITY);
        Fact::of(self.lo + b.lo, self.hi + b.hi, self.nan || b.nan || opposed).widened(ROUND)
    }

    fn mul(self, b: Fact) -> Fact {
        // 0 * inf is NaN; the bounds of the non-NaN products take it as 0.
        let p = |x: f64, y: f64| {
            let v = x * y;
            if v.is_nan() {
                0.0
            } else {
                v
            }
        };
        let ps = [
            p(self.lo, b.lo),
            p(self.lo, b.hi),
            p(self.hi, b.lo),
            p(self.hi, b.hi),
        ];
        let lo = ps.iter().copied().fold(f64::INFINITY, f64::min);
        let hi = ps.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        let nan = self.nan
            || b.nan
            || (self.infinite() && b.holds_zero())
            || (b.infinite() && self.holds_zero());
        Fact::of(lo, hi, nan).widened(ROUND)
    }

    /// `vmax a, b`: `a` when either is NaN (v2_alu.v:224).
    fn max(self, b: Fact) -> Fact {
        if !b.nan || on(NAN_BLIND_MUTANT) {
            return Fact::of(self.lo.max(b.lo), self.hi.max(b.hi), self.nan);
        }
        Fact::of(self.lo, self.hi.max(b.hi), self.nan)
    }

    /// `vmin a, b`: `a` when either is NaN.
    fn min(self, b: Fact) -> Fact {
        if !b.nan || on(NAN_BLIND_MUTANT) {
            return Fact::of(self.lo.min(b.lo), self.hi.min(b.hi), self.nan);
        }
        Fact::of(self.lo.min(b.lo), self.hi, self.nan)
    }

    fn abs(self) -> Fact {
        if self.lo >= 0.0 {
            self
        } else if self.hi <= 0.0 {
            self.neg()
        } else {
            Fact::of(0.0, self.hi.max(-self.lo), self.nan)
        }
    }

    /// 2^a as designed: +inf from 128, 0 below E8's normals.
    fn exp2(self) -> Fact {
        let e = |x: f64| {
            if x >= 128.0 {
                f64::INFINITY
            } else {
                x.exp2()
            }
        };
        let lo = e(self.lo) * (1.0 - TABLE);
        let lo = if lo < 2f64.powi(-126) { 0.0 } else { lo };
        Fact::of(lo, e(self.hi) * (1.0 + TABLE), self.nan)
    }

    /// 1 / a for a bound away from zero on one side; unbounded across it.
    fn inv(self) -> Fact {
        if self.lo > 0.0 || self.hi < 0.0 {
            let (lo, hi) = (flushed(self.lo), flushed(self.hi));
            return Fact::of(1.0 / hi, 1.0 / lo, self.nan).widened(TABLE);
        }
        Fact {
            nan: self.nan,
            ..Fact::TOP
        }
    }

    fn rsqrt(self) -> Fact {
        if self.lo > 0.0 {
            let lo = flushed(self.lo);
            return Fact::of(1.0 / self.hi.sqrt(), 1.0 / lo.sqrt(), self.nan).widened(TABLE);
        }
        Fact::TOP
    }

    fn log2(self) -> Fact {
        if self.lo > 0.0 {
            let lo = if self.lo < 2f64.powi(-126) {
                f64::NEG_INFINITY
            } else {
                self.lo.log2()
            };
            return Fact::of(lo, self.hi.log2(), self.nan).widened(TABLE);
        }
        Fact::TOP
    }
}

/// A binding of a name: the name and how many times it was bound before.
type Bind = (String, u32);

/// The instance's names: their facts, binding counts, which statistic is the
/// row maximum of which binding, and what a NaN does to them.
#[derive(Clone, Default)]
struct State {
    facts: HashMap<String, Fact>,
    bound: HashMap<String, u32>,
    /// statistic -> the binding it is the `rmax` of
    max_of: HashMap<String, Bind>,
    /// row value -> its columns
    cols: HashMap<String, i64>,
    /// value -> bindings it is NaN in every lane of that a binding is NaN in
    lane: HashMap<String, HashSet<Bind>>,
    /// value -> bindings a NaN anywhere in whose row makes its whole row NaN
    row: HashMap<String, HashSet<Bind>>,
    /// value -> the row-maximum bounds (their `sub` uids) it derives from
    deps: HashMap<String, BTreeSet<usize>>,
}

impl State {
    fn fact(&self, t: &Term) -> Fact {
        if let Some(f) = number(t) {
            return Fact::constant(f);
        }
        value_name(t)
            .and_then(|n| self.facts.get(n))
            .copied()
            .unwrap_or(Fact::TOP)
    }

    fn binding(&self, name: &str) -> Bind {
        (name.to_string(), self.bound.get(name).copied().unwrap_or(0))
    }

    fn bind(&mut self, name: &str, f: Fact) {
        self.facts.insert(name.to_string(), f);
        *self.bound.entry(name.to_string()).or_insert(0) += 1;
        self.max_of.remove(name);
        self.lane.remove(name);
        self.row.remove(name);
        self.deps.remove(name);
    }

    /// Every name `nodes` assigns, unbounded, derived from every row-maximum
    /// bound (an earlier trip's value).
    fn forget(&mut self, nodes: &[std::sync::Arc<Node>], carried: &BTreeSet<usize>) {
        for name in assigned(nodes) {
            self.bind(&name, Fact::TOP);
            self.deps.insert(name, carried.clone());
        }
    }
}

/// One pass of the analysis: the row-maximum bounds it may not use, those it
/// used and those an output refuses.
struct Pass {
    ranges: Ranges,
    rejected: HashSet<usize>,
    carried: BTreeSet<usize>,
    /// each bound used: its `sub` uid -> the binding `x` it bounds
    relations: HashMap<usize, Bind>,
    refused: HashSet<usize>,
    out: Guards,
}

fn assigned(nodes: &[std::sync::Arc<Node>]) -> Vec<String> {
    let mut out = Vec::new();
    for n in nodes {
        match n.as_ref() {
            Node::Op(o) => out.extend(o.target.clone()),
            Node::For(f) => out.extend(assigned(&f.body)),
            Node::If(i) => out.extend(assigned(&i.body)),
            Node::When(w) => out.extend(assigned(&w.body)),
            Node::Par(p) => out.extend(assigned(&p.body)),
        }
    }
    out
}

/// Each EXP2 / EXP2D op's risk in instance `par` under `ranges`.
///
/// A row-maximum bound (`x - rmax x <= 0`, RowMax) holds on a row free of
/// NaN; on a row holding one, every output of the instance derived from it
/// must be NaN across that row whatever the guards, as a softmax's is
/// through its row sum. An output derived from it without that refuses the
/// bound, and the analysis runs again without it.
pub fn guards(par: &RPar, ranges: Ranges) -> Guards {
    if (ranges == Ranges::Off || on(OFF_MUTANT)) && !forced() {
        return Guards::new();
    }
    let (mut rejected, mut carried) = (HashSet::new(), BTreeSet::new());
    loop {
        let mut p = Pass {
            ranges,
            rejected: rejected.clone(),
            carried: carried.clone(),
            relations: HashMap::new(),
            refused: HashSet::new(),
            out: Guards::new(),
        };
        walk(&par.body, &mut State::default(), &mut p);
        let used: BTreeSet<usize> = p.relations.keys().copied().collect();
        if p.refused.is_subset(&rejected) && used == carried {
            return p.out;
        }
        rejected.extend(p.refused);
        carried = used;
    }
}

/// A mutant that sets every risk.
fn forced() -> bool {
    on(ASSUMED_MUTANT) || on(FLOOR_MUTANT)
}

fn walk(nodes: &[std::sync::Arc<Node>], s: &mut State, p: &mut Pass) {
    for n in nodes {
        match n.as_ref() {
            Node::Op(o) => step(o, s, p),
            Node::For(f) => {
                if !on(STALE_MUTANT) {
                    s.forget(&f.body, &p.carried);
                }
                walk(&f.body, s, p);
            }
            Node::If(i) => {
                let before = s.clone();
                walk(&i.body, s, p);
                if !on(STALE_MUTANT) {
                    for name in assigned(&i.body) {
                        let fact = |st: &State| st.facts.get(&name).copied().unwrap_or(Fact::TOP);
                        let tags = |m: &HashMap<String, HashSet<Bind>>, o: &HashMap<_, _>| {
                            let empty = HashSet::new();
                            let a: &HashSet<Bind> = m.get(&name).unwrap_or(&empty);
                            let b: &HashSet<Bind> = o.get(&name).unwrap_or(&empty);
                            a.intersection(b).cloned().collect::<HashSet<_>>()
                        };
                        let lane = tags(&before.lane, &s.lane);
                        let row = tags(&before.row, &s.row);
                        let mut deps = before.deps.get(&name).cloned().unwrap_or_default();
                        deps.extend(s.deps.get(&name).cloned().unwrap_or_default());
                        s.bind(&name, fact(&before).hull(fact(s)));
                        s.lane.insert(name.clone(), lane);
                        s.row.insert(name.clone(), row);
                        s.deps.insert(name, deps);
                    }
                }
            }
            Node::When(w) => walk(&w.body, s, p),
            Node::Par(q) => walk(&q.body, s, p),
        }
    }
}

/// The risk EXP2 / EXP2D op `op` runs at with argument `a`.
pub fn risk(op: &str, a: Fact) -> Risk {
    if on(ASSUMED_MUTANT) {
        return Risk::NONE;
    }
    if on(FLOOR_MUTANT) && op == "exp2" {
        return Risk {
            high: false,
            low: true,
        };
    }
    let low = match op {
        "exp2" => a.lo < -128.0,
        _ => a.lo <= -128.0,
    };
    Risk {
        high: a.hi >= 128.0,
        low,
    }
}

/// The operands a NaN in passes through to every lane it reaches: `vmax` and
/// `vmin` keep a NaN `a` only, EXP2D's `floor b` is not NaN.
fn nan_operands(op: &str) -> &'static [usize] {
    match op {
        "mov" | "neg" | "abs" | "exp2" | "inv" | "rsqrt" | "log2" | "pack" | "max" | "min"
        | "exp2d" => &[0],
        "add" | "sub" | "mul" => &[0, 1],
        "fma" | "fnma" => &[0, 1, 2],
        _ => &[],
    }
}

fn step(o: &Op, s: &mut State, p: &mut Pass) {
    let pos = o.st.positional();
    let arg = |i: usize| pos.get(i).map(|t| s.fact(t)).unwrap_or(Fact::TOP);
    if o.op == "exp2" || o.op == "exp2d" {
        p.out.insert(o.uid, risk(&o.op, arg(0)));
    }
    let reads: Vec<&str> = pos.iter().filter_map(|t| value_name(t)).collect();
    let mut deps: BTreeSet<usize> = reads
        .iter()
        .flat_map(|n| s.deps.get(*n).cloned().unwrap_or_default())
        .collect();
    if o.target.is_none() || o.op == "pack" {
        for n in &reads {
            let row = s.row.get(*n);
            for d in s.deps.get(*n).into_iter().flatten() {
                let Some(x) = p.relations.get(d) else {
                    continue;
                };
                if !row.is_some_and(|r| r.contains(x)) && !on(UNPOISONED_MUTANT) {
                    p.refused.insert(*d);
                }
            }
        }
    }
    let Some(target) = o.target.clone() else {
        return;
    };
    let mut lane = HashSet::new();
    let mut row = HashSet::new();
    for &i in nan_operands(&o.op) {
        let Some(t) = pos.get(i) else { continue };
        if let Term::Name(n) = t {
            lane.insert(s.binding(n));
            lane.extend(s.lane.get(n).cloned().unwrap_or_default());
        }
        if let Some(n) = value_name(t) {
            row.extend(s.row.get(n).cloned().unwrap_or_default());
        }
    }
    if let ("xlane" | "xrot", Some(n)) = (o.op.as_str(), pos.first().and_then(|t| value_name(t))) {
        row.extend(s.row.get(n).cloned().unwrap_or_default());
    }
    let whole = o.st.kw("valid_cols").is_none() && o.st.kw("carry").is_none();
    if let ("rsum", Some(Term::Name(n)), true) = (o.op.as_str(), pos.first(), whole) {
        row.insert(s.binding(n));
        row.extend(s.lane.get(n).cloned().unwrap_or_default());
        row.extend(s.row.get(n).cloned().unwrap_or_default());
    }
    let ranges = p.ranges;
    let f = match o.op.as_str() {
        "mov" => arg(0),
        "neg" => arg(0).neg(),
        "abs" => arg(0).abs(),
        "exp2" => arg(0).exp2(),
        "inv" => arg(0).inv(),
        "rsqrt" => arg(0).rsqrt(),
        "log2" => arg(0).log2(),
        "add" => arg(0).add(arg(1)),
        "sub" => {
            let d = arg(0).add(arg(1).neg());
            match below_max(&pos, s) {
                Some(x) if ranges == Ranges::RowMax && !p.rejected.contains(&o.uid) => {
                    p.relations.insert(o.uid, x);
                    deps.insert(o.uid);
                    Fact::of(d.lo, d.hi.min(0.0), d.nan)
                }
                _ => d,
            }
        }
        "mul" => arg(0).mul(arg(1)),
        "max" => arg(0).max(arg(1)),
        "min" => arg(0).min(arg(1)),
        "fma" => arg(0).mul(arg(1)).add(arg(2)),
        "fnma" => arg(0).mul(arg(1)).neg().add(arg(2)),
        "exp2d" => Fact::of(0.0, f64::INFINITY, arg(0).nan || arg(1).nan),
        "cmplt" | "cmpgt" | "cmpeq" => Fact::of(0.0, 1.0, false),
        "sel" => arg(1).hull(arg(2)),
        "xlane" | "xrot" => arg(0),
        "rmax" if o.st.kw("carry").is_none() => {
            let x = arg(0);
            match o.st.kw("valid_cols") {
                Some(_) => Fact::of(f64::NEG_INFINITY, x.hi, x.nan),
                None => x,
            }
        }
        "rsum" if o.st.kw("carry").is_none() => {
            let cols = pos
                .first()
                .and_then(|t| value_name(t))
                .and_then(|n| s.cols.get(n))
                .copied();
            match cols {
                Some(n) => {
                    let x = arg(0);
                    let x = match o.st.kw("valid_cols") {
                        Some(_) => x.hull(Fact::of(0.0, 0.0, false)),
                        None => x,
                    };
                    let n = n as f64;
                    Fact::of(n * x.lo, n * x.hi, x.nan || x.infinite()).widened(ROUND * n)
                }
                None => Fact::TOP,
            }
        }
        _ => Fact::TOP,
    };
    let x = match pos.first() {
        Some(Term::Name(x)) => Some(s.binding(x)),
        _ => None,
    };
    s.bind(&target, f);
    s.lane.insert(target.clone(), lane);
    s.row.insert(target.clone(), row);
    s.deps.insert(target.clone(), deps);
    if let Some(ty) = &o.ty {
        if ty.shape.len() == 2 {
            s.cols.insert(target.clone(), ty.shape[1]);
        }
    }
    if o.op == "rmax" && (o.st.kw("valid_cols").is_none() || on(MASKED_MUTANT)) {
        if let Some(x) = x {
            s.max_of.insert(target, x);
        }
    }
}

/// The binding of `x` in `sub x, m[:, *]` where `m` is `rmax x` of it.
fn below_max(pos: &[&Term], s: &State) -> Option<Bind> {
    let (Some(Term::Name(x)), Some(Term::View(m, sl))) = (pos.first(), pos.get(1)) else {
        return None;
    };
    if !matches!(sl.last(), Some(Term::NewAxis)) {
        return None;
    }
    let x = s.binding(x);
    (s.max_of.get(m) == Some(&x)).then_some(x)
}

#[cfg(test)]
mod tests;
