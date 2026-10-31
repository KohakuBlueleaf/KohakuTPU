//! The fill throttle (L2.VC.027): a `vsync fe on=fill` before a fill that
//! would lose a completion entry (ISA.VC.193), and nothing in an image that
//! loses none.
//!
//! The walk follows dispatch order and keeps, for each queue and for the front
//! end, how many fills' landings its next instruction is ordered after. It
//! reads the syncs and the queues' order and no register dependence, so a fill
//! it holds is at least the verifier's.

use std::borrow::Cow;
use std::collections::{BTreeMap, HashMap};

use ktpu_l1::lower::ops::is_math;
use ktpu_l1::sched::timing::walk_words;
use ktpu_l1::text::{fmt, read, Module, Stmt, Term};
use ktpu_machine::fill_lost;

use crate::ir::Result;
use crate::mutant::on;
use crate::target::Target;

const FILL: usize = 0;
const UNPACK: usize = 1;
const MATH: usize = 2;
const PACK: usize = 3;
const DRAIN: usize = 4;

fn kind_of(name: &str) -> Option<usize> {
    ["fill", "unpack", "math", "pack", "drain"]
        .iter()
        .position(|k| *k == name)
}

/// One walk of an image: `syncs` is what the throttle has placed so far, by
/// the line of the fill each stands before, as that sync's slack.
struct Walk<'a> {
    depth: u32,
    syncs: &'a BTreeMap<u32, u32>,
    fills: u32,
    /// Each queue's next instruction, and the front end's, is after this many landings.
    queue: [u32; 5],
    front: u32,
    /// By kind: the landings each finished instruction is after.
    done: [Vec<u32>; 5],
    track: Vec<i64>,
    words: HashMap<String, i64>,
    scalars: HashMap<String, i64>,
    lost: Option<(u32, u32)>,
}

impl Walk<'_> {
    fn finished(&self, kind: usize, slack: u32) -> u32 {
        let upto = self.done[kind].len().saturating_sub(slack as usize);
        upto.checked_sub(1).map_or(0, |i| self.done[kind][i])
    }

    fn enter(&mut self, queue: usize, kind: Option<usize>) {
        self.queue[queue] = self.queue[queue].max(self.front);
        if let Some(k) = kind {
            let after = self.queue[queue];
            self.done[k].push(after);
        }
    }

    /// The fill of `st`, or its line and the slack that saves its entry.
    fn fill(&mut self, st: &Stmt) {
        if let Some(&slack) = self.syncs.get(&st.line) {
            self.front = self.front.max(self.finished(FILL, slack));
        }
        self.enter(FILL, None);
        let name = st.positional().first().map(|t| fmt(t));
        let words = name.and_then(|n| self.words.get(&n).copied());
        let words = words.unwrap_or(1);
        let held = self.fills + 1 - self.queue[FILL];
        let slot = (self.fills % self.depth) as usize;
        let shadowed = std::mem::replace(&mut self.track[slot], words);
        if fill_lost(self.depth, held, shadowed, words) {
            let keep = if shadowed == words {
                2 * self.depth
            } else {
                self.depth + 1
            };
            self.lost = Some((st.line, keep - 2));
        }
        self.fills += 1;
        self.done[FILL].push(self.fills);
    }

    fn sync(&mut self, st: &Stmt) {
        let positional = st.positional();
        let waiter = positional.first().and_then(|t| t.as_name());
        let kind = st.kw("on").and_then(|t| t.as_name()).and_then(kind_of);
        let slack = st.kw("slack").and_then(|t| t.as_int()).unwrap_or(0) as u32;
        let (Some(waiter), Some(kind)) = (waiter, kind) else {
            return;
        };
        let after = self.finished(kind, slack);
        match kind_of(waiter) {
            Some(q) => {
                self.queue[q] = self.queue[q].max(self.front).max(after);
            }
            None if waiter == "fe" => self.front = self.front.max(after),
            None => {}
        }
    }

    fn walk(&mut self, stmts: &[Stmt]) {
        for st in stmts {
            if self.lost.is_some() {
                return;
            }
            match st.op.as_str() {
                "desc" => {
                    if let (Some(Term::Assign(lhs, _)), Some(walk)) =
                        (st.args.first(), st.kw("walk"))
                    {
                        self.words.insert(fmt(lhs), walk_words(walk));
                    }
                }
                "seti" => {
                    if let [Term::Assign(a, b)] = st.args.as_slice() {
                        if let (Some(n), Some(v)) = (a.as_name(), b.as_int()) {
                            self.scalars.insert(n.to_string(), v as i64);
                        }
                    }
                }
                "loop" => {
                    let trips = st.positional().first().and_then(|t| t.as_name());
                    let trips = trips.and_then(|n| self.scalars.get(n).copied());
                    // A pass holds at least one fill or none: the lag settles or
                    // crosses the wrap limit within this many; an unset count is past it.
                    let cap = 4 * i64::from(self.depth);
                    for _ in 0..trips.unwrap_or(cap).clamp(1, cap) {
                        self.walk(&st.body);
                    }
                }
                "skipz" => self.walk(&st.body),
                "vfill" => self.fill(st),
                "vsync" => self.sync(st),
                "vunpk" => self.enter(UNPACK, Some(UNPACK)),
                "vpack" => self.enter(PACK, Some(PACK)),
                "vdrain" => self.enter(DRAIN, Some(DRAIN)),
                "vpost" => self.enter(DRAIN, None),
                "vbar" => {
                    self.enter(UNPACK, None);
                    let landed = self.finished(FILL, 0);
                    self.queue[UNPACK] = self.queue[UNPACK].max(landed);
                }
                op if is_math(op) => self.enter(MATH, Some(MATH)),
                _ => {}
            }
        }
    }
}

