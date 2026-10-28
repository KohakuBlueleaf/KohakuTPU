//! A cluster instance's L2 ops to its L1 words (L2.MG.001-.024).
//!
//! Allocation: a fill takes the bank of its side that holds no live value
//! and that the latest sweep does not read. Order: program order; a drain to
//! memory becomes the tile's last sweep's `emit=` plus a fused DRAIN placed
//! just before the unit's next emitting sweep; without `fuse`, or when that
//! sweep already emits, a plain DRAIN after it.

mod fill;
mod sweep;
pub mod tiles;

use std::collections::HashMap;

use indexmap::IndexMap;
use ktpu_l1::text::{Env, Stmt, Term};

use super::links::Links;
use super::memory as M;
use super::sync::{mem, Access};
use crate::ir::repr::repr;
use crate::ir::{const_of, value, Body, Instance, Node, Op, Result, Type};
use crate::mutant::on;
use crate::target::Target;

pub const SHARE: [&str; 3] = ["port", "all", "cross"];

/// A sweep's fields; `emit` set by a later drain.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Gemm {
    pub gm: i64,
    pub gn: i64,
    pub nk: i64,
    pub ab: i64,
    pub bb: i64,
    pub acc: i64,
    pub aoff: i64,
    pub boff: i64,
    pub emit: Option<String>,
    pub to: bool,
}

/// A shared view's match key: its buffer and its fill runs
/// `[(byte offset, entries, first L1 entry)]`.
pub type ShareKey = (String, Vec<(i64, i64, i64)>);

/// One L1 cluster word as text, and the item segment it came from.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Line {
    pub text: String,
    pub seg: usize,
    pub gemm: Option<Gemm>,
    /// a fill of a shared view: its mode, and the view to match across instances
    pub share: Option<String>,
    pub key: Option<ShareKey>,
    pub peers: Vec<i64>,
    pub fill: bool,
    /// a drain added into cluster `into`'s accumulator (on node `far`)
    pub into: Option<i64>,
    pub drain: bool,
    pub far: Option<i64>,
}

impl Line {
    pub fn new(text: impl Into<String>, seg: usize) -> Line {
        Line {
            text: text.into(),
            seg,
            ..Default::default()
        }
    }

    pub fn render(&self) -> String {
        if self.peers.is_empty() {
            return self.text.clone();
        }
        let p: Vec<String> = self.peers.iter().map(|p| format!("mg[{p}]")).collect();
        format!("{} peers=({})", self.text, p.join(", "))
    }
}

#[derive(Clone, Debug)]
struct Val {
    kind: &'static str,
    shape: (i64, i64),
    bank: i64,
    nk: i64,
    /// acc: the line of the sweep that last wrote it
    gemm: Option<usize>,
    zero: bool,
    at: i64,
    entry: i64,
    flow: bool,
}

impl Val {
    fn new(kind: &'static str, shape: (i64, i64)) -> Val {
        Val {
            kind,
            shape,
            bank: 0,
            nk: 0,
            gemm: None,
            zero: false,
            at: -1,
            entry: 0,
            flow: false,
        }
    }

    /// `[lo, hi)` L1 entries the value takes: banks of `bank` entries, lane
    /// groups of `lanes` rows.
    fn span(&self, bank: i64, lanes: i64) -> (i64, i64) {
        let lo = self.bank * bank + self.entry;
        (lo, lo + (self.shape.0 / lanes) * self.nk)
    }
}

/// One cluster's words, cut into segments, what they touch, and the bank of
/// each side its last sweep reads.
#[derive(Clone, Debug, Default)]
pub struct Stream {
    pub unit: String,
    pub lines: Vec<Line>,
    /// (emitting gemm line, drain text)
    held: Vec<(usize, String)>,
    pub accesses: Vec<Access>,
    pub reading: (Option<i64>, Option<i64>),
    pub node: i64,
}

struct Ctx<'a> {
    body: &'a Body,
    t: &'a Target,
    s: Stream,
    vals: IndexMap<String, Val>,
    uses: HashMap<i64, i64>,
}

fn side_ix(side: &str) -> usize {
    if side == "A" {
        0
    } else {
        1
    }
}

fn fail(body: &Body, st: &Stmt, clause: &'static str, msg: impl Into<String>) -> ktpu_l1::Error {
    body.reader.fail(st, clause, msg)
}

