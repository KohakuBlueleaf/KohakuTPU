//! The sign of a zero through the ops that move a value unchanged
//! (ISA.VC.058): the core sends vmov, vsel, vmax, vmin, vneg and vabs, and a
//! copy written `vmul d, k1, X`, through the FMA as `x * 1.0 + (+0)`, so a zero
//! leaves them +0. A zero product passes the addend through unchanged, so
//! `vadd d, k0, x` is x and `vsub d, k0, x` is -x bit for bit, both zeros
//! included (model probe 3).
//!
//! A pick writes P1 at its destination's chunks (ISA.VC.080) and its
//! predicated copy reads P1 there (ISA.VC.081), so the compare names the
//! destination's view on p1. A merge crossbar also redirects the a operand
//! (ISA.VC.076), so no copy of a merge operand is exact: under preserve the
//! reduction trees build no merge pick (`tree::lane_folds`).

use std::collections::BTreeSet;

use ktpu_l1::text::{Stmt, Term};

use super::gen::typed::{self as ir, Event};

/// How the lowering moves a value it does not change (the search's
/// `zero_sign`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ZeroSign {
    /// The core's moves: a zero comes back +0.
    Native,
    /// Every data move, select, neg and max / min in a form that returns the
    /// sign of a zero as the operation defines it.
    Preserve,
}

/// The lanes a beat covers and the chunks of a register (`vc.lanes`,
/// `vc.chunks`): where an op reads and writes its predicate bits.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Lanes {
    pub lanes: i64,
    pub chunks: i64,
}

impl Lanes {
    pub fn of(t: &crate::target::Target) -> Lanes {
        Lanes {
            lanes: t.lanes,
            chunks: t.chunks,
        }
    }
}

impl ZeroSign {
    /// The lowering's default.
    pub const DEFAULT: ZeroSign = ZeroSign::Native;
    pub const ALL: [ZeroSign; 2] = [ZeroSign::Native, ZeroSign::Preserve];

    pub fn name(self) -> &'static str {
        match self {
            ZeroSign::Native => "native",
            ZeroSign::Preserve => "preserve",
        }
    }

    pub fn parse(s: &str) -> Option<ZeroSign> {
        Self::ALL.into_iter().find(|z| z.name() == s)
    }

    /// Whether a literal `f` is set as -0 (`vsub d, k0, k0`, 0x8000 on the
    /// model) rather than read from k0.
    pub fn negative_zero(self, f: f64) -> bool {
        self == ZeroSign::Preserve
            && f == 0.0
            && f.is_sign_negative()
            && !crate::mutant::on("l2.zero_sign/fold-negzero")
    }

    /// Whether this value rewrites `op` as a compare and copies, which read
    /// no merge operand exactly: a reduction tree of it folds lanes by xor.
    pub fn picks(self, op: &str) -> bool {
        self == ZeroSign::Preserve
            && match op {
                "vmax" | "vmin" => !crate::mutant::on("l2.zero_sign/native-minmax"),
                "vsel" => !crate::mutant::on("l2.zero_sign/native-sel"),
                _ => false,
            }
    }

    /// `events` with every move in this value's form (unchanged when native).
    pub fn events(self, events: Vec<Event>, lanes: Lanes) -> Result<Vec<Event>, String> {
        Ok(self.sign(events, None, lanes)?.0)
    }

    /// A modulo trip's head and the trip, which repeats after it: both in
    /// this value's form, and the head's length.
    pub fn trip(
        self,
        head: Vec<Event>,
        trip: Vec<Event>,
        lanes: Lanes,
    ) -> Result<(Vec<Event>, usize), String> {
        self.sign(head, Some(trip), lanes)
    }

    /// [`ZeroSign::events`] of the template generator's code.
    #[cfg(feature = "witness")]
    pub fn code(
        self,
        code: Vec<super::gen::Code>,
        lanes: Lanes,
    ) -> Result<Vec<super::gen::Code>, String> {
        Ok(self.sign(code, None, lanes)?.0)
    }

    /// [`ZeroSign::trip`] of the template generator's code.
    #[cfg(feature = "witness")]
    pub fn code_trip(
        self,
        head: Vec<super::gen::Code>,
        trip: Vec<super::gen::Code>,
        lanes: Lanes,
    ) -> Result<(Vec<super::gen::Code>, usize), String> {
        self.sign(head, Some(trip), lanes)
    }

    fn sign<P: Program>(
        self,
        head: Vec<P>,
        trip: Option<Vec<P>>,
        lanes: Lanes,
    ) -> Result<(Vec<P>, usize), String> {
        if self == ZeroSign::Native {
            let n = head.len();
            let mut all = head;
            all.extend(trip.unwrap_or_default());
            return Ok((all, n));
        }
        let mut items: Vec<Option<&P>> = head.iter().map(Some).collect();
        if let Some(trip) = &trip {
            items.push(None);
            items.extend(trip.iter().map(Some));
            items.push(None);
        }
        let mut steps = Vec::with_capacity(items.len());
        for (i, item) in items.iter().enumerate() {
            steps.push(match item {
                Some(p) => p.step(i.checked_sub(1).and_then(|j| items[j])),
                None if i == head.len() => Step::Open(Block::Loop),
                None => Step::Close,
            });
        }
        let mut out = Vec::with_capacity(items.len());
        let mut at = None;
        for (item, new) in items.iter().zip(rewrite(&steps, lanes)?) {
            match (item, new) {
                (Some(p), Some(new)) => out.extend(p.rebuilt(new)),
                (Some(p), None) => out.push((*p).clone()),
                (None, _) => {
                    at.get_or_insert(out.len());
                }
            }
        }
        let at = at.unwrap_or(out.len());
        Ok((out, at))
    }
}

