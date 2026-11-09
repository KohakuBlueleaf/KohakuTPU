//! Bounded computed results. Eviction never removes immutable BDD nodes.

use std::collections::VecDeque;

use super::{Bit, HashMap};

type Key = (Bit, Bit, Bit);
const LIMIT: usize = 1 << 16;

#[derive(Debug, Default)]
pub(super) struct Computed {
    values: HashMap<Key, Bit>,
    order: VecDeque<Key>,
}

impl Computed {
    pub(super) fn len(&self) -> usize {
        self.values.len()
    }

    pub(super) fn get(&self, key: &Key) -> Option<&Bit> {
        self.values.get(key)
    }

    pub(super) fn insert(&mut self, key: Key, value: Bit) {
        if let Some(previous) = self.values.get_mut(&key) {
            *previous = value;
            return;
        }
        // One FIFO eviction, not a whole-table flush in the middle of an ITE.
        if self.values.len() == LIMIT {
            let oldest = self.order.pop_front().expect("full computed cache");
            self.values.remove(&oldest);
        }
        self.values.insert(key, value);
        self.order.push_back(key);
    }
}

