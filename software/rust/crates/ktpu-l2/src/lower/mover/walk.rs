//! Walk levels: merging, squeezing, cutting into moves a walker takes
//! (L2.MOV.008), their text, spans, and `walk=` levels.

use std::collections::{BTreeSet, HashMap, HashSet};

use super::*;

/// Adjacent levels one run of the other merged: (a, s*b), (b, s) -> (a*b, s).
pub fn merge(levels: &[(i64, i64)]) -> Levels {
    if on("l2.mover/no-merge") {
        return levels.to_vec();
    }
    let mut out: Levels = Vec::new();
    for &(n, s) in levels.iter().rev() {
        match out.last() {
            Some(&(m, t)) if t * m == s => {
                out.pop();
                out.push((n * m, t));
            }
            _ => out.push((n, s)),
        }
    }
    out.reverse();
    out
}

/// Levels of one step dropped (a walk keeps at least one).
pub fn squeeze(levels: &[(i64, i64)]) -> Levels {
    let out: Levels = levels.iter().copied().filter(|l| l.0 != 1).collect();
    if out.is_empty() {
        levels.last().map(|l| vec![*l]).unwrap_or_default()
    } else {
        out
    }
}

pub(super) fn walk_text(name: &str, off: i64, levels: &[(i64, i64)]) -> String {
    let lv: Vec<String> = merge(&squeeze(levels))
        .iter()
        .map(|(n, s)| format!("({n}, {s})"))
        .collect();
    format!("w({name} + {off}, {})", lv.join(", "))
}

/// `(buffer, lo, hi)` bytes a walk touches, its last element `step` long.
pub(super) fn span_of(
    name: &str,
    off: i64,
    levels: &[(i64, i64)],
    step: i64,
) -> (String, i64, i64) {
    let (mut lo, mut hi) = (off, off);
    for &(n, s) in levels {
        if s >= 0 {
            hi += (n - 1) * s;
        } else {
            lo += (n - 1) * s;
        }
    }
    (name.to_string(), lo, hi + step)
}

pub(super) fn count(levels: &[(i64, i64)]) -> i64 {
    levels.iter().map(|l| l.0).product()
}

/// Words in the innermost contiguous run of a walk.
pub fn run_words(levels: &[(i64, i64)], word: i64) -> i64 {
    let (n, s) = *merge(&squeeze(levels)).last().unwrap();
    if s == word {
        n
    } else {
        1
    }
}

