//! Bank-aware placement of a vector core's values (L2.SEARCH.006).
//!
//! A V2 register file is `vc.rf_banks` banks (a word's chunk mod that), each `rf_copies`
//! read copies. With two copies a math beat reading three distinct words of
//! one bank holds the math engine a cycle, and a beat reading two leaves the
//! pack engine no copy; with three only the second, at three words. A value
//! stored rotated by one chunk reads the other bank on every beat: `rotate`
//! picks a rotation (0 or 1) per value and rewrites every reference.

use std::collections::{HashMap, HashSet};

use super::modulo::Core;

pub mod typed;

const HOLD: i64 = 2;
const STARVE: i64 = 1;
const PASSES: usize = 16;
/// Past this many `vsel`s a value is assembled lane by lane: it stays unrotated.
const ASSEMBLED_VSELS: usize = 16;

fn cost(
    reads: &[(&str, u64, u64)],
    beats: u64,
    rho: &HashMap<&str, u64>,
    copies: u32,
    core: &Core,
) -> i64 {
    let (chunks, banks) = (core.chunks as u64, core.rf_banks);
    let mut cost = 0;
    for i in 0..beats {
        let mut words: HashSet<(&str, u64)> = HashSet::new();
        for &(s, b, st) in reads {
            words.insert((s, (b + rho.get(s).copied().unwrap_or(0) + st * i) % chunks));
        }
        for bank in 0..banks {
            let n = words.iter().filter(|(_, c)| c % banks == bank).count();
            if copies == 2 {
                cost += HOLD * (n >= 3) as i64 + STARVE * (n >= 2) as i64;
            } else if copies == 3 {
                cost += STARVE * (n >= 3) as i64;
            }
        }
    }
    cost
}
