//! The original non-closed difference-bound matrix, local to one region.
//! Missing edges, i128 arithmetic and asymmetric widening retain base semantics.

use std::collections::{HashMap, HashSet};

use crate::frontier_profile::{entries, snapshot};

pub(super) struct Frontiers {
    upper: HashMap<(usize, usize), i128>,
    defined: HashSet<usize>,
}

impl Clone for Frontiers {
    fn clone(&self) -> Self {
        snapshot();
        entries(self.upper.len() + self.defined.len());
        Self {
            upper: self.upper.clone(),
            defined: self.defined.clone(),
        }
    }
}

impl Frontiers {
    pub(super) fn new(units: usize) -> Self {
        let mut upper = HashMap::new();
        let mut defined = HashSet::new();
        for u in 0..units {
            upper.insert((2 * u, 2 * u + 1), 0);
            upper.insert((2 * u + 1, 2 * u), 0);
            defined.extend([2 * u, 2 * u + 1]);
        }
        Self { upper, defined }
    }

    pub(super) fn bound(&self, x: usize, y: usize) -> Option<i128> {
        if x == y {
            Some(0)
        } else {
            self.upper.get(&(x, y)).copied()
        }
    }

    pub(super) fn difference(&self, x: usize, y: usize) -> Option<i128> {
        let hi = self.bound(x, y)?;
        (self.bound(y, x)? == -hi).then_some(hi)
    }

    pub(super) fn covered(&self, x: usize, y: usize) -> bool {
        self.bound(x, y).is_some_and(|d| d <= 0)
    }

    pub(super) fn is_defined(&self, v: usize) -> bool {
        self.defined.contains(&v)
    }

    pub(super) fn defined_vars(&self) -> impl Iterator<Item = usize> + '_ {
        self.defined.iter().copied()
    }

    pub(super) fn projected_upper<'a>(
        &'a self,
        keep: impl Fn(usize) -> bool + 'a,
    ) -> impl Iterator<Item = ((usize, usize), i128)> + 'a {
        entries(self.upper.len());
        self.upper
            .iter()
            .filter(move |&(&(x, y), _)| keep(x) && keep(y))
            .map(|(&p, &v)| (p, v))
    }

    pub(super) fn assign(&mut self, to: usize, from: usize) {
        if to == from {
            return;
        }
        entries(self.upper.len());
        self.upper.retain(|&(x, y), _| x != to && y != to);
        let mut copied = Vec::new();
        entries(self.upper.len());
        for (&(x, y), &bound) in &self.upper {
            if x == from {
                copied.push(((to, y), bound));
            }
            if y == from {
                copied.push(((x, to), bound));
            }
        }
        self.upper.extend(copied);
        self.upper.insert((to, from), 0);
        self.upper.insert((from, to), 0);
        if self.defined.contains(&from) {
            self.defined.insert(to);
        } else {
            self.defined.remove(&to);
        }
    }

    pub(super) fn add(&mut self, to: usize, n: i128) {
        entries(self.upper.len());
        for (&(x, y), bound) in &mut self.upper {
            if x == to {
                *bound += n;
            }
            if y == to {
                *bound -= n;
            }
        }
    }

    pub(super) fn join(&mut self, other: &Self) -> bool {
        let mut changed = false;
        entries(self.upper.len() + self.defined.len());
        self.upper.retain(|pair, bound| {
            let Some(&incoming) = other.upper.get(pair) else {
                changed = true;
                return false;
            };
            if incoming > *bound {
                changed = true;
                if incoming <= 0 {
                    *bound = 0;
                } else {
                    return false;
                }
            }
            true
        });
        self.defined.retain(|v| {
            let keep = other.defined.contains(v);
            changed |= !keep;
            keep
        });
        changed
    }

    pub(super) fn insert(&mut self, x: usize, y: usize, bound: i128) {
        self.upper.insert((x, y), bound);
    }
}
