//! Dependency queries over interned resources and live interval trees.
//!
//! Bounds select possible conflicts; the original footprint decides each leaf.
//! Removing a completed or issued effect updates only its path to the root.

use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

use super::{clash, complete, on, overlap, Access, Stage, State, FENCE};

type Key = (usize, usize);

#[derive(Default)]
pub(crate) struct Names<'a> {
    ids: HashMap<&'a str, usize>,
}

impl<'a> Names<'a> {
    fn intern(&mut self, name: &'a str) -> usize {
        let next = self.ids.len();
        *self.ids.entry(name).or_insert(next)
    }
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct Mode {
    unit: usize,
    write: bool,
    flow: bool,
    own: bool,
}

impl Mode {
    fn meets(self, other: Self, exact: bool) -> bool {
        if !(self.write || other.write) {
            return false;
        }
        !exact
            || (!on("l2.sync/never-clash")
                && (on("l2.sync/flow-clashes") || !self.flow || !other.flow)
                && !(self.unit == other.unit
                    && self.own
                    && other.own
                    && !on("l2.sync/own-clashes")))
    }
}

#[derive(Clone, Copy)]
struct Effect<'a> {
    access: &'a Access,
    key: Key,
    mode: Mode,
    stage: usize,
    ordinal: usize,
    id: usize,
    lo: i64,
    hi: i64,
}

impl<'a> Effect<'a> {
    fn new(
        names: &mut Names<'a>,
        access: &'a Access,
        stage: usize,
        ordinal: usize,
        id: usize,
    ) -> Self {
        let key = (names.intern(&access.key.0), names.intern(&access.key.1));
        let unit = names.intern(&access.unit);
        let (lo, hi) = access.range();
        Self {
            access,
            key,
            mode: Mode {
                unit,
                write: access.write,
                flow: access.flow,
                own: key.0 == unit,
            },
            stage,
            ordinal,
            id,
            lo,
            hi,
        }
    }
}

struct Entry<'a> {
    effect: Effect<'a>,
    live: bool,
    min_lo: i64,
    max_hi: i64,
    min_stage: usize,
}

struct Tree<'a> {
    mode: Mode,
    entries: Vec<Entry<'a>>,
}

