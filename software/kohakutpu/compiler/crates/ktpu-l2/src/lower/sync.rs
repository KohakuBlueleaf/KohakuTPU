//! Order between the stages of one node's program (L2.ORDER.001-.007): a
//! later stage waits for an earlier one only where both touch one thing.
//!
//! An access is a range of a key: `("mem", BUFFER)` in bytes, a unit's own
//! memory (`("vc[1]", "l1")` words, `("mg[0]", "A")` entries of both banks)
//! or a cluster's receive port (`("mg[0]", "rx")`). Accesses conflict when
//! one writes and the ranges meet, unless one unit touches its own memory
//! both times. A streamed transfer (`flow`) orders itself.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use crate::mutant::on;

mod index;
pub(crate) use index::AccessIndex;

/// The unit of a fence's accesses: what it waits for, never waited on.
pub const FENCE: &str = "fence";

/// Memory ownership is line-granular (L2.ORDER.009).
const LINE: i64 = 64;

/// Most elements a footprint enumerates before it stands as its bounding range.
const ELEMENTS: i64 = 1 << 17;

/// A strided memory footprint inside `[lo, hi)`: `step` bytes at `start` plus
/// every combination of `levels` `(stride, count)`.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Footprint {
    start: i64,
    levels: Vec<(i64, i64)>,
    step: i64,
    /// Its sorted lines, made once; None past [`ELEMENTS`].
    lines: Option<Arc<Vec<i64>>>,
}

impl Footprint {
    pub fn new(start: i64, levels: Vec<(i64, i64)>, step: i64) -> Self {
        let mut f = Footprint {
            start,
            levels,
            step,
            lines: None,
        };
        f.lines = f.enumerate().map(Arc::new);
        f
    }

    /// Source geometry of this immutable footprint.
    pub fn geometry(&self) -> (i64, &[(i64, i64)], i64) {
        (self.start, &self.levels, self.step)
    }

