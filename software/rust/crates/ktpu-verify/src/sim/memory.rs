//! Races in a unit's local memory (verify.md s7).
//!
//! Every access to a location is an event with a clock. Two accesses race
//! when one writes and neither happens before the other. Accesses arrive in
//! the run's order, which extends happens-before, so per location it
//! suffices to keep the last write and, per process, the last read since.

use std::collections::{BTreeMap, HashSet};
use std::fmt;
use std::rc::Rc;

use crate::model::clock::Clock;
use crate::model::finding::{Finding, Kind};

/// Race findings kept per unit.
pub const LIMIT: u32 = 8;
/// Locations below this are held in a vector indexed by location; others
/// (negative or past it) in a map.
const DENSE: i64 = 1 << 20;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub(crate) enum Space {
    A,
    B,
    Acc,
    L1,
}

/// One value per location of a space.
struct Locs<T> {
    dense: Vec<Option<T>>,
    sparse: BTreeMap<i64, T>,
}

impl<T> Default for Locs<T> {
    fn default() -> Self {
        Locs {
            dense: Vec::new(),
            sparse: BTreeMap::new(),
        }
    }
}

impl<T: Default> Locs<T> {
    fn get(&self, loc: i64) -> Option<&T> {
        if (0..DENSE).contains(&loc) {
            self.dense.get(loc as usize).and_then(Option::as_ref)
        } else {
            self.sparse.get(&loc)
        }
    }

    fn remove(&mut self, loc: i64) -> Option<T> {
        if (0..DENSE).contains(&loc) {
            self.dense.get_mut(loc as usize).and_then(Option::take)
        } else {
            self.sparse.remove(&loc)
        }
    }

    fn entry(&mut self, loc: i64) -> &mut T {
        if (0..DENSE).contains(&loc) {
            let i = loc as usize;
            if i >= self.dense.len() {
                self.dense.resize_with(i + 1, || None);
            }
            self.dense[i].get_or_insert_with(T::default)
        } else {
            self.sparse.entry(loc).or_default()
        }
    }

    /// Every location holding a value, ascending.
    fn locations(&self) -> Vec<i64> {
        let mut out: Vec<i64> = self.sparse.keys().copied().collect();
        out.extend(
            self.dense
                .iter()
                .enumerate()
                .filter(|(_, v)| v.is_some())
                .map(|(i, _)| i as i64),
        );
        out.sort_unstable();
        out
    }
}

impl fmt::Display for Space {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Space::A => "A",
            Space::B => "B",
            Space::Acc => "acc",
            Space::L1 => "L1",
        })
    }
}

/// What made an access, as a race finding names it; written out only for
/// a finding.
#[derive(Clone, Debug)]
pub(crate) enum What {
    /// `{op} word {pc}`: a cluster word.
    Word(&'static str, i64),
    /// `{op} at pc {pc}`: a vector core word.
    AtPc(&'static str, i64),
    /// `a burst from {sender}`.
    Burst(Rc<str>),
}

impl fmt::Display for What {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            What::Word(op, pc) => write!(f, "{op} word {pc}"),
            What::AtPc(op, pc) => write!(f, "{op} at pc {pc}"),
            What::Burst(from) => write!(f, "a burst from {from}"),
        }
    }
}

struct Access {
    clock: Clock,
    proc: usize,
    what: What,
}

/// One access call's event, shared by every location it touches.
type Event = Rc<Access>;

/// A location's reads since its last write, one per process, in first-read
/// order: the first held in place, the rest spilled.
#[derive(Default)]
struct Readers {
    first: Option<(usize, Event)>,
    more: Vec<(usize, Event)>,
}

impl Readers {
    /// Process `proc`'s read is now `e`.
    fn set(&mut self, proc: usize, e: &Event) {
        match &mut self.first {
            None => self.first = Some((proc, e.clone())),
            Some(slot) if slot.0 == proc => slot.1 = e.clone(),
            Some(_) => match self.more.iter_mut().find(|(p, _)| *p == proc) {
                Some(slot) => slot.1 = e.clone(),
                None => self.more.push((proc, e.clone())),
            },
        }
    }

    /// Every read, in first-read order.
    fn all(self) -> impl Iterator<Item = (usize, Event)> {
        self.first.into_iter().chain(self.more)
    }
}

pub(crate) struct Memory {
    name: String,
    /// Per space (`Space as usize`): location -> its last write.
    writes: [Locs<Option<Event>>; 4],
    /// Per space: location -> its reads since the last write.
    reads: [Locs<Readers>; 4],
    found: u32,
    seen: HashSet<(String, String)>,
}

impl Memory {
    pub fn new(name: &str) -> Self {
        Memory {
            name: name.to_string(),
            writes: Default::default(),
            reads: Default::default(),
            found: 0,
            seen: HashSet::new(),
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn access(
        &mut self,
        out: &mut Vec<Finding>,
        space: Space,
        locs: impl IntoIterator<Item = i64>,
        write: bool,
        clock: &Clock,
        proc: usize,
        what: &What,
    ) {
        let e: Event = Rc::new(Access {
            clock: clock.clone(),
            proc,
            what: what.clone(),
        });
        let s = space as usize;
        for loc in locs {
            let key = (space, loc);
            if let Some(Some(w)) = self.writes[s].get(loc) {
                if w.clock.at(w.proc) > clock.at(w.proc) {
                    let w = w.clone();
                    self.race(out, key, &w, &e);
                }
            }
            if write {
                if let Some(rs) = self.reads[s].remove(loc) {
                    for (_, r) in rs.all() {
                        if r.clock.at(r.proc) > clock.at(r.proc) {
                            self.race(out, key, &r, &e);
                        }
                    }
                }
                *self.writes[s].entry(loc) = Some(e.clone());
            } else {
                self.reads[s].entry(loc).set(proc, &e);
            }
        }
    }

    fn race(&mut self, out: &mut Vec<Finding>, key: (Space, i64), a: &Access, b: &Access) {
        let pair = (a.what.to_string(), b.what.to_string());
        if self.seen.contains(&pair) || self.found >= LIMIT {
            return;
        }
        let text = format!(
            "{} {}: {} and {} are not ordered",
            key.0, key.1, pair.0, pair.1
        );
        self.seen.insert(pair);
        self.found += 1;
        out.push(Finding::new(Kind::Race, self.name.clone(), text));
    }

    /// The locations of `space` with a read since their last write, and
    /// those written, sorted.
    pub fn touched(&self, space: Space) -> (Vec<i64>, Vec<i64>) {
        let s = space as usize;
        (self.reads[s].locations(), self.writes[s].locations())
    }
}
