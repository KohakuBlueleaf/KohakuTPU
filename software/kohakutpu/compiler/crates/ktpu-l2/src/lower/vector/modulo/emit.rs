//! A plan's code: prologue, VLOOP kernel and tail, each sync's slack, and the
//! register dataflow replay that checks it.

use std::collections::{HashMap, HashSet};

use super::*;

pub(in crate::lower::vector) mod typed;

/// A load or store's K L1 slots: `base` the trip-0 buffer the generated code
/// names, `slots` each copy's first word.
#[derive(Clone, Debug)]
pub struct Ring {
    pub ad: usize,
    /// "load" | "store"
    pub kind: &'static str,
    pub base: i64,
    pub words: i64,
    pub slots: Vec<i64>,
}

/// How the stream's last iterations finish.
pub const TAILS: [&str; 2] = ["explicit", "kernel"];

const FILL: i64 = -1;
const DRAIN: i64 = -2;

fn kind_ix(k: &str) -> usize {
    match k {
        "fill" => 0,
        "unpack" => 1,
        "pack" => 2,
        _ => 3,
    }
}

/// Replays the emission's register writes in program order: every read of
/// iteration j's value finds it in that copy's register.
#[allow(clippy::too_many_arguments)]
fn dataflow(
    p: &Plan,
    order: &[(i64, i64)],
    trips: i64,
    pinned: &HashMap<String, i64>,
    n_inst: i64,
    tail: &str,
    lo_loop: i64,
    hi_loop: i64,
) -> Result<(), String> {
    let mut seen: HashSet<&str> = HashSet::new();
    let mut merge: Vec<HashSet<&str>> = Vec::new();
    for o in &p.ops {
        merge.push(
            o.reads
                .iter()
                .filter(|x| o.writes.contains(x) && !seen.contains(x.as_str()))
                .map(|x| x.as_str())
                .collect(),
        );
        for w in &o.writes {
            seen.insert(w);
        }
    }
    let mut holds: HashMap<i64, (String, i64)> = HashMap::new();
    for n in 0..n_inst {
        let past = tail == "kernel" && lo_loop <= n && n < hi_loop;
        for &(t, i) in order {
            if i < 0 {
                continue;
            }
            let j = n - p.stage(t);
            if j < 0 || (j >= trips && !past) {
                continue;
            }
            let o = &p.ops[i as usize];
            if j < trips {
                for x in &o.reads {
                    if pinned.contains_key(x) || x == PRED || merge[i as usize].contains(x.as_str())
                    {
                        continue;
                    }
                    let r = p.phys[&(x.clone(), floor_mod(j, p.k))];
                    if holds.get(&r) != Some(&(x.clone(), j)) {
                        return Err(format!(
                            "modulo II {} K {}: iteration {j}'s {} reads {x} from v{r}, which holds {:?}",
                            p.ii,
                            p.k,
                            o.op,
                            holds.get(&r)
                        ));
                    }
                }
            }
            for x in &o.writes {
                if !pinned.contains_key(x) && x != PRED {
                    holds.insert(p.phys[&(x.clone(), floor_mod(j, p.k))], (x.clone(), j));
                }
            }
        }
    }
    Ok(())
}
