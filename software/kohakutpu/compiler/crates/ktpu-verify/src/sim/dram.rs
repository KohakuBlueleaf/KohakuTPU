//! Card-wide physical intervals, occupied from issue until completion.
//! RAW/WAR/WAW are byte conflicts; independent DRAM writers also own whole
//! 64-byte lines (round-i verification, L1.VER.009 and L1.VER.017).

use std::collections::BTreeMap;
use std::rc::Rc;

use crate::model::clock::Clock;
use crate::model::finding::{Finding, Kind};

use super::fx::FxHashSet;

mod history;
use history::History;

pub(crate) const LIMIT: usize = 2_000_000;
pub(crate) type Footprint = Vec<(u64, u64)>;

#[derive(Clone, PartialEq, Eq)]
struct Access {
    start: Clock,
    done: u32,
    owner: usize,
    write: bool,
    what: String,
}

#[derive(Clone, PartialEq, Eq)]
struct Span {
    end: u64,
    // One history per completion process and access direction. The number of
    // processes is fixed by the target, not by the number of issued accesses.
    frontier: BTreeMap<(usize, bool), History>,
}

impl Span {
    fn new(end: u64, access: &Rc<Access>) -> Self {
        let mut history = History::default();
        history.push(access);
        Self {
            end,
            frontier: BTreeMap::from([((access.owner, access.write), history)]),
        }
    }
}

#[derive(Clone, Default, PartialEq, Eq)]
struct Intervals(BTreeMap<u64, Span>);

impl Intervals {
    fn retire(&mut self, floor: &Clock) {
        self.0.retain(|_, span| {
            span.frontier.retain(|_, history| {
                history.retire(floor);
                !history.is_empty()
            });
            !span.frontier.is_empty()
        });
    }

    fn split(&mut self, at: u64) {
        let Some((&lo, span)) = self.0.range(..at).next_back() else {
            return;
        };
        if span.end <= at {
            return;
        }
        let tail = span.clone();
        if let Some(span) = self.0.get_mut(&lo) {
            span.end = at;
        }
        self.0.insert(at, tail);
    }

    fn visit(
        &mut self,
        ranges: Footprint,
        current: &Rc<Access>,
        kind: Kind,
        work: &mut usize,
        reported: &mut FxHashSet<(String, String)>,
        out: &mut Vec<Finding>,
    ) -> Result<(), ()> {
        for (lo, hi) in ranges {
            if lo == hi {
                continue;
            }
            self.split(lo);
            self.split(hi);
            let keys: Vec<_> = self.0.range(lo..hi).map(|(&at, _)| at).collect();
            let mut cursor = lo;
            for at in keys {
                if cursor < at {
                    self.0.insert(cursor, Span::new(at, current));
                }
                let span = self.0.get_mut(&at).expect("indexed span");
                for (&(owner, write), history) in &span.frontier {
                    if !(write || current.write) {
                        continue;
                    }
                    // memory-protocol §7.1 and mag_mem_port's ready-order FIFO
                    // order WAW on one source/destination response stream. They
                    // do not publish its commit to a reader (§7.2).
                    if write && current.write && owner == current.owner {
                        continue;
                    }
                    history.conflicts(current, |previous| {
                        *work += 1;
                        if *work > LIMIT {
                            return Err(());
                        }
                        let Some(previous) = previous else {
                            return Ok(());
                        };
                        if reported.insert((previous.what.clone(), current.what.clone())) {
                            let hazard = match (previous.write, current.write) {
                                (true, true) => "WAW",
                                (true, false) => "RAW",
                                _ => "WAR",
                            };
                            out.push(Finding::new(
                                kind,
                                "shared physical memory",
                                format!(
                                    "{hazard} at {at:#x}: {} and {} are not ordered",
                                    previous.what, current.what
                                ),
                            ));
                        }
                        Ok(())
                    })?;
                }
                // FIFO alone cannot discard an older write: a later-visited
                // read may precede its successor yet race the older commit.
                span.frontier
                    .entry((current.owner, current.write))
                    .or_default()
                    .push(current);
                cursor = span.end;
                *work += 1;
                if *work > LIMIT {
                    return Err(());
                }
            }
            if cursor < hi {
                self.0.insert(cursor, Span::new(hi, current));
            }
            *work += 1;
            if *work > LIMIT {
                return Err(());
            }
        }
        Ok(())
    }
}

#[derive(Clone, Default, PartialEq, Eq)]
pub(crate) struct GlobalDram {
    bytes: Intervals,
    writers: Intervals,
    reported: FxHashSet<(String, String)>,
    work: usize,
    refused: bool,
}

impl GlobalDram {
    pub fn retained(&self, stats: &mut super::stats::Retained) {
        for (kind, intervals) in [("dram_bytes", &self.bytes), ("dram_lines", &self.writers)] {
            stats.add(kind, intervals.0.len());
            for span in intervals.0.values() {
                for (&(owner, write), history) in &span.frontier {
                    stats.add(format!("{kind}/stream/{owner}/{write}"), 1);
                    history.retained(stats);
                }
            }
        }
    }

    pub fn retire(&mut self, floor: &Clock) {
        self.bytes.retire(floor);
        self.writers.retire(floor);
    }

