//! The plan: an II's placement (iterative modulo scheduling), each value's
//! lifetime, and its K register copies coloured on the circle of K x II.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, HashSet};

use super::*;

fn eng_index(e: char) -> usize {
    match e {
        'M' => 0,
        'U' => 1,
        _ => 2,
    }
}

/// The least t in lo..lo+ii-1 whose `occ` (at most ii) slots from t (mod ii)
/// are free: one scan from lo, a run of free slots counted as it goes.
fn first_fit(row: &[Option<usize>], lo: i64, occ: i64, ii: i64) -> Option<i64> {
    if occ <= 0 {
        return (occ == 0).then_some(lo);
    }
    let (iiu, occ) = (ii as usize, occ as usize);
    let s = floor_mod(lo, ii) as usize;
    let mut free = 0;
    for k in s..s + iiu + occ - 1 {
        if row[k % iiu].is_some() && !crate::mutant::on("l2.vector/place-overlaps") {
            free = 0;
            continue;
        }
        free += 1;
        if free == occ {
            return Some(lo + (k + 1 - occ - s) as i64);
        }
    }
    None
}

/// Iterative modulo scheduling (Rau): ops by height take the first run of
/// `occ` consecutive free slots from their earliest start; where none is free
/// an op takes its slot and evicts what holds it.
pub fn place(ops: &mut [MOp], ii: i64) -> bool {
    let n = ops.len();
    if ops.iter().any(|o| o.occ > ii) {
        return false;
    }
    let pred: Vec<Vec<(usize, i64)>> = ops
        .iter()
        .map(|o| {
            o.deps
                .iter()
                .map(|&(p, lat, end)| (p, lat + if end { ops[p].occ } else { 0 }))
                .collect()
        })
        .collect();
    let mut succ: Vec<Vec<(usize, i64)>> = vec![Vec::new(); n];
    for (i, ps) in pred.iter().enumerate() {
        for &(p, d) in ps {
            succ[p].push((i, d));
        }
    }
    let mut height = vec![0i64; n];
    for i in (0..n).rev() {
        height[i] = succ[i]
            .iter()
            .map(|&(s, d)| d + height[s])
            .max()
            .unwrap_or(ops[i].occ);
    }
    let iiu = ii as usize;
    let mut owner: [Vec<Option<usize>>; 3] = [vec![None; iiu], vec![None; iiu], vec![None; iiu]];
    let mut at: Vec<Option<i64>> = vec![None; n];
    let mut last: Vec<Option<i64>> = vec![None; n];
    let mut queue: BinaryHeap<Reverse<(i64, usize)>> =
        (0..n).map(|i| Reverse((-height[i], i))).collect();
    let mut budget = ((PLACE_BUDGET + 1) * n) as i64;

    fn evict(
        j: usize,
        ops: &[MOp],
        owner: &mut [Vec<Option<usize>>; 3],
        at: &mut [Option<i64>],
        queue: &mut BinaryHeap<Reverse<(i64, usize)>>,
        height: &[i64],
        ii: i64,
    ) {
        let e = eng_index(ops[j].eng);
        let t = at[j].unwrap();
        for b in 0..ops[j].occ {
            owner[e][floor_mod(t + b, ii) as usize] = None;
        }
        at[j] = None;
        queue.push(Reverse((-height[j], j)));
    }

    while let Some(Reverse((_, i))) = queue.pop() {
        if at[i].is_some() {
            continue;
        }
        budget -= 1;
        if budget < 0 {
            return false;
        }
        let e = eng_index(ops[i].eng);
        let early = pred[i]
            .iter()
            .filter_map(|&(p, d)| at[p].map(|a| a + d))
            .max()
            .unwrap_or(0);
        let lo = match last[i] {
            Some(l) if l >= early => l + 1,
            _ => early,
        };
        let occ = ops[i].occ;
        let t = match first_fit(&owner[e], lo, occ, ii) {
            Some(t) => t,
            None => {
                let mut victims: Vec<usize> = (0..occ)
                    .filter_map(|b| owner[e][floor_mod(lo + b, ii) as usize])
                    .collect();
                victims.sort();
                victims.dedup();
                for j in victims {
                    evict(j, ops, &mut owner, &mut at, &mut queue, &height, ii);
                }
                lo
            }
        };
        for b in 0..occ {
            owner[e][floor_mod(t + b, ii) as usize] = Some(i);
        }
        at[i] = Some(t);
        last[i] = Some(t);
        for &(s, d) in &succ[i] {
            if let Some(a) = at[s] {
                if a < t + d && !crate::mutant::on("l2.vector/place-keeps-early") {
                    evict(s, ops, &mut owner, &mut at, &mut queue, &height, ii);
                }
            }
        }
    }
    for (i, o) in ops.iter_mut().enumerate() {
        o.t = at[i].unwrap();
        o.e = o.t + o.occ;
    }
    true
}

/// `[(symbol, (first cycle, last cycle))]` of the values in registers.
pub type Life = Vec<(String, (i64, i64))>;

