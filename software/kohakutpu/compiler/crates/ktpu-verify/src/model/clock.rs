//! Vector clocks (verify.md s7.1): process index -> events of it seen. One
//! process's own count is held apart (`own`, `n`) and the rest in a table
//! copies share until a join raises one of them, so a copy is O(1).

use std::sync::Arc;

#[derive(Clone, Debug, Default)]
pub struct Clock {
    base: Arc<Vec<u32>>,
    own: Option<usize>,
    n: u32,
}

impl Clock {
    pub(crate) fn storage(&self) -> (usize, usize) {
        (Arc::as_ptr(&self.base) as usize, self.base.capacity())
    }

    pub fn new() -> Self {
        Clock::default()
    }

    /// Events of process `k` seen.
    pub fn at(&self, k: usize) -> u32 {
        if self.own == Some(k) {
            self.n
        } else {
            self.base.get(k).copied().unwrap_or(0)
        }
    }

    pub fn join(&mut self, other: &Clock) {
        let same = Arc::ptr_eq(&self.base, &other.base);
        if same {
            if let Some(owner) = self.own {
                let n = other.at(owner);
                self.n = self.n.max(n);
            }
            if let Some(owner) = other.own {
                if self.at(owner) < other.n {
                    self.raise(owner, other.n);
                }
            }
            return;
        }
        for (k, &n) in other.base.iter().enumerate() {
            if Some(k) != other.own && n > 0 && self.at(k) < n {
                self.raise(k, n);
            }
        }
        if let Some(o) = other.own {
            if self.at(o) < other.n {
                self.raise(o, other.n);
            }
        }
    }

    /// Componentwise lower bound for a set of access start clocks. This is
    /// a query summary, never a clock joined into an execution process.
    pub fn meet(&self, other: &Self) -> Self {
        if Arc::ptr_eq(&self.base, &other.base) && self.own == other.own {
            return Self {
                base: self.base.clone(),
                own: self.own,
                n: self.n.min(other.n),
            };
        }
        let len = self
            .base
            .len()
            .max(other.base.len())
            .max(self.own.map_or(0, |k| k + 1))
            .max(other.own.map_or(0, |k| k + 1));
        Self {
            base: Arc::new((0..len).map(|k| self.at(k).min(other.at(k))).collect()),
            own: None,
            n: 0,
        }
    }

    fn raise(&mut self, k: usize, n: u32) {
        if Some(k) == self.own {
            self.n = n;
        } else {
            let base = Arc::make_mut(&mut self.base);
            if base.len() <= k {
                base.resize(k + 1, 0);
            }
            base[k] = n;
        }
    }

    /// Every event in this proof summary is included in `other`.
    pub(crate) fn covered_by(&self, other: &Clock) -> bool {
        (0..self.base.len())
            .chain(self.own)
            .all(|k| self.at(k) <= other.at(k))
    }

    /// Process `owner`'s count set to `n` (its own event).
    pub fn tick(&mut self, owner: usize, n: u32) {
        if self.own != Some(owner) {
            if let Some(o) = self.own {
                let base = Arc::make_mut(&mut self.base);
                if base.len() <= o {
                    base.resize(o + 1, 0);
                }
                base[o] = self.n;
            }
            self.own = Some(owner);
        }
        self.n = n;
    }

    /// Whether the event this clock stamps (on process `owner`) happens
    /// before the event `other` stamps.
    pub fn before(&self, other: &Clock, owner: usize) -> bool {
        self.at(owner) <= other.at(owner)
    }
}

impl PartialEq for Clock {
    fn eq(&self, other: &Self) -> bool {
        let len = self.base.len().max(other.base.len());
        (0..len)
            .chain(self.own)
            .chain(other.own)
            .all(|k| self.at(k) == other.at(k))
    }
}

impl Eq for Clock {}