/// `[(byte offsets, levels)]`, one per walk each: `walks` (equal step counts)
/// as consecutive runs of steps, each at most `limit` steps and each walk's
/// levels -- with its `tails` and `spare` levels a caller puts outside -- at
/// most a walker's levels (L2.MOV.008).
pub fn cut(
    walks: &[Levels],
    limit: i64,
    tails: Option<&[Levels]>,
    spare: usize,
    tg: &Target,
) -> Vec<(Vec<i64>, Vec<Levels>)> {
    if on("l2.mover/no-cut") {
        return vec![(
            vec![0; walks.len()],
            walks.iter().map(|w| squeeze(w)).collect(),
        )];
    }
    let walks = aligned(&walks.iter().map(|w| squeeze(w)).collect::<Vec<_>>());
    let empty: Vec<Levels> = vec![vec![]; walks.len()];
    let tails: &[Levels] = tails.unwrap_or(&empty);
    let total = count(&walks[0]);
    let ndim = tg.levels;
    let fits = |levels: &[(i64, i64)], tail: &[(i64, i64)]| -> bool {
        let lv: Levels = levels.iter().chain(tail).copied().collect();
        let n = if lv.is_empty() {
            0
        } else {
            merge(&squeeze(&lv)).len()
        };
        n + spare <= ndim
    };
    if total <= limit && walks.iter().zip(tails).all(|(lv, t)| fits(lv, t)) {
        return vec![(vec![0; walks.len()], walks)];
    }
    let at: Vec<HashMap<i64, usize>> = walks
        .iter()
        .map(|lv| {
            let mut units = HashMap::new();
            let mut u = 1;
            for i in (0..=lv.len()).rev() {
                units.insert(u, i);
                if i > 0 {
                    u *= lv[i - 1].0;
                }
            }
            units
        })
        .collect();
    let mut common: BTreeSet<i64> = at[0].keys().copied().collect();
    for a in &at[1..] {
        let k: HashSet<i64> = a.keys().copied().collect();
        common.retain(|u| k.contains(u));
    }
    let mut unit = None;
    for &u in common.iter().rev() {
        if u > limit || u == total {
            continue;
        }
        let inner: Vec<&[(i64, i64)]> = walks.iter().zip(&at).map(|(lv, a)| &lv[a[&u]..]).collect();
        if inner.iter().zip(tails).all(|(i, t)| fits(i, t)) {
            unit = Some(u);
            break;
        }
    }
    let unit = unit.expect("one step always fits a walker");
    let outer: Vec<&[(i64, i64)]> = walks
        .iter()
        .zip(&at)
        .map(|(lv, a)| &lv[..a[&unit]])
        .collect();
    let inner: Vec<&[(i64, i64)]> = walks
        .iter()
        .zip(&at)
        .map(|(lv, a)| &lv[a[&unit]..])
        .collect();
    let group = outer.iter().zip(&inner).zip(tails).all(|((o, i), t)| {
        let mut l = vec![(2, o.last().unwrap().1)];
        l.extend(i.iter().copied());
        fits(&l, t)
    });
    let most = if group { limit / unit } else { 1 };
    let mut out = Vec::new();
    let (mut b, blocks) = (0, total / unit);
    while b < blocks {
        let mut run = most.min(blocks - b);
        for o in &outer {
            let n = o.last().unwrap().0;
            run = run.min(n - b % n);
        }
        let mut offs = Vec::new();
        let mut lvs = Vec::new();
        for (o, i) in outer.iter().zip(&inner) {
            let (mut off, mut rest) = (0, b);
            for &(n, s) in o.iter().rev() {
                off += rest % n * s;
                rest /= n;
            }
            offs.push(off);
            let mut l = vec![(run, o.last().unwrap().1)];
            l.extend(i.iter().copied());
            lvs.push(l);
        }
        out.push((offs, lvs));
        b += run;
    }
    out
}

/// Each walk's levels split where another walk's level boundaries fall
/// inside them.
fn aligned(walks: &[Levels]) -> Vec<Levels> {
    let mut marks: BTreeSet<i64> = BTreeSet::new();
    for lv in walks {
        let mut u = 1;
        for &(n, _) in lv.iter().rev() {
            u *= n;
            marks.insert(u);
        }
    }
    let mut out = Vec::new();
    for lv in walks {
        let mut new: Levels = Vec::new();
        let mut u = 1;
        for &(n, s) in lv.iter().rev() {
            let mut prev = 1;
            let mut cs: Vec<i64> = marks
                .iter()
                .filter(|&&m| m % u == 0 && 1 < m / u && m / u < n)
                .map(|&m| m / u)
                .collect();
            cs.sort();
            for c in cs {
                if n % c == 0 && c % prev == 0 {
                    new.push((c / prev, s * prev));
                    prev = c;
                }
            }
            new.push((n / prev, s * prev));
            u *= n;
        }
        new.reverse();
        out.push(new);
    }
    out
}

pub(super) fn walk_levels(body: &Body, st: &Stmt, t: &Term, env: &Env) -> Result<Levels> {
    let Some(items) = tuple(t) else {
        return Err(fail(body, st, "L2.MOV.015", "walk=((COUNT, STRIDE), ...)"));
    };
    if items.is_empty() {
        return Err(fail(body, st, "L2.MOV.015", "walk=((COUNT, STRIDE), ...)"));
    }
    let mut out = Vec::new();
    for lv in items {
        let Some(pair) = tuple(lv) else {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!("a walk level is (COUNT, STRIDE), got {}", repr(lv)),
            ));
        };
        if pair.len() != 2 {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!("a walk level is (COUNT, STRIDE), got {}", repr(lv)),
            ));
        }
        let n = const_of(body, st, &pair[0], env)?;
        let s = const_of(body, st, &pair[1], env)?;
        if n < 1 {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!("a walk level of {n} steps"),
            ));
        }
        out.push((n, s));
    }
    Ok(out)
}
