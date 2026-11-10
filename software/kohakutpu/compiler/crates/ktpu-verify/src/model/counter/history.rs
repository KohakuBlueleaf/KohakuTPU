//! Exact owner publications, with immutable chunks shared across part proofs.
//!
//! Freezing moves (not copies) the current part's marks into a leaf. Equal-rank
//! adjacent trees merge like a binary carry, so there are O(log parts) roots.
//! No publication is discarded and no clock is joined into an executing CPU.

use std::sync::Arc;

use super::Clock;

type Mark = (i64, Clock);

#[derive(Debug)]
pub(crate) struct Tree {
    rank: usize,
    len: usize,
    last: Mark,
    body: Body,
}

#[derive(Debug)]
enum Body {
    Leaf(Vec<Mark>),
    Branch(Arc<Tree>, Arc<Tree>),
}

#[derive(Debug)]
pub(crate) enum History {
    Flat(Vec<Mark>),
    Shared {
        roots: Vec<Arc<Tree>>,
        live: Vec<Mark>,
    },
}

impl Default for History {
    fn default() -> Self {
        Self::Flat(Vec::new())
    }
}

impl Clone for History {
    fn clone(&self) -> Self {
        match self {
            Self::Flat(marks) => {
                if crate::composition::active() {
                    crate::work::visit_history(marks.len());
                }
                Self::Flat(marks.clone())
            }
            Self::Shared { roots, live } => {
                // Meter the actual copied handles/marks, not the logical length
                // of an immutable prefix. No Arc::make_mut of a growing vector.
                crate::work::visit_history(roots.len() + live.len());
                Self::Shared {
                    roots: roots.clone(),
                    live: live.clone(),
                }
            }
        }
    }
}

impl PartialEq for History {
    fn eq(&self, other: &Self) -> bool {
        // Preserve the ordinary single-package equality path without a tree
        // iterator or any history instrumentation.
        if let (Self::Flat(a), Self::Flat(b)) = (self, other) {
            return a == b;
        }
        if let (Self::Shared { roots: a, live: x }, Self::Shared { roots: b, live: y }) =
            (self, other)
        {
            if a.len() == b.len() && a.iter().zip(b).all(|(a, b)| Arc::ptr_eq(a, b)) {
                return x == y;
            }
        }
        self.len() == other.len() && self.into_iter().eq(other)
    }
}
impl Eq for History {}

impl History {
    // Tree summaries own Clock handles too, although they are not additional
    // logical publications. Include them in the opt-in physical state census.
    pub fn summary_clocks(&self, mut visit: impl FnMut(&Clock)) {
        if let Self::Shared { roots, .. } = self {
            let mut pending: Vec<_> = roots.iter().map(Arc::as_ref).collect();
            while let Some(tree) = pending.pop() {
                visit(&tree.last.1);
                if let Body::Branch(left, right) = &tree.body {
                    pending.push(left);
                    pending.push(right);
                }
            }
        }
    }

    pub fn len(&self) -> usize {
        match self {
            Self::Flat(marks) => marks.len(),
            Self::Shared { roots, live } => roots.iter().map(|r| r.len).sum::<usize>() + live.len(),
        }
    }

    pub fn last(&self) -> Option<&Mark> {
        match self {
            Self::Flat(marks) => marks.last(),
            Self::Shared { roots, live } => live.last().or_else(|| roots.last().map(|r| &r.last)),
        }
    }

    pub fn push(&mut self, mark: Mark) {
        match self {
            Self::Flat(marks) => marks.push(mark),
            Self::Shared { live, .. } => live.push(mark),
        }
    }

    pub fn freeze(&mut self) {
        if let Self::Flat(marks) = self {
            let live = std::mem::take(marks);
            *self = Self::Shared {
                roots: Vec::new(),
                live,
            };
        }
        let Self::Shared { roots, live } = self else {
            unreachable!()
        };
        let Some(last) = live.last().cloned() else {
            return;
        };
        crate::work::visit_history(live.len());
        let mut tree = Arc::new(Tree {
            rank: 0,
            len: live.len(),
            last,
            body: Body::Leaf(std::mem::take(live)),
        });
        while roots.last().is_some_and(|left| left.rank == tree.rank) {
            let left = roots.pop().expect("equal-rank left subtree");
            crate::work::visit_history(1);
            tree = Arc::new(Tree {
                rank: tree.rank + 1,
                len: left.len + tree.len,
                last: tree.last.clone(),
                body: Body::Branch(left, tree),
            });
        }
        roots.push(tree);
    }