    /// The sorted lines it touches, or None when it holds too many elements.
    fn enumerate(&self) -> Option<Vec<i64>> {
        let count = self
            .levels
            .iter()
            .try_fold(1i64, |n, &(_, c)| n.checked_mul(c.max(1)))?;
        if count > ELEMENTS || self.step <= 0 {
            return None;
        }
        let mut starts = vec![self.start];
        for &(stride, n) in &self.levels {
            let mut next = Vec::with_capacity(starts.len() * n.max(1) as usize);
            for &s in &starts {
                for i in 0..n.max(1) {
                    next.push(s + i * stride);
                }
            }
            starts = next;
        }
        let mut lines = Vec::new();
        for s in starts {
            let (first, last) = (s.div_euclid(LINE), (s + self.step - 1).div_euclid(LINE));
            lines.extend(first..=last);
        }
        lines.sort_unstable();
        lines.dedup();
        Some(lines)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Access {
    /// "mg[0]", "vc[1]", "mover", or [`FENCE`]
    pub unit: String,
    pub write: bool,
    pub key: (String, String),
    pub lo: i64,
    pub hi: i64,
    /// a streamed transfer's ring: credits and counts order it
    pub flow: bool,
    /// the exact memory footprint within `[lo, hi)`, when known
    pub footprint: Option<Arc<Footprint>>,
    /// Descriptor whose base advances between repeated vector sends.
    pub(crate) descriptor: Option<usize>,
}

impl Access {
    pub fn new(unit: &str, write: bool, key: (&str, &str), lo: i64, hi: i64) -> Self {
        Access {
            unit: unit.to_string(),
            write,
            key: (key.0.to_string(), key.1.to_string()),
            lo,
            hi,
            flow: false,
            footprint: None,
            descriptor: None,
        }
    }

    /// A memory access of `step` bytes at `start` over `levels`, its range the
    /// footprint's bounds.
    pub fn strided(
        unit: &str,
        write: bool,
        buf: &str,
        start: i64,
        levels: &[(i64, i64)],
        step: i64,
    ) -> Self {
        let (mut lo, mut hi) = (start, start);
        for &(stride, n) in levels {
            let reach = (n.max(1) - 1) * stride;
            if reach >= 0 {
                hi += reach;
            } else {
                lo += reach;
            }
        }
        let mut a = Access::new(unit, write, ("mem", buf), lo, hi + step);
        a.footprint = Some(Arc::new(Footprint::new(start, levels.to_vec(), step)));
        a
    }

    /// This access moved by `delta` bytes, its footprint with it.
    pub fn shifted(&self, delta: i64) -> Option<Self> {
        let mut a = self.clone();
        a.lo = a.lo.checked_add(delta)?;
        a.hi = a.hi.checked_add(delta)?;
        if let Some(f) = &a.footprint {
            let start = f.start.checked_add(delta)?;
            a.footprint = Some(Arc::new(Footprint::new(start, f.levels.clone(), f.step)));
        }
        Some(a)
    }

    /// Associate a memory effect with its emitted descriptor.
    pub(crate) fn described(mut self, descriptor: usize) -> Self {
        self.descriptor = Some(descriptor);
        self
    }

    /// The union of `count` sends whose descriptor base advances by `step`.
    pub(crate) fn repeated(&self, count: i64, step: i64) -> Option<Self> {
        let mut a = self.clone();
        let reach = count.checked_sub(1)?.checked_mul(step)?;
        a.lo = a.lo.checked_add(reach.min(0))?;
        a.hi = a.hi.checked_add(reach.max(0))?;
        if count > 1 && step != 0 {
            if let Some(f) = &a.footprint {
                let mut levels = f.levels.clone();
                levels.push((step, count));
                a.footprint = Some(Arc::new(Footprint::new(f.start, levels, f.step)));
            }
        }
        Some(a)
    }

    /// Physical identity for dependency comparisons; symbolic scratch stays local.
    pub fn bound(&self, bindings: &BTreeMap<String, i64>) -> crate::ir::Result<Self> {
        if self.key.0 != "mem" {
            return Ok(self.clone());
        }
        let Some(&base) = bindings.get(&self.key.1) else {
            return Ok(self.clone());
        };
        let mut a = self
            .shifted(base)
            .ok_or_else(|| crate::ir::Error::new("L2.ORDER.009", "bound access overflow"))?;
        a.key = ("bound_dram".into(), String::new());
        Ok(a)
    }

    /// The lines it touches: its footprint's, else every line of its range.
    fn lines(&self) -> Option<&[i64]> {
        self.footprint
            .as_ref()
            .and_then(|f| f.lines.as_deref().map(Vec::as_slice))
    }

    /// Memory ownership is line-granular; unit-local ranges retain their units.
    pub fn range(&self) -> (i64, i64) {
        if matches!(self.key.0.as_str(), "mem" | "bound_dram") && self.lo < self.hi {
            (
                self.lo.div_euclid(64) * 64,
                ((self.hi - 1).div_euclid(64) * 64).saturating_add(64),
            )
        } else {
            (self.lo, self.hi)
        }
    }

    pub fn flowing(mut self, flow: bool) -> Self {
        self.flow = flow;
        self
    }
}

/// A byte range of buffer `buf`.
pub fn mem(unit: &str, write: bool, buf: &str, lo: i64, hi: i64) -> Access {
    Access::new(unit, write, ("mem", buf), lo, hi)
}

fn own(a: &Access) -> bool {
    a.key.0 == a.unit && !on("l2.sync/own-clashes")
}

/// Whether two accesses conflict (L2.ORDER.003).
pub fn clash(a: &Access, b: &Access) -> bool {
    may_clash(a, b) && overlap(a, b)
}

/// Resource/owner modes, independent of their addressed intervals.
fn may_clash(a: &Access, b: &Access) -> bool {
    !on("l2.sync/never-clash")
        && a.key == b.key
        && (a.write || b.write)
        && (on("l2.sync/flow-clashes") || !a.flow || !b.flow)
        && !(a.unit == b.unit && own(a) && own(b))
}

/// Geometry of accesses whose resource/owner modes can conflict.
fn overlap(a: &Access, b: &Access) -> bool {
    let whole = on("l2.sync/whole-buffer");
    let (alo, ahi, blo, bhi) = if whole {
        (0, 1 << 40, 0, 1 << 40)
    } else {
        let ((alo, ahi), (blo, bhi)) = (a.range(), b.range());
        (alo, ahi, blo, bhi)
    };
    if alo >= bhi || blo >= ahi {
        return false;
    }
    whole || !matches!(a.key.0.as_str(), "mem" | "bound_dram") || lines_meet(a, b)
}

/// Whether two memory accesses share a line, each by its exact footprint where
/// it has one (a column share and its neighbour meet in range, not in lines).
fn lines_meet(a: &Access, b: &Access) -> bool {
    let range_lines = |x: &Access| {
        let (lo, hi) = x.range();
        (lo.div_euclid(LINE), (hi - 1).div_euclid(LINE))
    };
    match (a.lines(), b.lines()) {
        (None, None) => true,
        (Some(set), None) => {
            let (first, last) = range_lines(b);
            let at = set.partition_point(|&l| l < first);
            set.get(at).is_some_and(|&l| l <= last)
        }
        (None, Some(set)) => {
            let (first, last) = range_lines(a);
            let at = set.partition_point(|&l| l < first);
            set.get(at).is_some_and(|&l| l <= last)
        }
        (Some(x), Some(y)) => {
            let (mut i, mut j) = (0, 0);
            while i < x.len() && j < y.len() {
                match x[i].cmp(&y[j]) {
                    std::cmp::Ordering::Equal => return true,
                    std::cmp::Ordering::Less => i += 1,
                    std::cmp::Ordering::Greater => j += 1,
                }
            }
            false
        }
    }
}

/// The marks `mark-counter` numbers, from 0 a run.
static MARKS: AtomicUsize = AtomicUsize::new(0);

/// Restart `l2.sync/mark-counter`'s numbering.
pub fn reset_marks() {
    MARKS.store(0, Ordering::Relaxed);
}

/// A `pipe` group: (loop id, iteration, K).
pub type Group = (usize, usize, i64);

/// One stage's node lines and effects.
#[derive(Clone, Debug, Default)]
pub struct Stage {
    pub lines: Vec<String>,
    /// the units its lines send to (a mark after the stage names each)
    pub units: Vec<String>,
    pub accesses: Vec<Access>,
    pub group: Option<Group>,
    /// the posted move's token, for a mover stage
    pub post: Option<String>,
    pub blocking_mover: bool,
    /// a blocking move: everything is complete after it
    pub closes: bool,
    /// the lines as segments the node may interleave with another stage's
    pub segs: Vec<Vec<String>>,
    /// a cross-node handshake: nothing is sent ahead of it
    pub fence: bool,
    /// its phase: a run of a `par` over nodes
    pub phase: i64,
    /// the stages a stream joins into this one (`lower::grouped`)
    pub members: Vec<Stage>,
}

impl Stage {
    pub fn new(lines: Vec<String>, units: Vec<String>, accesses: Vec<Access>) -> Self {
        Stage {
            lines,
            units,
            accesses,
            phase: -1,
            ..Default::default()
        }
    }
}

enum Out {
    Line(String),
    Mark(String, String),
}

#[derive(Default)]
struct State {
    out: Vec<Out>,
    /// unit -> emission position of the latest stage waited on through
    done: HashMap<String, i64>,
    /// stage -> emission position
    pos: HashMap<usize, usize>,
    /// stages in emission order
    order: Vec<usize>,
    /// mark names a wait names
    used: HashSet<String>,
}

fn mark(s: usize, unit: &str) -> String {
    if on("l2.sync/mark-counter") {
        let n = MARKS.fetch_add(1, Ordering::Relaxed);
        return format!("k{n}_{}", &unit[..unit.len().min(2)]);
    }
    format!("k{}_{}", s, unit.replace(['[', ']'], ""))
}

/// The node's lines: the stages in program order, each preceded by the waits
/// its conflicts need; a `pipe` group's later iterations sent ahead where they
/// need no wait. Ends with a barrier (L2.ORDER.004-.006).
pub fn schedule(stages: &[Stage]) -> Vec<String> {
    let effects: Vec<_> = stages
        .iter()
        .map(|stage| AccessIndex::new(&stage.accesses))
        .collect();
    let mut st = State::default();
    for s in 0..stages.len() {
        if st.pos.contains_key(&s) {
            continue;
        }
        let mut pending = needs(&effects, &st, s);
        if !pending.is_empty() && stages[s].group.is_some() {
            for j in ahead(stages, s) {
                if !st.pos.contains_key(&j) && hoistable(stages, &effects, &st, j) {
                    let pending = needs(&effects, &st, j);
                    emit_ready(stages, &mut st, j, pending);
                }
            }
            pending = needs(&effects, &st, s);
        }
        emit_ready(stages, &mut st, s, pending);
    }
    let mut out = Vec::new();
    for ln in &st.out {
        match ln {
            Out::Mark(name, unit) => {
                if st.used.contains(name) || on("l2.sync/every-mark") {
                    out.push(format!("{name} = mark {unit}"));
                }
            }
            Out::Line(l) => out.push(l.clone()),
        }
    }
    if let Some(&last) = st.order.last() {
        if stages[last].closes {
            return out;
        }
    }
    let blocking_mover = stages.iter().any(|s| s.blocking_mover);
    let posts: Vec<&String> = st
        .order
        .iter()
        .filter(|&&s| !blocking_mover || !complete(&st, s, "mover"))
        .filter_map(|&s| stages[s].post.as_ref())
        .collect();
    if let Some(p) = posts.last() {
        out.push(format!("wait moves {p}"));
    }
    out.push("barrier".to_string());
    out
}

fn complete(st: &State, p: usize, unit: &str) -> bool {
    st.done.get(unit).copied().unwrap_or(-1) >= st.pos[&p] as i64
}

/// `(earlier stage, access)` emitted, not yet complete, that `s` must wait for.
fn needs<'a>(effects: &[AccessIndex<'a>], st: &State, s: usize) -> Vec<(usize, &'a Access)> {
    let mut got = Vec::new();
    for &p in &st.order {
        got.extend(
            effects[p]
                .pending(&effects[s], |unit| unit == FENCE || complete(st, p, unit))
                .into_iter()
                .map(|access| (p, access)),
        );
    }
    got
}