thread_local! {
    static LAST_II: std::cell::Cell<i64> = const { std::cell::Cell::new(0) };
}

/// The last II `plan` tried a placement at on this thread.
pub fn last_ii() -> i64 {
    LAST_II.with(|c| c.get())
}

fn lifetimes(ops: &[MOp], pinned: &HashSet<String>) -> Life {
    let mut life: Vec<(String, (i64, i64))> = Vec::new();
    let mut idx: HashMap<String, usize> = HashMap::new();
    for o in ops {
        for x in &o.writes {
            if pinned.contains(x) || x == PRED {
                continue;
            }
            match idx.get(x) {
                Some(&k) => {
                    let (s, e) = life[k].1;
                    life[k].1 = (s.min(o.t), e.max(o.e));
                }
                None => {
                    idx.insert(x.clone(), life.len());
                    life.push((x.clone(), (o.t.min(o.t), o.t.max(o.e))));
                }
            }
        }
    }
    for o in ops {
        for x in &o.reads {
            if pinned.contains(x) || x == PRED {
                continue;
            }
            let end = o.e + if o.eng == 'P' { 4 } else { 0 };
            match idx.get(x) {
                Some(&k) => {
                    let (s, e) = life[k].1;
                    life[k].1 = (s, e.max(end));
                }
                None => {
                    idx.insert(x.clone(), life.len());
                    life.push((x.clone(), (o.t, o.t.max(end))));
                }
            }
        }
    }
    life
}

/// Registers for (symbol, copy j): arcs on a circle of K x II, the lowest
/// free register from `first`.
pub fn colour(
    life: &Life,
    ii: i64,
    k: i64,
    regs: i64,
    first: i64,
) -> Option<HashMap<(String, i64), i64>> {
    if crate::mutant::on("l2.vector/colour-one") {
        let one = (0..k).flat_map(|j| life.iter().map(move |(x, _)| ((x.clone(), j), first)));
        return Some(one.collect());
    }
    let period = ii * k;
    let mut arcs: Vec<(i64, i64, &str, i64)> = Vec::new();
    for (x, (s, e)) in life {
        if e - s > period {
            return None;
        }
        for j in 0..k {
            let a = floor_mod(s + j * ii, period);
            arcs.push((a, a + e - s, x.as_str(), j));
        }
    }
    arcs.sort();
    let mut end: HashMap<i64, i64> = HashMap::new();
    let mut start: HashMap<i64, i64> = HashMap::new();
    let mut out = HashMap::new();
    for (s, e, x, j) in arcs {
        let mut placed = false;
        for r in first..regs {
            if let Some(&er) = end.get(&r) {
                if er > s || e - period > start[&r] {
                    continue;
                }
            }
            let ne = end.get(&r).map(|&v| v.max(e)).unwrap_or(e);
            end.insert(r, ne);
            start.entry(r).or_insert(s);
            out.insert((x.to_string(), j), r);
            placed = true;
            break;
        }
        if !placed {
            return None;
        }
    }
    Some(out)
}

#[derive(Clone, Debug)]
pub struct Plan {
    pub ops: Vec<MOp>,
    pub ii: i64,
    pub k: i64,
    pub stages: i64,
    pub phys: HashMap<(String, i64), i64>,
    pub fill_t: Option<i64>,
    pub drain_t: Option<i64>,
    pub s0: i64,
    pub core: Core,
}

impl Plan {
    pub fn stage(&self, t: i64) -> i64 {
        floor_div(t, self.ii) - self.s0
    }
}

fn padded(life: &Life, packed: &HashSet<String>, n: i64) -> Life {
    life.iter()
        .map(|(x, (s, e))| (x.clone(), (*s, e + if packed.contains(x) { n } else { 0 })))
        .collect()
}