impl Tree<'_> {
    fn refresh(entries: &mut [Entry<'_>]) {
        let mid = entries.len() / 2;
        let (left, rest) = entries.split_at_mut(mid);
        let (root, right) = rest.split_at_mut(1);
        let root = &mut root[0];
        (root.min_lo, root.max_hi, root.min_stage) = if root.live {
            (root.effect.lo, root.effect.hi, root.effect.stage)
        } else {
            (i64::MAX, i64::MIN, usize::MAX)
        };
        for child in [left, right] {
            if !child.is_empty() {
                let child = &child[child.len() / 2];
                root.min_lo = root.min_lo.min(child.min_lo);
                root.max_hi = root.max_hi.max(child.max_hi);
                root.min_stage = root.min_stage.min(child.min_stage);
            }
        }
    }

    fn finish(entries: &mut [Entry<'_>]) {
        if entries.is_empty() {
            return;
        }
        let mid = entries.len() / 2;
        Self::finish(&mut entries[..mid]);
        Self::finish(&mut entries[mid + 1..]);
        Self::refresh(entries);
    }

    fn set(entries: &mut [Entry<'_>], at: usize, live: bool) {
        let mid = entries.len() / 2;
        match at.cmp(&mid) {
            std::cmp::Ordering::Less => Self::set(&mut entries[..mid], at, live),
            std::cmp::Ordering::Equal => entries[mid].live = live,
            std::cmp::Ordering::Greater => {
                Self::set(&mut entries[mid + 1..], at - mid - 1, live);
            }
        }
        Self::refresh(entries);
    }

    fn query<'a>(
        entries: &[Entry<'a>],
        next: &Effect<'_>,
        before: usize,
        exact: bool,
        visits: &Cell<usize>,
        found: &mut impl FnMut(Effect<'a>) -> bool,
    ) -> bool {
        if entries.is_empty() {
            return false;
        }
        visits.set(visits.get() + 1);
        let mid = entries.len() / 2;
        let root = &entries[mid];
        let whole = exact && on("l2.sync/whole-buffer");
        if root.min_stage >= before
            || (!whole && (root.max_hi <= next.lo || root.min_lo >= next.hi))
        {
            return false;
        }
        let a = root.effect;
        if root.live
            && a.stage < before
            && (whole || (a.lo < next.hi && next.lo < a.hi))
            && (!exact || overlap(a.access, next.access))
            && found(a)
        {
            return true;
        }
        Self::query(&entries[..mid], next, before, exact, visits, found)
            || Self::query(&entries[mid + 1..], next, before, exact, visits, found)
    }
}

struct Forest<'a> {
    keys: BTreeMap<Key, Vec<usize>>,
    trees: Vec<Tree<'a>>,
    locations: Vec<(usize, usize)>,
    visits: Cell<usize>,
}

impl<'a> Forest<'a> {
    fn new(effects: impl Iterator<Item = Effect<'a>>, count: usize, live: bool) -> Self {
        let mut grouped: BTreeMap<(Key, Mode), Vec<Entry<'a>>> = BTreeMap::new();
        for effect in effects {
            grouped
                .entry((effect.key, effect.mode))
                .or_default()
                .push(Entry {
                    effect,
                    live,
                    min_lo: i64::MAX,
                    max_hi: i64::MIN,
                    min_stage: usize::MAX,
                });
        }
        let mut keys: BTreeMap<Key, Vec<usize>> = BTreeMap::new();
        let mut trees = Vec::new();
        let mut locations = vec![(0, 0); count];
        for ((key, mode), mut entries) in grouped {
            let tree = trees.len();
            entries.sort_by_key(|e| (e.effect.lo, e.effect.id));
            for (at, entry) in entries.iter().enumerate() {
                locations[entry.effect.id] = (tree, at);
            }
            Tree::finish(&mut entries);
            keys.entry(key).or_default().push(tree);
            trees.push(Tree { mode, entries });
        }
        Self {
            keys,
            trees,
            locations,
            visits: Cell::new(0),
        }
    }

    fn set(&mut self, id: usize, live: bool) {
        let (tree, at) = self.locations[id];
        Tree::set(&mut self.trees[tree].entries, at, live);
    }

    fn query(
        &self,
        next: &Effect<'_>,
        before: usize,
        exact: bool,
        mut found: impl FnMut(Effect<'a>) -> bool,
    ) -> bool {
        self.visits.set(self.visits.get() + 1);
        let Some(trees) = self.keys.get(&next.key) else {
            return false;
        };
        trees.iter().any(|&at| {
            let tree = &self.trees[at];
            tree.mode.meets(next.mode, exact)
                && Tree::query(&tree.entries, next, before, exact, &self.visits, &mut found)
        })
    }
}

/// Immutable effects for cross-node dependency queries, sharing one name table.
pub(crate) struct AccessIndex<'a> {
    effects: Vec<Effect<'a>>,
    forest: Forest<'a>,
}

impl<'a> AccessIndex<'a> {
    pub(crate) fn new(accesses: &'a [Access], names: &mut Names<'a>) -> Self {
        let effects: Vec<_> = accesses
            .iter()
            .enumerate()
            .map(|(i, a)| Effect::new(names, a, 0, i, i))
            .collect();
        let forest = Forest::new(effects.iter().copied(), effects.len(), true);
        Self { effects, forest }
    }

    pub(crate) fn conflicts(&self, next: &Self) -> bool {
        let (haystack, needles) = if self.effects.len() >= next.effects.len() {
            (&self.forest, &next.effects)
        } else {
            (&next.forest, &self.effects)
        };
        needles
            .iter()
            .any(|effect| haystack.query(effect, usize::MAX, true, |_| true))
    }
}

struct Owner<'a> {
    name: &'a str,
    pending: VecDeque<(usize, usize)>,
    done: i64,
    waitable: bool,
}

/// Live emitted effects and not-yet-issued effects of one schedule.
pub(super) struct Dependencies<'a> {
    stages: &'a [Stage],
    effects: Vec<Vec<Effect<'a>>>,
    pending: Forest<'a>,
    unissued: Forest<'a>,
    owners: BTreeMap<usize, Owner<'a>>,
    units: Vec<Vec<usize>>,
    unit_stages: BTreeMap<usize, BTreeSet<usize>>,
    fences: BTreeSet<usize>,
    exhaustive: bool,
    reference_visits: Cell<usize>,
}