    /// First mark reaching k, or the last available mark, exactly as the old
    /// slice partition_point. Per-owner counts are strictly increasing.
    pub fn reaching(&self, k: i64) -> Option<&Mark> {
        match self {
            Self::Flat(marks) => slice_reaching(marks, k, false),
            Self::Shared { roots, live } => {
                let i = roots.partition_point(|r| {
                    crate::work::visit_history(1);
                    r.last.0 < k
                });
                if let Some(root) = roots.get(i) {
                    let mut tree = root.as_ref();
                    loop {
                        crate::work::visit_history(1);
                        match &tree.body {
                            Body::Leaf(marks) => return slice_reaching(marks, k, true),
                            Body::Branch(left, right) => {
                                tree = if k <= left.last.0 { left } else { right };
                            }
                        }
                    }
                }
                slice_reaching(live, k, true).or_else(|| roots.last().map(|r| &r.last))
            }
        }
    }
}

fn slice_reaching(marks: &[Mark], k: i64, meter: bool) -> Option<&Mark> {
    if marks.is_empty() {
        return None;
    }
    let i = marks.partition_point(|m| {
        if meter {
            crate::work::visit_history(1);
        }
        m.0 < k
    });
    Some(&marks[i.min(marks.len() - 1)])
}

pub(crate) struct Marks<'a> {
    pending: Vec<&'a Tree>,
    current: std::slice::Iter<'a, Mark>,
    live: std::slice::Iter<'a, Mark>,
}

impl<'a> IntoIterator for &'a History {
    type Item = &'a Mark;
    type IntoIter = Marks<'a>;
    fn into_iter(self) -> Marks<'a> {
        match self {
            History::Flat(marks) => Marks {
                pending: Vec::new(),
                current: marks.iter(),
                live: [].iter(),
            },
            History::Shared { roots, live } => Marks {
                pending: roots.iter().rev().map(Arc::as_ref).collect(),
                current: [].iter(),
                live: live.iter(),
            },
        }
    }
}

impl<'a> Iterator for Marks<'a> {
    type Item = &'a Mark;
    fn next(&mut self) -> Option<Self::Item> {
        loop {
            if let Some(mark) = self.current.next() {
                return Some(mark);
            }
            let Some(tree) = self.pending.pop() else {
                return self.live.next();
            };
            match &tree.body {
                Body::Leaf(marks) => self.current = marks.iter(),
                Body::Branch(left, right) => {
                    self.pending.push(right);
                    self.pending.push(left);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn freezes_preserve_every_prefix_and_branch_isolation() {
        let mut shared = History::default();
        let mut flat = History::default();
        for i in 1..=257 {
            let mut clock = Clock::new();
            clock.tick(0, i);
            let mark = (i64::from(i) * 3, clock);
            shared.push(mark.clone());
            flat.push(mark);
            if i % 7 == 0 {
                shared.freeze();
            }
        }
        shared.freeze();
        assert_eq!(flat, shared);
        for k in 0..=780 {
            assert_eq!(flat.reaching(k), shared.reaching(k));
        }
        let mut fork = shared.clone();
        fork.push((900, Clock::new()));
        fork.freeze();
        assert_eq!(shared, flat);
        assert_eq!(fork.last().unwrap().0, 900);
        assert_eq!(shared.last().unwrap().0, 771);
        assert_eq!(shared.into_iter().count(), 257);
    }

    #[test]
    fn clone_cost_is_handles_not_cumulative_publications() {
        let mut history = History::default();
        for part in 0..32 {
            for i in 1..=1000 {
                history.push((part * 1000 + i, Clock::new()));
            }
            history.freeze();
            let before = crate::work::history_visits();
            let fork = history.clone();
            let copied = crate::work::history_visits() - before;
            assert!(copied <= 6, "copied {copied} roots at part {part}");
            assert_eq!(fork, history);
        }
    }
}