impl Default for ZeroSign {
    fn default() -> ZeroSign {
        ZeroSign::DEFAULT
    }
}

/// An operand of a math line, typed or text.
trait Operand: Clone + PartialEq {
    fn named(name: &str) -> Self;
    /// The option `key=p1`.
    fn when(key: &str) -> Self;
    fn is_named(&self, name: &str) -> bool;
    /// p1 itself or a view of it.
    fn is_p1(&self) -> bool;
    /// This operand's chunk select on register `name`.
    fn at(&self, name: &str) -> Self;
    /// (base chunk, stride) of a register operand: `[c:]` (c, 1), `[c]`
    /// (c, 0), the whole register (0, 1); None for any other select.
    fn select(&self) -> Option<(i64, i64)>;
    /// A `vl=` option: Some(Some(n)) for `vl=n`, Some(None) for a VL the
    /// line does not state.
    fn vl(&self) -> Option<Option<i64>>;
    /// An `if=` / `unless=` option.
    fn predicates(&self) -> bool;
    /// An `if=p1` / `unless=p1` option.
    fn reads_p1(&self) -> bool;
    /// The vector registers (and symbols) it names.
    fn regs(&self) -> BTreeSet<String>;
}

/// A generated vector symbol `%N` (`gen::typed::Symbol::name`): the pass
/// runs right after the select fusion, in the band and modulo paths and the
/// text mirror alike, before allocation resolves any symbol to a `vN`.
fn vector_name(n: &str) -> bool {
    n.len() > 1 && n.starts_with('%') && n[1..].bytes().all(|b| b.is_ascii_digit())
}

impl Operand for Term {
    fn named(name: &str) -> Term {
        Term::name(name)
    }
    fn when(key: &str) -> Term {
        ir::kw(key, Term::name("p1"))
    }
    fn is_named(&self, name: &str) -> bool {
        self == &Term::name(name)
    }
    fn is_p1(&self) -> bool {
        matches!(self, Term::Name(n) | Term::View(n, _) if n == "p1")
    }
    fn at(&self, name: &str) -> Term {
        match self {
            Term::View(_, slices) => Term::View(name.into(), slices.clone()),
            _ => Term::name(name),
        }
    }
    fn select(&self) -> Option<(i64, i64)> {
        match self {
            Term::Name(_) => Some((0, 1)),
            Term::View(_, slices) => match slices.as_slice() {
                [Term::Slice(Some(b), None)] => Some((b.as_int()? as i64, 1)),
                [Term::Int(b, _)] => Some((*b as i64, 0)),
                _ => None,
            },
            _ => None,
        }
    }
    fn vl(&self) -> Option<Option<i64>> {
        match self {
            Term::Assign(k, v) if k.is_named("vl") => Some(v.as_int().map(|n| n as i64)),
            _ => None,
        }
    }
    fn predicates(&self) -> bool {
        matches!(self, Term::Assign(k, _) if k.is_named("if") || k.is_named("unless"))
    }
    fn reads_p1(&self) -> bool {
        matches!(self, Term::Assign(k, v)
            if (k.is_named("if") || k.is_named("unless")) && v.is_named("p1"))
    }
    fn regs(&self) -> BTreeSet<String> {
        fn go(t: &Term, out: &mut BTreeSet<String>) {
            match t {
                Term::Name(n) | Term::View(n, _) if vector_name(n) => {
                    out.insert(n.clone());
                }
                Term::Call(_, a) => a.iter().for_each(|x| go(x, out)),
                Term::Kw(_, x) => go(x, out),
                _ => {}
            }
        }
        let mut out = BTreeSet::new();
        go(self, &mut out);
        out
    }
}

