//! Exact local-word intervals with one access history per homogeneous span.

use std::collections::BTreeMap;

use super::{Clock, Event, Readers};

#[derive(Clone, Default, PartialEq, Eq)]
struct State {
    write: Option<Event>,
    reads: Readers,
}

#[derive(Clone, PartialEq, Eq)]
struct Span {
    end: i64,
    state: State,
}

#[derive(Clone, Default, PartialEq, Eq)]
pub(super) struct Ranges(BTreeMap<i64, Span>);

impl Ranges {
    fn split(&mut self, at: i64) {
        let Some((&lo, span)) = self.0.range(..at).next_back() else {
            return;
        };
        if span.end <= at {
            return;
        }
        let tail = span.clone();
        self.0.get_mut(&lo).expect("indexed span").end = at;
        self.0.insert(at, tail);
    }

    pub fn access(
        &mut self,
        ranges: impl IntoIterator<Item = (i64, i64)>,
        write: bool,
        clock: &Clock,
        event: &Event,
        mut conflict: impl FnMut(i64, &Event),
    ) {
        for (lo, hi) in ranges {
            if lo >= hi {
                continue;
            }
            self.split(lo);
            self.split(hi);
            let keys: Vec<_> = self.0.range(lo..hi).map(|(&at, _)| at).collect();
            let mut cursor = lo;
            for at in keys.into_iter().chain(std::iter::once(hi)) {
                if cursor < at {
                    self.0.insert(
                        cursor,
                        Span {
                            end: at,
                            state: State::default(),
                        },
                    );
                    self.update(cursor, write, clock, event, &mut conflict);
                }
                if at < hi {
                    self.update(at, write, clock, event, &mut conflict);
                    cursor = self.0[&at].end;
                }
            }
            self.merge(lo, hi);
        }
    }

    fn update(
        &mut self,
        at: i64,
        write: bool,
        clock: &Clock,
        event: &Event,
        conflict: &mut impl FnMut(i64, &Event),
    ) {
        let state = &mut self.0.get_mut(&at).expect("indexed span").state;
        if let Some(w) = &state.write {
            if w.epoch > clock.at(w.proc) {
                conflict(at, w);
            }
        }
        if write {
            for (_, r) in std::mem::take(&mut state.reads).all() {
                if r.epoch > clock.at(r.proc) {
                    conflict(at, &r);
                }
            }
            state.write = Some(event.clone());
        } else {
            state.reads.set(event.proc, event);
        }
    }

    fn merge(&mut self, lo: i64, hi: i64) {
        let first = self.0.range(..lo).next_back().map_or(lo, |(&at, _)| at);
        let keys: Vec<_> = self.0.range(first..=hi).map(|(&at, _)| at).collect();
        let mut previous = None;
        for at in keys {
            if let Some(before) = previous {
                if self.0[&before].end == at && self.0[&before].state == self.0[&at].state {
                    let end = self.0.remove(&at).expect("indexed span").end;
                    self.0.get_mut(&before).expect("indexed span").end = end;
                    continue;
                }
            }
            previous = Some(at);
        }
    }

    pub fn retained(&self, stats: &mut crate::sim::stats::Retained) {
        stats.add("local_l1_spans", self.0.len());
        for span in self.0.values() {
            if let Some(event) = &span.state.write {
                stats.object("local_events", std::rc::Rc::as_ptr(event) as usize);
            }
            for (_, event) in span.state.reads.first.iter().chain(&span.state.reads.more) {
                stats.object("local_events", std::rc::Rc::as_ptr(event) as usize);
            }
        }
    }

    pub fn touched(&self) -> (Vec<i64>, Vec<i64>) {
        let mut reads = Vec::new();
        let mut writes = Vec::new();
        for (&at, span) in &self.0 {
            if span.state.reads.first.is_some() {
                reads.extend(at..span.end);
            }
            if span.state.write.is_some() {
                writes.extend(at..span.end);
            }
        }
        (reads, writes)
    }
}