/// `inst`'s items as a stream. Without `fuse` every drain is plain; `links`
/// pairs transfers with their `recv`s; `reading` the banks the unit's
/// previous stage left read.
pub fn lower(
    body: &Body,
    inst: &Instance,
    t: &Target,
    fuse: bool,
    links: &mut Links,
    reading: Option<(Option<i64>, Option<i64>)>,
) -> Result<Stream> {
    let mut c = Ctx {
        body,
        t,
        s: Stream {
            unit: format!("mg[{}]", inst.index.unwrap()),
            node: inst.node,
            reading: reading.unwrap_or((None, None)),
            ..Default::default()
        },
        vals: IndexMap::new(),
        uses: last_uses(&inst.items),
    };
    let mut seg = 0;
    let first = inst.cuts.iter().min().copied();
    let mut posts: HashMap<i64, Vec<(i64, String)>> = HashMap::new();
    let mut stream_n: IndexMap<String, i64> = IndexMap::new();
    let mut stream_k: IndexMap<String, i64> = IndexMap::new();
    if let Some(first) = first {
        for (j, (n, env)) in inst.items.iter().enumerate() {
            if let Node::Op(op) = n.as_ref() {
                if op.op == "recv" && j >= first {
                    let pos = op.st.positional();
                    let src = value(body, &op.st, pos[0], env)?;
                    let key = unit_key(&src);
                    let space = op.ty.as_ref().map(|t| t.space.clone()).unwrap_or_default();
                    if links.stream(&key, &c.s.unit, &space).is_some() {
                        *stream_n.entry(key).or_insert(0) += 1;
                    }
                }
            }
        }
    }
    for (i, (n, env)) in inst.items.iter().enumerate() {
        if inst.cuts.contains(&i) && i > 0 {
            seg += 1;
        }
        let Node::Op(op) = n.as_ref() else {
            return Err(fail(body, n.st(), "L2.MG.016", "a cluster runs ops only"));
        };
        let ii = i as i64;
        match op.op.as_str() {
            "fill" => c.fill(ii, op, env, seg)?,
            "recv" if first.map(|f| i >= f).unwrap_or(false) => {
                let got = c.recv_stream(op, env, links, ii, seg, &stream_n, &mut stream_k)?;
                match got {
                    Some((nn, src, k)) => {
                        if k + 2 < stream_n[&src] {
                            let at = c.uses.get(&ii).copied().unwrap_or(ii);
                            posts.entry(at).or_default().push((nn, src));
                        }
                        continue;
                    }
                    None => c.recv(op, env, links, ii, seg)?,
                }
            }
            "recv" => c.recv(op, env, links, ii, seg)?,
            "zero" => {
                let ty = c.ty(op, "acc")?;
                c.acc_fits(op, &ty)?;
                let mut v = Val::new("acc", (ty.shape[0], ty.shape[1]));
                v.zero = true;
                c.vals.insert(op.target.clone().unwrap(), v);
            }
            "gemm" => c.gemm(op, env, seg)?,
            "drain" => c.drain(op, env, seg, fuse && !on("l2.cluster/no-fuse"), links)?,
            other => {
                return Err(fail(
                    body,
                    &op.st,
                    "L2.MG.016",
                    format!("no cluster op '{other}': fill, recv, zero, gemm, drain"),
                ))
            }
        }
        for (nn, src) in posts.remove(&ii).unwrap_or_default() {
            c.s.lines
                .push(Line::new(format!("post n={nn} to={src}"), seg));
        }
    }
    let mut s = place_held(c.s);
    for ln in s.lines.iter_mut() {
        if on("l2.cluster/zero-ignored") {
            ln.text = ln.text.replace("acc=0", "acc=1");
        }
        if on("l2.cluster/bank0-only") {
            ln.text = bank0(&ln.text);
        }
        if on("l2.cluster/drain-uncounted") {
            ln.text = uncounted(&ln.text);
        }
    }
    Ok(s)
}

/// `re.sub(r"\b([AB])1\b", r"\g<1>0", text)`.
fn bank0(text: &str) -> String {
    let b = text.as_bytes();
    let w = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let mut out = text.to_string().into_bytes();
    for i in 0..b.len().saturating_sub(1) {
        if (b[i] == b'A' || b[i] == b'B')
            && b[i + 1] == b'1'
            && (i == 0 || !w(b[i - 1]))
            && (i + 2 >= b.len() || !w(b[i + 2]))
        {
            out[i + 1] = b'0';
        }
    }
    String::from_utf8(out).unwrap()
}

/// `re.sub(r" count\b", "", text)`.
fn uncounted(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(i) = rest.find(" count") {
        let after = &rest[i + 6..];
        let boundary = after
            .chars()
            .next()
            .map(|c| !(c.is_ascii_alphanumeric() || c == '_'))
            .unwrap_or(true);
        out.push_str(&rest[..i]);
        if !boundary {
            out.push_str(" count");
        }
        rest = after;
    }
    out.push_str(rest);
    out
}

/// `"vc[3]"` of an evaluated `vc[3]`.
fn unit_key(src: &Term) -> String {
    match src {
        Term::View(n, s) if s.len() == 1 => match &s[0] {
            Term::Int(v, _) => format!("{n}[{v}]"),
            other => format!("{n}[{}]", ktpu_l1::text::fmt(other)),
        },
        other => ktpu_l1::text::fmt(other),
    }
}