#[cfg(feature = "witness")]
impl Operand for String {
    fn named(name: &str) -> String {
        name.to_string()
    }
    fn when(key: &str) -> String {
        format!("{key}=p1")
    }
    fn is_named(&self, name: &str) -> bool {
        self == name
    }
    fn is_p1(&self) -> bool {
        self == "p1" || self.starts_with("p1[")
    }
    fn at(&self, name: &str) -> String {
        match self.find('[') {
            Some(i) => format!("{name}{}", &self[i..]),
            None => name.to_string(),
        }
    }
    fn select(&self) -> Option<(i64, i64)> {
        let Some(i) = self.find('[') else {
            return (!self.contains('(')).then_some((0, 1));
        };
        let inner = self[i + 1..].strip_suffix(']')?.trim();
        match inner.strip_suffix(':') {
            Some(base) => Some((base.trim().parse().ok()?, 1)),
            None => Some((inner.parse().ok()?, 0)),
        }
    }
    fn vl(&self) -> Option<Option<i64>> {
        self.strip_prefix("vl=").map(|n| n.parse().ok())
    }
    fn predicates(&self) -> bool {
        self.starts_with("if=") || self.starts_with("unless=")
    }
    fn reads_p1(&self) -> bool {
        self == "if=p1" || self == "unless=p1"
    }
    fn regs(&self) -> BTreeSet<String> {
        self.split(|c: char| !(c.is_ascii_alphanumeric() || c == '%'))
            .filter(|n| vector_name(n))
            .map(str::to_string)
            .collect()
    }
}

/// `op dst, srcs.. opts..`.
#[derive(Clone, Debug, PartialEq)]
struct Line<T> {
    op: String,
    dst: T,
    srcs: Vec<T>,
    opts: Vec<T>,
}

impl Line<Term> {
    /// The line of a statement with operands (None for one without).
    fn of(st: &Stmt) -> Option<Line<Term>> {
        let (opts, pos): (Vec<Term>, Vec<Term>) = st
            .args
            .iter()
            .cloned()
            .partition(|a| matches!(a, Term::Assign(..)));
        Some(Line {
            op: st.op.clone(),
            dst: pos.first()?.clone(),
            srcs: pos[1..].to_vec(),
            opts,
        })
    }

    fn stmt(self) -> Stmt {
        ir::math(&self.op, self.dst, self.srcs, self.opts)
    }
}

#[cfg(feature = "witness")]
impl Line<String> {
    /// The line of a text instruction with operands (None for one without).
    fn read(line: &str) -> Option<Line<String>> {
        let op = line.split(' ').next()?;
        let (args, options) = super::gen::operands(line, op)?;
        Some(Line {
            op: op.to_string(),
            dst: args[0].to_string(),
            srcs: args[1..].iter().map(|a| a.to_string()).collect(),
            opts: options.split_whitespace().map(str::to_string).collect(),
        })
    }

    fn text(&self) -> String {
        let mut s = format!("{} {}", self.op, self.dst);
        for a in &self.srcs {
            s += &format!(", {a}");
        }
        for o in &self.opts {
            s += &format!(" {o}");
        }
        s
    }
}