fn ahead(stages: &[Stage], s: usize) -> Vec<usize> {
    if on("l2.sync/no-ahead") {
        return vec![];
    }
    let (lp, it, pipe) = stages[s].group.unwrap();
    (s + 1..stages.len())
        .filter(|&j| match stages[j].group {
            Some((l, i, _)) => l == lp && (i as i64) < it as i64 + pipe,
            None => false,
        })
        .collect()
}

fn hoistable(stages: &[Stage], effects: &[AccessIndex<'_>], st: &State, j: usize) -> bool {
    if !needs(effects, st, j).is_empty() {
        return false;
    }
    for k in 0..j {
        if st.pos.contains_key(&k) {
            continue;
        }
        if stages[k].fence || stages[k].units.iter().any(|u| stages[j].units.contains(u)) {
            return false;
        }
        if effects[k].blocks(&effects[j]) {
            return false;
        }
    }
    true
}

fn emit_ready(stages: &[Stage], st: &mut State, s: usize, pending: Vec<(usize, &Access)>) {
    // unit -> latest emission position to wait through; ordered by position.
    let mut waits: BTreeMap<String, usize> = BTreeMap::new();
    let mut first_seen: Vec<String> = Vec::new();
    for (p, a) in pending {
        let unit = if a.unit == "mover" {
            "mover"
        } else {
            a.unit.as_str()
        };
        let at = st.pos[&p];
        match waits.get_mut(unit) {
            Some(v) => *v = (*v).max(at),
            None => {
                waits.insert(unit.to_string(), at);
                first_seen.push(unit.to_string());
            }
        }
    }
    // Python: sorted(waits.items(), key=position), stable over insertion order.
    let mut items: Vec<(String, usize)> = first_seen
        .into_iter()
        .map(|u| {
            let at = waits[&u];
            (u, at)
        })
        .collect();
    items.sort_by_key(|(_, at)| *at);
    for (unit, at) in items {
        let p = st.order[at];
        if unit == "mover" {
            st.out.push(Out::Line(format!(
                "wait moves {}",
                stages[p].post.clone().unwrap_or_default()
            )));
            // A posted move's wait covers every move before it.
            for &q in &st.order[..=at] {
                if stages[q].post.is_some() && !on("l2.sync/forget-done") {
                    let cur = st.done.get("mover").copied().unwrap_or(-1);
                    st.done
                        .insert("mover".to_string(), cur.max(st.pos[&q] as i64));
                }
            }
            continue;
        }
        let name = mark(p, &unit);
        st.used.insert(name.clone());
        st.out.push(Out::Line(format!("wait {unit} upto={name}")));
        if !on("l2.sync/forget-done") {
            let cur = st.done.get(&unit).copied().unwrap_or(-1);
            st.done.insert(unit.clone(), cur.max(at as i64));
        }
    }
    st.pos.insert(s, st.order.len());
    st.order.push(s);
    for ln in &stages[s].lines {
        st.out.push(Out::Line(ln.clone()));
    }
    if stages[s].blocking_mover {
        st.done.insert("mover".into(), st.pos[&s] as i64);
    }
    for u in &stages[s].units {
        st.out.push(Out::Mark(mark(s, u), u.clone()));
    }
}