/// The syncs `module`'s fills need: by the line of the fill each stands
/// before, as its slack.
fn syncs_of(module: &Module, depth: u32) -> BTreeMap<u32, u32> {
    let mut syncs = BTreeMap::new();
    for d in module.images.values() {
        loop {
            let mut w = Walk {
                depth,
                syncs: &syncs,
                fills: 0,
                queue: [0; 5],
                front: 0,
                done: Default::default(),
                track: vec![0; depth as usize],
                words: HashMap::new(),
                scalars: HashMap::new(),
                lost: None,
            };
            w.walk(&d.stmts);
            let Some((line, slack)) = w.lost else { break };
            let at = syncs.entry(line).or_insert(slack);
            *at = slack.min(*at);
        }
    }
    syncs
}

/// `image` with a sync before each line of `syncs`.
fn with_syncs(image: &str, syncs: &BTreeMap<u32, u32>) -> String {
    let mut lines: Vec<String> = image.lines().map(str::to_string).collect();
    for (&line, slack) in syncs.iter().rev() {
        let at = line as usize - 1;
        let indent = lines[at].len() - lines[at].trim_start().len();
        let sync = format!("{}vsync fe on=fill slack={slack}", " ".repeat(indent));
        lines.insert(at, sync);
    }
    lines.join("\n")
}

/// `image`'s text with the syncs its fills need, `image` itself when none.
pub fn throttle(image: String, t: &Target) -> Result<String> {
    if !image.contains("vfill ") || on("l2.fill/no-throttle") {
        return Ok(image);
    }
    let syncs = syncs_of(&read(&image, "<image>")?, t.fill_track_depth);
    match syncs.is_empty() {
        true => Ok(image),
        false => Ok(with_syncs(&image, &syncs)),
    }
}

/// `module` with [`throttle`]'s syncs: the image the passes after the search
/// take, and so the one every fit is decided on (L2.SEARCH.001).
pub fn throttled<'a>(module: &'a Module, t: &Target) -> Result<Cow<'a, Module>> {
    if on("l2.fill/no-throttle") || on("l2.vector/fit-unthrottled") {
        return Ok(Cow::Borrowed(module));
    }
    let syncs = syncs_of(module, t.fill_track_depth);
    if syncs.is_empty() {
        return Ok(Cow::Borrowed(module));
    }
    let mut got = read(&with_syncs(&module.source, &syncs), &module.file)?;
    (got.vregs, got.vcopies) = (module.vregs, module.vcopies);
    Ok(Cow::Owned(got))
}