/// `line` as a template over `syms` (each `%N` by its first place there).
#[cfg(feature = "witness")]
fn template(line: &str, syms: &[String]) -> String {
    let b = line.as_bytes();
    let mut out = String::new();
    let mut i = 0;
    while i < b.len() {
        let j = (i + 1..=b.len())
            .find(|&j| j == b.len() || !b[j].is_ascii_digit())
            .unwrap();
        if b[i] == b'%' && j > i + 1 {
            let at = syms.iter().position(|s| s == &line[i..j]).unwrap();
            out += &format!("{{{at}}}");
            i = j;
        } else {
            out += match b[i] {
                b'{' => "{{",
                b'}' => "}}",
                _ => &line[i..i + 1],
            };
            i += 1;
        }
    }
    out
}

/// How a block repeats: a loop runs its body again after its end, a skip
/// runs it once or not at all.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Block {
    Loop,
    Skip,
}

impl Block {
    fn of(header: &str) -> Block {
        if header == "skipz" {
            Block::Skip
        } else {
            Block::Loop
        }
    }
}

/// One step of a program for the pass: a line it may rewrite, a fixed line
/// it only reads for its p1 use, or a block edge.
enum Step<T> {
    Op(Line<T>),
    Fixed(Option<Line<T>>),
    Open(Block),
    Close,
}

/// A program element the pass walks: typed events or generator code.
trait Program: Clone {
    type T: Operand;
    /// This element as a step; `before` is the element ahead of it (a
    /// block's header).
    fn step(&self, before: Option<&Self>) -> Step<Self::T>;
    /// This op's replacement lines as elements.
    fn rebuilt(&self, lines: Vec<Line<Self::T>>) -> Vec<Self>;
}

impl Program for Event {
    type T = Term;
    fn step(&self, before: Option<&Event>) -> Step<Term> {
        match self {
            Event::Op { stmt, .. } => Step::Op(Line::of(stmt).unwrap()),
            Event::Fixed(stmt) => Step::Fixed(Line::of(stmt)),
            Event::Open => Step::Open(match before {
                Some(Event::Fixed(h)) => Block::of(&h.op),
                _ => Block::Loop,
            }),
            Event::Close => Step::Close,
        }
    }
    fn rebuilt(&self, lines: Vec<Line<Term>>) -> Vec<Event> {
        let Event::Op { syms, .. } = self else {
            unreachable!("only an op is rewritten")
        };
        lines
            .into_iter()
            .map(|l| Event::Op {
                stmt: l.stmt(),
                syms: syms.clone(),
            })
            .collect()
    }
}

#[cfg(feature = "witness")]
impl Program for super::gen::Code {
    type T = String;
    fn step(&self, before: Option<&Self>) -> Step<String> {
        use super::gen::Code;
        match self {
            Code::Op(t, s) => Step::Op(Line::read(&super::pat::format(t, s)).unwrap()),
            Code::Raw(r) if r == "{" => Step::Open(match before {
                Some(Code::Raw(h)) => Block::of(h.split(' ').next().unwrap_or("")),
                _ => Block::Loop,
            }),
            Code::Raw(r) if r == "}" => Step::Close,
            Code::Raw(r) => Step::Fixed(Line::read(r)),
        }
    }
    fn rebuilt(&self, lines: Vec<Line<String>>) -> Vec<Self> {
        use super::gen::Code;
        let Code::Op(_, syms) = self else {
            unreachable!("only an op is rewritten")
        };
        lines
            .into_iter()
            .map(|l| Code::Op(template(&l.text(), syms), syms.clone()))
            .collect()
    }
}

/// A set of P1 bits, bit `chunk * lanes + lane`, in words enough for every
/// bit of the core.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Bits(Vec<u64>);

impl Bits {
    fn none(lanes: Lanes) -> Bits {
        Bits(vec![0; (lanes.lanes * lanes.chunks + 63) as usize / 64])
    }

    fn every(lanes: Lanes) -> Bits {
        let mut b = Bits::none(lanes);
        (0..lanes.lanes * lanes.chunks).for_each(|i| b.insert(i));
        b
    }

    fn insert(&mut self, i: i64) {
        self.0[(i / 64) as usize] |= 1 << (i % 64);
    }

    fn union(&mut self, other: &Bits) {
        self.0.iter_mut().zip(&other.0).for_each(|(a, b)| *a |= b);
    }

