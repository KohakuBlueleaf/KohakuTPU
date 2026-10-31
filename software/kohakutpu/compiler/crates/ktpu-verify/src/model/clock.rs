//! Vector clocks (verify.md s7.1): process index -> events of it seen. One
//! process's own count is held apart (`own`, `n`) and the rest in a table
//! copies share until a join raises one of them, so a copy is O(1).

use std::rc::Rc;

#[derive(Clone, Debug, Default)]
pub struct Clock {
    base: Rc<Vec<u32>>,
    own: Option<usize>,
    n: u32,
}

impl Clock {
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
        let same = Rc::ptr_eq(&self.base, &other.base);
        if same && self.own == other.own && self.n >= other.n {
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

    fn raise(&mut self, k: usize, n: u32) {
        if Some(k) == self.own {
            self.n = n;
        } else {
            let base = Rc::make_mut(&mut self.base);
            if base.len() <= k {
                base.resize(k + 1, 0);
            }
            base[k] = n;
        }
    }

    /// Process `owner`'s count set to `n` (its own event).
    pub fn tick(&mut self, owner: usize, n: u32) {
        if self.own != Some(owner) {
            if let Some(o) = self.own {
                let base = Rc::make_mut(&mut self.base);
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
