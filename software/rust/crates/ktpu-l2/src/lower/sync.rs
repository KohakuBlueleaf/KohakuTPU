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

use crate::mutant::on;

/// The unit of a fence's accesses: what it waits for, never waited on.
pub const FENCE: &str = "fence";

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
    if on("l2.sync/never-clash") {
        return false;
    }
    let whole = on("l2.sync/whole-buffer");
    let (alo, ahi, blo, bhi) = if whole {
        (0, 1 << 40, 0, 1 << 40)
    } else {
        (a.lo, a.hi, b.lo, b.hi)
    };
    let flows = !on("l2.sync/flow-clashes") && a.flow && b.flow;
    if a.key != b.key || !(a.write || b.write) || flows {
        return false;
    }
    if alo >= bhi || blo >= ahi {
        return false;
    }
    !(a.unit == b.unit && own(a) && own(b))
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
    let mut st = State::default();
    for s in 0..stages.len() {
        if st.pos.contains_key(&s) {
            continue;
        }
        if !needs(stages, &st, s).is_empty() && stages[s].group.is_some() {
            for j in ahead(stages, s) {
                if !st.pos.contains_key(&j) && hoistable(stages, &st, j) {
                    emit(stages, &mut st, j);
                }
            }
        }
        emit(stages, &mut st, s);
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
    let posts: Vec<&String> = st
        .order
        .iter()
        .filter(|&&s| !stages.iter().any(|s| s.blocking_mover) || !complete(&st, s, "mover"))
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
fn needs<'a>(stages: &'a [Stage], st: &State, s: usize) -> Vec<(usize, &'a Access)> {
    let mut got = Vec::new();
    for &p in &st.order {
        for a in &stages[p].accesses {
            if a.unit == FENCE || complete(st, p, &a.unit) {
                continue;
            }
            if stages[s].accesses.iter().any(|b| clash(a, b)) {
                got.push((p, a));
            }
        }
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

fn hoistable(stages: &[Stage], st: &State, j: usize) -> bool {
    if !needs(stages, st, j).is_empty() {
        return false;
    }
    for k in 0..j {
        if st.pos.contains_key(&k) {
            continue;
        }
        if stages[k].fence || stages[k].units.iter().any(|u| stages[j].units.contains(u)) {
            return false;
        }
        for a in &stages[k].accesses {
            for b in &stages[j].accesses {
                if a.key == b.key && (a.write || b.write) && a.lo < b.hi && b.lo < a.hi {
                    return false;
                }
            }
        }
    }
    true
}

fn emit(stages: &[Stage], st: &mut State, s: usize) {
    // unit -> latest emission position to wait through; ordered by position.
    let mut waits: BTreeMap<String, usize> = BTreeMap::new();
    let mut first_seen: Vec<String> = Vec::new();
    for (p, a) in needs(stages, st, s) {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn acc(unit: &str, write: bool, key: (&str, &str), lo: i64, hi: i64) -> Access {
        Access::new(unit, write, key, lo, hi)
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_ORDER_003_conflicts() {
        let a = acc("mg[0]", true, ("mem", "y"), 0, 64);
        let b = acc("vc[0]", false, ("mem", "y"), 32, 96);
        assert!(clash(&a, &b));
        let c = acc("vc[0]", false, ("mem", "y"), 64, 96);
        assert!(!clash(&a, &c), "ranges that only touch do not meet");
        let r = acc("vc[0]", false, ("mem", "y"), 0, 64);
        let r2 = acc("vc[1]", false, ("mem", "y"), 0, 64);
        assert!(!clash(&r, &r2), "two reads never conflict");
        let own1 = acc("vc[0]", true, ("vc[0]", "l1"), 0, 8);
        let own2 = acc("vc[0]", true, ("vc[0]", "l1"), 0, 8);
        assert!(
            !clash(&own1, &own2),
            "a unit's own memory is ordered by its run"
        );
        let f1 = acc("mg[0]", true, ("vc[0]", "l1"), 0, 8).flowing(true);
        let f2 = acc("vc[0]", false, ("vc[0]", "l1"), 0, 8).flowing(true);
        assert!(!clash(&f1, &f2), "two streamed accesses never conflict");
        assert!(clash(&f1, &acc("vc[0]", false, ("vc[0]", "l1"), 0, 8)));
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_ORDER_004_wait_the_conflicting_unit() {
        let s0 = Stage::new(
            vec!["send vc[0]".into()],
            vec!["vc[0]".into()],
            vec![acc("vc[0]", true, ("mem", "y"), 0, 64)],
        );
        let s1 = Stage::new(
            vec!["send vc[1]".into()],
            vec!["vc[1]".into()],
            vec![acc("vc[1]", true, ("mem", "z"), 0, 64)],
        );
        let s2 = Stage::new(
            vec!["send mg[0]".into()],
            vec!["mg[0]".into()],
            vec![acc("mg[0]", false, ("mem", "y"), 0, 64)],
        );
        let got = schedule(&[s0, s1, s2]);
        assert_eq!(
            got,
            vec![
                "send vc[0]",
                "k0_vc0 = mark vc[0]",
                "send vc[1]",
                "wait vc[0] upto=k0_vc0",
                "send mg[0]",
                "barrier"
            ]
        );
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_ORDER_005_posted_then_barrier() {
        let mut m = Stage::new(
            vec!["mv0 = move posted".into(), "    copy x".into()],
            vec![],
            vec![acc("mover", true, ("mem", "y"), 0, 64)],
        );
        m.post = Some("mv0".into());
        let v = Stage::new(
            vec!["send vc[0]".into()],
            vec!["vc[0]".into()],
            vec![acc("vc[0]", false, ("mem", "y"), 0, 32)],
        );
        let got = schedule(&[m, v]);
        assert_eq!(
            got,
            vec![
                "mv0 = move posted",
                "    copy x",
                "wait moves mv0",
                "send vc[0]",
                "wait moves mv0",
                "barrier"
            ]
        );
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_ORDER_006_pipe_sends_ahead() {
        // Iteration 0's core reads what its cluster wrote; iteration 1's
        // cluster touches nothing of it and runs on another unit: sent ahead.
        let mut a0 = Stage::new(
            vec!["send mg[0]".into()],
            vec!["mg[0]".into()],
            vec![acc("mg[0]", true, ("mem", "h"), 0, 64)],
        );
        a0.group = Some((1, 0, 2));
        let mut b0 = Stage::new(
            vec!["send vc[0]".into()],
            vec!["vc[0]".into()],
            vec![acc("vc[0]", false, ("mem", "h"), 0, 64)],
        );
        b0.group = Some((1, 0, 2));
        let mut a1 = Stage::new(
            vec!["send mg[1]".into()],
            vec!["mg[1]".into()],
            vec![acc("mg[1]", true, ("mem", "h"), 64, 128)],
        );
        a1.group = Some((1, 1, 2));
        let got = schedule(&[a0, b0, a1]);
        assert_eq!(
            got,
            vec![
                "send mg[0]",
                "k0_mg0 = mark mg[0]",
                "send mg[1]",
                "wait mg[0] upto=k0_mg0",
                "send vc[0]",
                "barrier"
            ]
        );
    }
}