    fn remove(&mut self, other: &Bits) {
        self.0.iter_mut().zip(&other.0).for_each(|(a, b)| *a &= !b);
    }

    fn meets(&self, other: &Bits) -> bool {
        self.0.iter().zip(&other.0).any(|(a, b)| a & b != 0)
    }

    fn is_empty(&self) -> bool {
        self.0.iter().all(|w| *w == 0)
    }
}

/// The P1 bits a line with destination `dst` and options `opts` touches: each
/// beat its destination chunk, the last beat only below VL mod lanes
/// (ISA.VC.059, .080, .081). None where the select or VL is not stated.
fn bits<T: Operand>(dst: &T, opts: &[T], lanes: Lanes) -> Option<Bits> {
    let vl = match opts.iter().find_map(Operand::vl) {
        None => lanes.lanes * lanes.chunks,
        Some(n) => n?,
    };
    let (base, stride) = dst.select()?;
    let beats = (vl + lanes.lanes - 1) / lanes.lanes;
    let mut out = Bits::none(lanes);
    for i in 0..beats {
        let chunk = (base + stride * i).rem_euclid(lanes.chunks);
        let n = if i == beats - 1 && vl % lanes.lanes != 0 {
            vl % lanes.lanes
        } else {
            lanes.lanes
        };
        (0..n).for_each(|l| out.insert(chunk * lanes.lanes + l));
    }
    Some(out)
}

/// P1 liveness over a program's blocks: the bits live after each step.
struct Flow<'a, T> {
    steps: &'a [Step<T>],
    lanes: Lanes,
    open: Vec<usize>,
    after: Vec<Bits>,
}

impl<T: Operand> Flow<'_, T> {
    /// The bits live before `steps[from..to]` given `out` live after it,
    /// recording each step's.
    fn back(&mut self, from: usize, to: usize, out: Bits) -> Bits {
        let mut live = out;
        let mut i = to;
        while i > from {
            i -= 1;
            self.after[i] = live.clone();
            match &self.steps[i] {
                Step::Close if !crate::mutant::on("l2.zero_sign/flat-liveness") => {
                    let open = self.open[i];
                    let Step::Open(block) = self.steps[open] else {
                        unreachable!("a block closes what it opens")
                    };
                    live = self.block(open + 1, i, block, live);
                    self.after[open] = live.clone();
                    i = open;
                }
                Step::Op(l) | Step::Fixed(Some(l)) => {
                    if l.op.starts_with("vcmp") && l.dst.is_p1() {
                        if let Some(written) = bits(&l.dst, &l.opts, self.lanes) {
                            live.remove(&written);
                        }
                    }
                    if l.opts.iter().any(Operand::reads_p1) {
                        live.union(
                            &bits(&l.dst, &l.opts, self.lanes)
                                .unwrap_or_else(|| Bits::every(self.lanes)),
                        );
                    }
                }
                _ => {}
            }
        }
        live
    }

    /// The bits live before a block whose body is `steps[from..to]`: a loop's
    /// body ends where it starts again, and either block may run no trip.
    fn block(&mut self, from: usize, to: usize, block: Block, out: Bits) -> Bits {
        let mut start = Bits::none(self.lanes);
        loop {
            let mut end = out.clone();
            if block == Block::Loop {
                end.union(&start);
            }
            let before = self.back(from, to, end);
            let fixed = before == start || block == Block::Skip;
            start = before;
            if fixed {
                break;
            }
        }
        start.union(&out);
        start
    }
}

/// Each step's replacement under `preserve` (None: kept).
fn rewrite<T: Operand>(
    steps: &[Step<T>],
    lanes: Lanes,
) -> Result<Vec<Option<Vec<Line<T>>>>, String> {
    let mut open = vec![0; steps.len()];
    let mut stack = vec![];
    for (i, s) in steps.iter().enumerate() {
        match s {
            Step::Open(_) => stack.push(i),
            Step::Close => open[i] = stack.pop().expect("a block closes what it opens"),
            _ => {}
        }
    }
    let mut flow = Flow {
        steps,
        lanes,
        open,
        after: vec![Bits::none(lanes); steps.len()],
    };
    flow.back(0, steps.len(), Bits::none(lanes));
    steps
        .iter()
        .zip(&flow.after)
        .map(|(s, live)| match s {
            Step::Op(l) => preserve(l, live, lanes),
            _ => Ok(None),
        })
        .collect()
}

