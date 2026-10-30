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

#[cfg(feature = "witness")]
enum Ev {
    Insn(String, Option<&'static str>),
    Sync(&'static str, &'static str, Option<i64>),
}

fn kind_ix(k: &str) -> usize {
    match k {
        "fill" => 0,
        "unpack" => 1,
        "pack" => 2,
        _ => 3,
    }
}

/// Replace whole-word `p1` by `with`.
#[cfg(feature = "witness")]
fn sub_p1(line: &str, with: &str) -> String {
    let b = line.as_bytes();
    let w = |c: u8| c.is_ascii_alphanumeric() || c == b'_';
    let mut out = String::with_capacity(line.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'p'
            && i + 1 < b.len()
            && b[i + 1] == b'1'
            && (i == 0 || !w(b[i - 1]))
            && (i + 2 >= b.len() || !w(b[i + 2]))
        {
            out.push_str(with);
            i += 2;
            continue;
        }
        out.push(b[i] as char);
        i += 1;
    }
    out
}

/// `(lines, modelled cycles)`: the prologue, a VLOOP over K instances, the
/// rest (`tail`); None where the loop body is not periodic or a sync would
/// precede its producer.
#[cfg(feature = "witness")]
pub fn emit(
    p: &Plan,
    rings: &[Ring],
    trips: i64,
    pinned: &HashMap<String, i64>,
    tail: &str,
) -> Result<Option<(Vec<String>, i64)>, String> {
    let (ii, k, s_n) = (p.ii, p.k, p.stages);
    let ops = &p.ops;
    let mut items: Vec<(i64, i64)> = ops
        .iter()
        .enumerate()
        .map(|(i, o)| (o.t, i as i64))
        .collect();
    if let Some(f) = p.fill_t {
        items.push((f, FILL));
    }
    if let Some(d) = p.drain_t {
        items.push((d, DRAIN));
    }
    let mut order = items.clone();
    order.sort_by_key(|x| (floor_mod(x.0, ii), x.0));
    let loads: Vec<&Ring> = rings.iter().filter(|r| r.kind == "load").collect();
    let stores: Vec<&Ring> = rings.iter().filter(|r| r.kind == "store").collect();
    let us: Vec<usize> = (0..ops.len()).filter(|&i| ops[i].eng == 'U').collect();
    let ps: Vec<usize> = (0..ops.len()).filter(|&i| ops[i].eng == 'P').collect();
    let min_t = |v: &[usize]| -> Option<usize> {
        let mut best: Option<usize> = None;
        for &i in v {
            if best.map(|b| ops[i].t < ops[b].t).unwrap_or(true) {
                best = Some(i);
            }
        }
        best
    };
    let max_t = |v: &[usize]| -> Option<usize> {
        let mut best: Option<usize> = None;
        for &i in v {
            if best.map(|b| ops[i].t > ops[b].t).unwrap_or(true) {
                best = Some(i);
            }
        }
        best
    };
    let (first_u, last_u, first_p, last_p) = (min_t(&us), max_t(&us), min_t(&ps), max_t(&ps));
    let ring_of = |word: i64| -> Result<&Ring, String> {
        rings
            .iter()
            .find(|r| r.base <= word && word < r.base + r.words)
            .ok_or_else(|| format!("l1[{word}] is in no ring"))
    };
    let text = |i: usize, j: i64| -> Result<String, String> {
        let o = &ops[i];
        let named = pat::syms(&o.tmpl);
        let names: Vec<String> = o
            .syms
            .iter()
            .enumerate()
            .map(|(n, s)| match pinned.get(s) {
                Some(r) => format!("v{r}"),
                None if !named.contains(&n) => String::new(),
                None => format!("v{}", p.phys[&(s.clone(), floor_mod(j, k))]),
            })
            .collect();
        let mut line = pat::format(&o.tmpl, &names);
        if o.reads.iter().any(|x| x == PRED) || o.writes.iter().any(|x| x == PRED) {
            let pc = 1 + floor_mod(floor_mod(j, k), pred_copies(k, p.core.preds));
            line = sub_p1(&line, &format!("p{pc}"));
        }
        if let Some(l1) = o.l1 {
            let r = ring_of(l1)?;
            let at = r.slots[floor_mod(j, k) as usize] + (l1 - r.base);
            if let Some((a, b, _)) = pat::l1_word(&line) {
                line = format!("{}l1[{at}]{}", &line[..a], &line[b..]);
            }
        }
        Ok(line)
    };

    let mut seq: Vec<Ev> = Vec::new();
    let mut count = [0i64; 4];
    let mut done: [HashMap<i64, i64>; 4] = Default::default();

    let n_inst = trips + s_n - 1;
    let lo_loop = s_n - 1;
    let reps = floor_div(
        0.max(if tail == "kernel" { n_inst } else { trips } - lo_loop),
        k,
    );
    let hi_loop = lo_loop + reps * k;
    dataflow(p, &order, trips, pinned, n_inst, tail, lo_loop, hi_loop)?;
    let mut marks: Vec<usize> = Vec::new();
    macro_rules! wait {
        ($waiter:expr, $on:expr, $j:expr) => {{
            let j: i64 = $j;
            let on: &'static str = $on;
            if j >= 0 && !done[kind_ix(on)].contains_key(&j) {
                false
            } else {
                let need = if j >= 0 {
                    Some(done[kind_ix(on)][&j])
                } else {
                    None
                };
                seq.push(Ev::Sync($waiter, on, need));
                true
            }
        }};
    }
    macro_rules! insn {
        ($t:expr, $kind:expr) => {{
            let kind: Option<&'static str> = $kind;
            if let Some(kd) = kind {
                count[kind_ix(kd)] += 1;
            }
            seq.push(Ev::Insn($t, kind));
        }};
    }
    for n in 0..n_inst {
        marks.push(seq.len());
        let past = tail == "kernel" && lo_loop <= n && n < hi_loop;
        for &(t, i) in &order {
            let j = n - p.stage(t);
            if j < 0 || (j >= trips && !past) {
                continue;
            }
            if i == FILL {
                if !wait!("fill", "unpack", j - k) {
                    return Ok(None);
                }
                for r in &loads {
                    insn!(
                        format!(
                            "vfill a{} -> l1[{}] rel",
                            r.ad,
                            r.slots[floor_mod(j, k) as usize]
                        ),
                        Some("fill")
                    );
                }
                done[0].insert(j, count[0]);
            } else if i == DRAIN {
                if j >= trips || !wait!("drain", "pack", j) {
                    return Ok(None);
                }
                for r in &stores {
                    insn!(
                        format!(
                            "vdrain a{} <- l1[{}] rel",
                            r.ad,
                            r.slots[floor_mod(j, k) as usize]
                        ),
                        Some("drain")
                    );
                }
                done[3].insert(j, count[3]);
            } else {
                let iu = i as usize;
                match ops[iu].eng {
                    'U' => {
                        if Some(iu) == first_u && p.fill_t.is_some() && !wait!("unpack", "fill", j)
                        {
                            return Ok(None);
                        }
                        insn!(text(iu, j)?, Some("unpack"));
                        if Some(iu) == last_u {
                            done[1].insert(j, count[1]);
                        }
                    }
                    'P' => {
                        if Some(iu) == first_p
                            && p.drain_t.is_some()
                            && !wait!("pack", "drain", j - k)
                        {
                            return Ok(None);
                        }
                        insn!(text(iu, j)?, Some("pack"));
                        if Some(iu) == last_p {
                            done[2].insert(j, count[2]);
                        }
                    }
                    _ => insn!(text(iu, j)?, None),
                }
            }
        }
    }
    marks.push(seq.len());
    let mut run = [0i64; 4];
    let mut exact: Vec<Option<i64>> = Vec::new();
    for ev in &seq {
        match ev {
            Ev::Insn(_, kind) => {
                if let Some(kd) = kind {
                    run[kind_ix(kd)] += 1;
                }
                exact.push(None);
            }
            Ev::Sync(_, on, need) => exact.push(Some(match need {
                None => 63,
                Some(nd) => 0.max(63.min(run[kind_ix(on)] - nd)),
            })),
        }
    }
    let mut lines: Vec<String> = Vec::new();
    let put =
        |lines: &mut Vec<String>, lo: usize, hi: usize, slack_of: &dyn Fn(usize) -> Option<i64>| {
            for (q, ev) in seq.iter().enumerate().take(hi).skip(lo) {
                match ev {
                    Ev::Insn(t, _) => lines.push(t.clone()),
                    Ev::Sync(w, on, _) => {
                        let s = slack_of(q)
                            .map(|v| v.to_string())
                            .unwrap_or_else(|| "None".into());
                        lines.push(format!("vsync {w} on={on} slack={s}"));
                    }
                }
            }
        };
    put(&mut lines, 0, marks[0], &|q| exact[q]);
    put(&mut lines, marks[0], marks[lo_loop as usize], &|q| exact[q]);
    if reps > 0 {
        let (a, b) = (marks[lo_loop as usize], marks[(lo_loop + k) as usize]);
        let per = b - a;
        for rep in 0..reps {
            let a2 = marks[(lo_loop + rep * k) as usize];
            for q in 0..per {
                let same = match (&seq[a + q], &seq[a2 + q]) {
                    (Ev::Insn(x, _), Ev::Insn(y, _)) => x == y,
                    (Ev::Sync(w1, o1, _), Ev::Sync(w2, o2, _)) => w1 == w2 && o1 == o2,
                    _ => false,
                };
                #[cfg(test)]
                typed::record_periodicity(0, same);
                if !same {
                    return Ok(None);
                }
            }
        }
        let mut slack: Vec<Option<i64>> = Vec::new();
        for q in 0..per {
            let vals: Vec<i64> = (0..reps)
                .filter_map(|rep| exact[marks[(lo_loop + rep * k) as usize] + q])
                .collect();
            slack.push(vals.iter().copied().min());
        }
        lines.push(format!("seti s{} = {reps}", p.core.loop_s));
        lines.push(format!("loop s{}", p.core.loop_s));
        lines.push("{".into());
        put(&mut lines, a, b, &|q| slack[q - a]);
        lines.push("}".into());
    }
    put(
        &mut lines,
        marks[hi_loop as usize],
        marks[n_inst as usize],
        &|q| exact[q],
    );
    Ok(Some((lines, ii * n_inst)))
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