/// Item index of a definition -> index of the last item reading that value
/// (before the name is bound again); a value never read maps to itself.
fn last_uses(items: &[crate::ir::Item]) -> HashMap<i64, i64> {
    let mut out = HashMap::new();
    let mut defs: HashMap<String, i64> = HashMap::new();
    for (i, (n, _)) in items.iter().enumerate() {
        let i = i as i64;
        let args: &[Term] = match n.as_ref() {
            Node::Op(o) => &o.args,
            _ => &[],
        };
        for a in args {
            let name = match a {
                Term::Name(t) => Some(t.as_str()),
                Term::View(t, _) => Some(t.as_str()),
                _ => None,
            };
            if let Some(name) = name {
                if let Some(&d) = defs.get(name) {
                    out.insert(d, i);
                }
            }
        }
        if let Some(t) = n.target() {
            defs.insert(t.to_string(), i);
            out.entry(i).or_insert(i);
        }
    }
    out
}

impl<'a> Ctx<'a> {
    fn ty(&self, op: &Op, space: &str) -> Result<Type> {
        let (Some(t), Some(_)) = (&op.ty, &op.target) else {
            return Err(fail(
                self.body,
                &op.st,
                "L2.MG.020",
                format!("NAME = {} ... : TYPE @{space}", op.op),
            ));
        };
        if t.space != space {
            return Err(fail(
                self.body,
                &op.st,
                "L2.MG.020",
                format!("{} makes a value @{space}, not @{}", op.op, t.space),
            ));
        }
        Ok(t.clone())
    }

    fn acc_fits(&self, op: &Op, t: &Type) -> Result<()> {
        let (m, n) = (t.shape[0], t.shape[1]);
        let g = self.t.mg_lanes;
        if m % g != 0 || n % g != 0 || (m / g) * (n / g) > self.t.tiles {
            return Err(fail(
                self.body,
                &op.st,
                "L2.SPACE.012",
                format!(
                    "f32[{m}, {n}] @acc: whole {g}x{g} sub-tiles, at most {}",
                    self.t.tiles
                ),
            ));
        }
        Ok(())
    }
}

/// Python's `str(list)` of ints.
pub fn py_list(v: &[i64]) -> String {
    format!(
        "[{}]",
        v.iter()
            .map(|x| x.to_string())
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn render_gemm(g: &Gemm) -> String {
    let mut emit = match &g.emit {
        Some(e) => format!(" emit={e}"),
        None => String::new(),
    };
    if g.to {
        emit = format!(" emit {}", g.emit.clone().unwrap_or_default());
    }
    let mut offs = String::new();
    if g.aoff != 0 {
        offs += &format!(" aoff={}", g.aoff);
    }
    if g.boff != 0 {
        offs += &format!(" boff={}", g.boff);
    }
    format!(
        "gemm {}x{}x{} a=A{} b=B{} acc={}{offs}{emit}",
        g.gm, g.gn, g.nk, g.ab, g.bb, g.acc
    )
}

/// Each fused drain just before the unit's next emitting sweep, or last.
fn place_held(mut s: Stream) -> Stream {
    for line in s.lines.iter_mut() {
        if let Some(g) = &line.gemm {
            line.text = render_gemm(g);
        }
    }
    // Lines by origin: an original line's index, or a placed drain.
    let mut out: Vec<(Option<usize>, Line)> = s
        .lines
        .iter()
        .cloned()
        .enumerate()
        .map(|(i, l)| (Some(i), l))
        .collect();
    let held = std::mem::take(&mut s.held);
    for (sweep, text) in held {
        let at = out.iter().position(|(o, _)| *o == Some(sweep)).unwrap();
        let nxt = (at + 1..out.len()).find(|&j| {
            out[j]
                .1
                .gemm
                .as_ref()
                .map(|g| g.emit.is_some())
                .unwrap_or(false)
        });
        match nxt {
            None => {
                let seg = out.last().unwrap().1.seg;
                out.push((None, Line::new(text, seg)));
                let last_gemm = out
                    .iter()
                    .enumerate()
                    .filter(|(_, (_, l))| l.gemm.is_some())
                    .map(|(k, _)| k)
                    .max();
                let pos = out.iter().position(|(o, _)| *o == Some(sweep)).unwrap();
                if Some(pos) == last_gemm {
                    s.reading = (None, None);
                }
            }
            Some(j) => {
                let seg = out[j].1.seg;
                out.insert(j, (None, Line::new(text, seg)));
            }
        }
    }
    s.lines = out.into_iter().map(|(_, l)| l).collect();
    s
}