/// A copy of `x` into `dst`: vmov for a constant (a k or s register).
fn copy<T: Operand>(dst: &T, x: &T, opts: Vec<T>) -> Line<T> {
    if x.regs().is_empty() || crate::mutant::on("l2.zero_sign/native-copy") {
        return Line {
            op: "vmov".into(),
            dst: dst.clone(),
            srcs: vec![x.clone()],
            opts,
        };
    }
    Line {
        op: "vadd".into(),
        dst: dst.clone(),
        srcs: vec![T::named("k0"), x.clone()],
        opts,
    }
}

/// `dst = p1 ? yes : no` after `cmp p1, srcs`, the compare on the
/// destination's chunks.
fn pick<T: Operand>(
    l: &Line<T>,
    cmp: &str,
    srcs: Vec<T>,
    yes: &T,
    no: &T,
    live: &Bits,
    lanes: Lanes,
) -> Result<Option<Vec<Line<T>>>, String> {
    if l.opts.iter().any(Operand::predicates) {
        return Err(format!("a predicated {} under zero_sign=preserve", l.op));
    }
    let clobbers = match bits(&l.dst, &l.opts, lanes) {
        Some(b) => b.meets(live),
        None => !live.is_empty(),
    };
    if clobbers {
        return Err(format!(
            "zero_sign=preserve writes p1 at a {} while p1 is live",
            l.op
        ));
    }
    let with = |key: &str| {
        let mut o = l.opts.clone();
        o.push(T::when(key));
        o
    };
    let d = &l.dst;
    let test = Line {
        op: cmp.into(),
        dst: if crate::mutant::on("l2.zero_sign/compare-unviewed") {
            T::named("p1")
        } else {
            d.at("p1")
        },
        srcs,
        opts: l.opts.clone(),
    };
    Ok(Some(if d == no {
        vec![test, copy(d, yes, with("if"))]
    } else if d == yes {
        vec![test, copy(d, no, with("unless"))]
    } else if d.regs().is_disjoint(&yes.regs()) && d.regs().is_disjoint(&no.regs()) {
        vec![test, copy(d, no, l.opts.clone()), copy(d, yes, with("if"))]
    } else {
        return Err(format!(
            "a {} whose destination overlaps a source in part",
            l.op
        ));
    }))
}

/// `l`'s replacement under `preserve`, given the P1 bits live after it.
fn preserve<T: Operand>(
    l: &Line<T>,
    live: &Bits,
    lanes: Lanes,
) -> Result<Option<Vec<Line<T>>>, String> {
    let copied = |x: &T| Ok(Some(vec![copy(&l.dst, x, l.opts.clone())]));
    let picks = ZeroSign::Preserve.picks(&l.op);
    match (l.op.as_str(), l.srcs.as_slice()) {
        ("vmov", [x]) if !x.regs().is_empty() => copied(x),
        ("vmul", [k, x] | [x, k]) if k.is_named("k1") && !x.regs().is_empty() => copied(x),
        ("vneg", [x]) => Ok(Some(vec![Line {
            op: "vsub".into(),
            dst: l.dst.clone(),
            srcs: vec![T::named("k0"), x.clone()],
            opts: l.opts.clone(),
        }])),
        ("vmax" | "vmin", [a, b]) if picks => {
            let cmp = if l.op == "vmax" { "vcmplt" } else { "vcmpgt" };
            pick(l, cmp, vec![a.clone(), b.clone()], b, a, live, lanes)
        }
        // `vsel d, x, k, x` moves x only when it is not zero and a constant
        // otherwise, both exact: the core's select is the preserving one.
        ("vsel", [a, b, c])
            if a == c && b.regs().is_empty() && !crate::mutant::on("l2.zero_sign/lift-picked") =>
        {
            Ok(None)
        }
        ("vsel", [a, b, c]) if picks => {
            let srcs = vec![c.clone(), T::named("k0")];
            pick(l, "vcmpeq", srcs, b, a, live, lanes)
        }
        _ => Ok(None),
    }
}

#[cfg(test)]
mod tests;
