//! A plan's index lists (fp32, each from a word) and mask rows (`_Tables`).

use super::Hw;
use std::collections::HashMap;

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Key {
    Rot(i64),
    Lanes(i64, i64),
}

pub(super) struct Tables {
    pub hw: Hw,
    pub index: Vec<f32>,
    pub masks: Vec<Vec<f32>>,
    keys: HashMap<Key, i64>,
}

impl Tables {
    pub fn new(hw: Hw) -> Tables {
        Tables {
            hw,
            index: vec![],
            masks: vec![],
            keys: HashMap::new(),
        }
    }

    /// The list `ix` from a word: padded to whole words with its last entry.
    pub fn put(&mut self, ix: &[i64]) -> i64 {
        let at = self.index.len() as i64;
        self.index.extend(ix.iter().map(|&x| x as f32));
        while !self.index.len().is_multiple_of(8) {
            self.index.push(ix[ix.len() - 1] as f32);
        }
        at
    }

    fn mask(&mut self, key: Key, row: Vec<f32>) -> i64 {
        if let Some(&got) = self.keys.get(&key) {
            return got;
        }
        let got = self.masks.len() as i64;
        self.keys.insert(key, got);
        self.masks.push(row);
        got
    }

    /// Rows appended as they are; the first one's row.
    pub fn rows(&mut self, rows: Vec<Vec<f32>>) -> i64 {
        let at = self.masks.len() as i64;
        self.masks.extend(rows);
        at
    }

    /// Lanes l < lanes - rot of every word (from the row's word).
    pub fn rot_mask(&mut self, rot: i64) -> i64 {
        let (row, lanes) = (self.hw.row, self.hw.lanes);
        let bits = (0..row)
            .map(|v| f32::from(u8::from(v % lanes < lanes - rot)))
            .collect();
        self.mask(Key::Rot(rot), bits)
    }

    /// Values `lo`.. (`n` of them) of a row.
    pub fn lane_mask(&mut self, lo: i64, n: i64) -> i64 {
        let bits = (0..self.hw.row)
            .map(|v| f32::from(u8::from(v >= lo && v < lo + n)))
            .collect();
        self.mask(Key::Lanes(lo, n), bits)
    }
}
