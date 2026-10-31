//! Scheduler operations and dependences.

use std::collections::{BTreeSet, HashMap, HashSet};

use super::*;

pub(super) mod typed;

#[derive(Clone, Debug)]
#[cfg_attr(test, derive(PartialEq, Eq))]
pub struct MOp {
    pub op: String,
    pub syms: Vec<String>,
    /// 'U' unpack, 'M' math, 'P' pack
    pub eng: char,
    pub reads: Vec<String>,
    pub writes: Vec<String>,
    pub occ: i64,
    /// the L1 word an unpack reads / a pack writes
    pub l1: Option<i64>,
    pub t: i64,
    pub e: i64,
    pub deps: Vec<(usize, i64, bool)>,
    pub xreads: Vec<String>,
}

/// RAW with latency after the producer's last beat; WAR / WAW in order.
pub fn link(ops: &mut [MOp], lat: &Params) {
    let mut last_w: HashMap<String, Vec<usize>> = HashMap::new();
    let mut readers: HashMap<String, Vec<usize>> = HashMap::new();
    for i in 0..ops.len() {
        let mut deps = Vec::new();
        for x in &ops[i].reads {
            for &p in last_w.get(x).map(|v| v.as_slice()).unwrap_or(&[]) {
                let po = &ops[p];
                let mut l = if po.eng == 'U' {
                    lat.unpack_math
                } else if ops[i].eng == 'P' {
                    lat.math_pack
                } else {
                    lat.math_math
                };
                if po.eng == 'M' && ops[i].xreads.contains(x) {
                    l += lat.xbar;
                }
                deps.push((p, l, true));
            }
        }
        for x in &ops[i].writes {
            for &p in last_w.get(x).map(|v| v.as_slice()).unwrap_or(&[]) {
                deps.push((p, 1, false));
            }
            for &p in readers.get(x).map(|v| v.as_slice()).unwrap_or(&[]) {
                deps.push((p, 1, false));
            }
        }
        ops[i].deps.extend(deps);
        for x in ops[i].reads.clone() {
            readers.entry(x).or_default().push(i);
        }
        for x in ops[i].writes.clone() {
            last_w.entry(x.clone()).or_default().push(i);
            readers.insert(x, Vec::new());
        }
    }
}