/// The smallest II from `ii_min` whose placement fits and whose registers
/// colour within `regs` (pinned symbols on their own registers), at the
/// smallest K giving packed values PAD_MIN cycles of reuse slack (else the
/// most slack).
#[allow(clippy::too_many_arguments)]
pub fn plan(
    ops: &[MOp],
    regs: i64,
    pinned: &HashMap<String, i64>,
    k_max: i64,
    ii_max: i64,
    lead: i64,
    ii_min: i64,
    core: &Core,
) -> Option<Plan> {
    let first = pinned.values().copied().max().unwrap_or(-1) + 1;
    let lower = ['M', 'U', 'P']
        .iter()
        .map(|e| {
            ops.iter()
                .filter(|o| o.eng == *e)
                .map(|o| o.occ)
                .sum::<i64>()
        })
        .max()
        .unwrap();
    let pinned_set: HashSet<String> = pinned.keys().cloned().collect();
    let mut ops = ops.to_vec();
    for ii in lower.max(ii_min).max(1)..=ii_max {
        for o in ops.iter_mut() {
            o.deps.clear();
            o.t = 0;
            o.e = 0;
        }
        link(&mut ops, &core.timing);
        LAST_II.with(|c| c.set(ii));
        if !place(&mut ops, ii) {
            continue;
        }
        let life = lifetimes(&ops, &pinned_set);
        let us: Vec<&MOp> = ops.iter().filter(|o| o.eng == 'U').collect();
        let ps: Vec<&MOp> = ops.iter().filter(|o| o.eng == 'P').collect();
        let fill_t = us.iter().map(|o| o.t).min().map(|t| t - lead);
        let drain_t = ps.iter().map(|o| o.e).max();
        let mut times: Vec<i64> = ops.iter().map(|o| o.t).collect();
        times.extend(fill_t);
        times.extend(drain_t);
        let s0 = floor_div(*times.iter().min().unwrap(), ii);
        let stages = floor_div(*times.iter().max().unwrap(), ii) - s0 + 1;
        let mut k_min = 2;
        if let Some(ft) = fill_t {
            let ue = us.iter().map(|o| o.e).max().unwrap();
            k_min = k_min.max(-floor_div(-(ue - ft + 1), ii));
        }
        if let Some(dt) = drain_t {
            let pt = ps.iter().map(|o| o.t).min().unwrap();
            k_min = k_min.max(floor_div(dt - pt, ii) + 1);
        }
        let live: HashSet<&str> = life.iter().map(|(x, _)| x.as_str()).collect();
        let packed: HashSet<String> = ops
            .iter()
            .filter(|o| o.eng == 'P')
            .flat_map(|o| o.reads.iter())
            .filter(|x| live.contains(x.as_str()))
            .cloned()
            .collect();
        let writers: Vec<i64> = ops
            .iter()
            .filter(|o| o.writes.iter().any(|w| w == PRED))
            .map(|o| o.t)
            .collect();
        let readers: Vec<i64> = ops
            .iter()
            .filter(|o| o.reads.iter().any(|w| w == PRED))
            .map(|o| o.e)
            .collect();
        let p_span =
            readers.iter().copied().max().unwrap_or(0) - writers.iter().copied().min().unwrap_or(0);
        let mut best: Option<(i64, Plan)> = None;
        for k in k_min..=k_max {
            if !writers.is_empty() && p_span >= pred_copies(k, core.preds) * ii {
                continue;
            }
            let Some(mut phys) = colour(&life, ii, k, regs, first) else {
                continue;
            };
            let (mut lo, mut hi) = (0, ii * k);
            while lo < hi {
                let mid = floor_div(lo + hi + 1, 2);
                if colour(&padded(&life, &packed, mid), ii, k, regs, first).is_some() {
                    lo = mid;
                } else {
                    hi = mid - 1;
                }
            }
            if lo != 0 {
                phys = colour(&padded(&life, &packed, lo), ii, k, regs, first).unwrap();
            }
            let got = Plan {
                ops: ops.clone(),
                ii,
                k,
                stages,
                phys,
                fill_t,
                drain_t,
                s0,
                core: *core,
            };
            if lo >= PAD_MIN {
                return Some(got);
            }
            if best.as_ref().map(|(b, _)| lo > *b).unwrap_or(true) {
                best = Some((lo, got));
            }
        }
        if let Some((_, p)) = best {
            return Some(p);
        }
        let mut span = times.clone();
        for (_, (s, e)) in &life {
            span.push(*s);
            span.push(*e);
        }
        if ii >= span.iter().max().unwrap() - span.iter().min().unwrap()
            && !crate::mutant::on("l2.vector/ii-unbounded")
        {
            return None;
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The reference: each slot's run of free slots over three periods,
    /// then the first start in lo..lo+ii-1 whose run holds `occ`.
    fn first_fit_table(row: &[Option<usize>], lo: i64, occ: i64, ii: i64) -> Option<i64> {
        let iiu = ii as usize;
        let n = 3 * iiu;
        let mut run = vec![0usize; n + 1];
        for k in (0..n).rev() {
            run[k] = if row[k % iiu].is_none() {
                run[k + 1] + 1
            } else {
                0
            };
        }
        let s = floor_mod(lo, ii) as usize;
        (0..iiu)
            .find(|&d| run[s + d] >= occ as usize)
            .map(|d| lo + d as i64)
    }

    #[test]
    fn first_fit_is_the_table_scan() {
        let mut x: u64 = 0x9e37_79b9_7f4a_7c15;
        let mut next = |m: u64| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            x % m
        };
        let (mut found, mut none) = (0, 0);
        for _ in 0..200_000 {
            let ii = 1 + next(40) as i64;
            let busy = next(101);
            let row: Vec<Option<usize>> =
                (0..ii).map(|_| (next(100) < busy).then_some(0)).collect();
            let lo = next(400) as i64 - 200;
            let occ = next(ii as u64 + 2) as i64 - 1;
            let want = first_fit_table(&row, lo, occ, ii);
            assert_eq!(
                first_fit(&row, lo, occ, ii),
                want,
                "row {row:?} lo {lo} occ {occ} ii {ii}"
            );
            match want {
                Some(_) => found += 1,
                None => none += 1,
            }
        }
        assert!(
            found > 10_000 && none > 10_000,
            "{found} found, {none} none"
        );
    }
}
