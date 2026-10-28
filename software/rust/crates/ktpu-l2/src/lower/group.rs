//! Stages joined by a stream (L2.LOW.008) and the closing mover stage.

use super::*;

fn rings_of(s: &Stage) -> HashSet<(String, String)> {
    s.accesses
        .iter()
        .filter(|a| a.flow)
        .map(|a| a.key.clone())
        .collect()
}

/// Consecutive stages joined by a stream as one stage (L2.LOW.008).
pub(super) fn grouped(stages: Vec<Stage>) -> Vec<Stage> {
    let mut out: Vec<Stage> = Vec::new();
    for s in stages {
        if let Some(prev) = out.last_mut() {
            if !prev.segs.is_empty()
                && !s.segs.is_empty()
                && prev.group == s.group
                && !rings_of(prev).is_disjoint(&rings_of(&s))
            {
                prev.accesses.extend(s.accesses.iter().cloned());
                let mut units = prev.units.clone();
                for u in &s.units {
                    if !units.contains(u) {
                        units.push(u.clone());
                    }
                }
                prev.units = units;
                prev.members.push(s);
                continue;
            }
        }
        let mut s = s;
        s.members = vec![Stage {
            segs: s.segs.clone(),
            ..Default::default()
        }];
        out.push(s);
    }
    for s in out.iter_mut() {
        if s.members.len() > 1 {
            let groups: Vec<Vec<Vec<String>>> = s.members.iter().map(|m| m.segs.clone()).collect();
            s.lines = merge_segs(&groups);
        }
    }
    out
}

fn merge_segs(groups: &[Vec<Vec<String>>]) -> Vec<String> {
    let is_run = |g: &Vec<Vec<String>>| {
        g.iter().flatten().all(|ln| {
            let l = ln.trim_start();
            l.starts_with("send vc") || l.starts_with("run ")
        })
    };
    let runs: Vec<&Vec<Vec<String>>> = groups.iter().filter(|g| is_run(g)).collect();
    let rest: Vec<&Vec<Vec<String>>> = groups
        .iter()
        .filter(|g| !runs.iter().any(|r| std::ptr::eq(*r, *g)))
        .collect();
    let mut lines: Vec<String> = runs
        .iter()
        .flat_map(|g| g.iter().flatten().cloned())
        .collect();
    let mut at = vec![0usize; rest.len()];
    while (0..rest.len()).any(|k| at[k] < rest[k].len()) {
        let k = (0..rest.len())
            .filter(|&k| at[k] < rest[k].len())
            .min_by(|&a, &b| {
                let fa = at[a] as f64 / rest[a].len() as f64;
                let fb = at[b] as f64 / rest[b].len() as f64;
                fa.partial_cmp(&fb).unwrap()
            })
            .unwrap();
        lines.extend(rest[k][at[k]].iter().cloned());
        at[k] += 1;
    }
    lines
}

/// A last mover stage is a plain `move`: the node waits for it and every unit.
pub(super) fn close(mut stages: Vec<Stage>) -> Vec<Stage> {
    if let Some(last) = stages.last_mut() {
        if last.post.is_some() {
            last.lines[0] = "move".into();
            last.post = None;
            last.closes = true;
        }
    }
    stages
}