    #[allow(clippy::too_many_arguments)]
    pub fn access(
        &mut self,
        out: &mut Vec<Finding>,
        footprint: Result<Footprint, String>,
        write: bool,
        start: &Clock,
        done: &Clock,
        owner: usize,
        what: String,
        special: u64,
    ) {
        if self.refused {
            return;
        }
        let ranges = match footprint.and_then(normalize) {
            Ok(ranges) => ranges,
            Err(why) => {
                out.push(Finding::new(Kind::Model, what, why));
                return;
            }
        };
        let lines = if write {
            normalize(
                ranges
                    .iter()
                    .filter(|&&(lo, hi)| lo < hi && lo & special == 0)
                    .map(|&(lo, hi)| (lo / 64 * 64, ((hi - 1) / 64 + 1) * 64))
                    .collect(),
            )
        } else {
            Ok(Vec::new())
        };
        let current = Rc::new(Access {
            start: start.clone(),
            done: done.at(owner),
            owner,
            write,
            what,
        });
        let result = self.bytes.visit(
            ranges,
            &current,
            Kind::Race,
            &mut self.work,
            &mut self.reported,
            out,
        );
        let result = result.and_then(|()| {
            self.writers.visit(
                lines.map_err(|_| ())?,
                &current,
                Kind::Line,
                &mut self.work,
                &mut self.reported,
                out,
            )
        });
        if result.is_err() {
            self.refused = true;
            out.push(Finding::new(
                Kind::Model,
                "shared physical memory",
                "global DRAM proof budget exhausted",
            ));
        }
    }
}

fn normalize(mut ranges: Footprint) -> Result<Footprint, String> {
    if ranges.len() > LIMIT {
        return Err("physical footprint exceeds proof budget".into());
    }
    ranges.sort_unstable();
    let mut out: Footprint = Vec::new();
    for (lo, hi) in ranges {
        if lo > hi {
            return Err("invalid physical interval".into());
        }
        if lo == hi {
            continue;
        }
        if let Some(last) = out.last_mut() {
            if lo <= last.1 {
                last.1 = last.1.max(hi);
                continue;
            }
        }
        out.push((lo, hi));
    }
    Ok(out)
}

pub(crate) fn interval(base: u64, bytes: u64) -> Result<Footprint, String> {
    let end = base
        .checked_add(bytes)
        .ok_or("physical interval overflow")?;
    Ok(vec![(base, end)])
}

/// Innermost-first descriptor walk; zero bounds represent one word.
pub(crate) fn walk(
    base: i128,
    dims: impl IntoIterator<Item = (i64, u64)>,
    bytes: u64,
) -> Result<Footprint, String> {
    let end = base
        .checked_add(i128::from(bytes))
        .ok_or("physical address overflow")?;
    let mut dims: Vec<_> = dims.into_iter().map(|(s, n)| (s, n.max(1))).collect();
    let mut points = 1usize;
    for &(_, count) in &dims {
        if count > LIMIT as u64 || points.saturating_mul(count as usize) > LIMIT {
            return Err("dynamic footprint exceeds the global DRAM proof budget".into());
        }
        points *= count as usize;
    }
    // Cartesian translation sums commute. Small strides expose contiguous
    // unions before outer dimensions replicate them.
    dims.sort_unstable_by_key(|&(stride, _)| i128::from(stride).abs());
    let mut ranges = vec![(base, end)];
    for (stride, count) in dims {
        if stride == 0 || count == 1 {
            continue;
        }
        let stride = i128::from(stride);
        let advance = stride * i128::from(count - 1);
        if ranges.iter().all(|&(lo, hi)| stride.abs() <= hi - lo) {
            let mut next = Vec::with_capacity(ranges.len());
            for (lo, hi) in ranges {
                push_range(&mut next, (lo + advance.min(0), hi + advance.max(0)));
            }
            ranges = next;
            continue;
        }
        // Normalize signed walks to positive translations of their low end.
        if advance < 0 {
            for (lo, hi) in &mut ranges {
                *lo += advance;
                *hi += advance;
            }
        }
        let stride = stride.abs();
        let mut copies = 1;
        while copies <= count / 2 {
            ranges = union_shift(&ranges, stride * i128::from(copies));
            copies *= 2;
        }
        if copies != count {
            ranges = union_shift(&ranges, stride * i128::from(count - copies));
        }
    }
    ranges
        .into_iter()
        .map(|(lo, hi)| {
            Ok((
                u64::try_from(lo).map_err(|_| "physical address overflow")?,
                u64::try_from(hi).map_err(|_| "physical interval overflow")?,
            ))
        })
        .collect::<Result<_, &str>>()
        .map_err(str::to_string)
}

fn push_range(ranges: &mut Vec<(i128, i128)>, (lo, hi): (i128, i128)) {
    if let Some(last) = ranges.last_mut() {
        if lo <= last.1 {
            last.1 = last.1.max(hi);
            return;
        }
    }
    ranges.push((lo, hi));
}

/// Merge an exact union and its translation without expanding its elements.
fn union_shift(ranges: &[(i128, i128)], shift: i128) -> Vec<(i128, i128)> {
    let mut out = Vec::with_capacity(ranges.len());
    let (mut a, mut b) = (0, 0);
    while a < ranges.len() || b < ranges.len() {
        let range = if a < ranges.len() && (b == ranges.len() || ranges[a].0 <= ranges[b].0 + shift)
        {
            let range = ranges[a];
            a += 1;
            range
        } else {
            let (lo, hi) = ranges[b];
            b += 1;
            (lo + shift, hi + shift)
        };
        push_range(&mut out, range);
    }
    out
}