impl<'a> Dependencies<'a> {
    pub(super) fn new(stages: &'a [Stage], exhaustive: bool) -> Self {
        let mut names = Names::default();
        let mut effects = Vec::new();
        let mut owners = BTreeMap::new();
        let mut units = Vec::new();
        let mut unit_stages: BTreeMap<usize, BTreeSet<usize>> = BTreeMap::new();
        let mut fences = BTreeSet::new();
        let mut count = 0;
        for (s, stage) in stages.iter().enumerate() {
            let mut row = Vec::new();
            for (ordinal, access) in stage.accesses.iter().enumerate() {
                let effect = Effect::new(&mut names, access, s, ordinal, count);
                owners.entry(effect.mode.unit).or_insert_with(|| Owner {
                    name: access.unit.as_str(),
                    pending: VecDeque::new(),
                    done: -1,
                    waitable: access.unit != FENCE,
                });
                row.push(effect);
                count += 1;
            }
            effects.push(row);
            let row: Vec<_> = stage.units.iter().map(|u| names.intern(u)).collect();
            for &unit in &row {
                unit_stages.entry(unit).or_default().insert(s);
            }
            units.push(row);
            if stage.fence {
                fences.insert(s);
            }
        }
        let pending = Forest::new(effects.iter().flatten().copied(), count, false);
        let unissued = Forest::new(effects.iter().flatten().copied(), count, true);
        Self {
            stages,
            effects,
            pending,
            unissued,
            owners,
            units,
            unit_stages,
            fences,
            exhaustive,
            reference_visits: Cell::new(0),
        }
    }

    pub(super) fn visits(&self) -> usize {
        self.pending.visits.get() + self.unissued.visits.get() + self.reference_visits.get()
    }

    pub(super) fn needs(&self, st: &State, s: usize) -> Vec<(usize, &'a Access)> {
        if self.exhaustive {
            let mut found = Vec::new();
            for &p in &st.order {
                for a in &self.stages[p].accesses {
                    self.reference_visits.set(self.reference_visits.get() + 1);
                    if a.unit != FENCE
                        && !complete(st, p, &a.unit)
                        && self.stages[s].accesses.iter().any(|b| {
                            self.reference_visits.set(self.reference_visits.get() + 1);
                            clash(a, b)
                        })
                    {
                        found.push((p, a));
                    }
                }
            }
            return found;
        }
        // Waits use the earliest conflicting ordinal for each stage and owner.
        let mut first: HashMap<(usize, usize), Effect<'a>> = HashMap::new();
        for next in &self.effects[s] {
            self.pending.query(next, usize::MAX, true, |effect| {
                first
                    .entry((effect.stage, effect.mode.unit))
                    .and_modify(|old| {
                        if effect.ordinal < old.ordinal {
                            *old = effect;
                        }
                    })
                    .or_insert(effect);
                false
            });
        }
        let mut found: Vec<_> = first.into_values().collect();
        found.sort_by_key(|e| (st.pos[&e.stage], e.ordinal));
        found.into_iter().map(|e| (e.stage, e.access)).collect()
    }

    pub(super) fn hoistable(&self, st: &State, j: usize) -> bool {
        if !self.needs(st, j).is_empty() {
            return false;
        }
        if self.exhaustive {
            for k in 0..j {
                if st.pos.contains_key(&k) {
                    continue;
                }
                if self.stages[k].fence
                    || self.stages[k]
                        .units
                        .iter()
                        .any(|u| self.stages[j].units.contains(u))
                {
                    return false;
                }
                if self.stages[k].accesses.iter().any(|a| {
                    self.stages[j].accesses.iter().any(|b| {
                        self.reference_visits.set(self.reference_visits.get() + 1);
                        let ((alo, ahi), (blo, bhi)) = (a.range(), b.range());
                        a.key == b.key && (a.write || b.write) && alo < bhi && blo < ahi
                    })
                }) {
                    return false;
                }
            }
            return true;
        }
        if self.fences.first().is_some_and(|&s| s < j)
            || self.units[j]
                .iter()
                .any(|unit| self.unit_stages[unit].first().is_some_and(|&s| s < j))
        {
            return false;
        }
        !self.effects[j]
            .iter()
            .any(|next| self.unissued.query(next, j, false, |_| true))
    }

    pub(super) fn emitted(&mut self, st: &State, s: usize) {
        if self.exhaustive {
            return;
        }
        for owner in self.owners.values_mut() {
            owner.done = st.done.get(owner.name).copied().unwrap_or(-1);
            while owner
                .pending
                .front()
                .is_some_and(|&(at, _)| (at as i64) <= owner.done)
            {
                if let Some((_, id)) = owner.pending.pop_front() {
                    self.pending.set(id, false);
                }
            }
        }
        let at = st.pos[&s];
        for effect in &self.effects[s] {
            self.unissued.set(effect.id, false);
            let owner = self
                .owners
                .get_mut(&effect.mode.unit)
                .expect("interned effect owner");
            if owner.waitable && owner.done < at as i64 {
                self.pending.set(effect.id, true);
                owner.pending.push_back((at, effect.id));
            }
        }
        for unit in &self.units[s] {
            self.unit_stages
                .get_mut(unit)
                .expect("interned stage unit")
                .remove(&s);
        }
        self.fences.remove(&s);
    }
}
