//! Shared fills: the node as the rendezvous of clusters reading one view
//! (L2.MG.005, .019).

use super::*;

/// A stream cut before each shared fill.
fn steps(lines: &[Line]) -> Vec<Vec<Line>> {
    let mut out: Vec<Vec<Line>> = vec![vec![]];
    let mut prev: Option<&ShareKey> = None;
    for ln in lines {
        if ln.share.is_some() && ln.key.as_ref() != prev {
            out.push(vec![]);
        }
        prev = if ln.share.is_some() {
            ln.key.as_ref()
        } else {
            None
        };
        out.last_mut().unwrap().push(ln.clone());
    }
    out
}

/// Instances sharing one read: `[(leader, [followers])]` (L2.MG.005, .019).
fn groups(
    body: &Body,
    st: &Stmt,
    insts: &[Instance],
    mode: &str,
    t: &Target,
) -> Result<Vec<(i64, Vec<i64>)>> {
    if on("l2.lower/share-unpaired") {
        return Ok(insts.iter().map(|i| (i.index.unwrap(), vec![])).collect());
    }
    let mut coords = t.mg.clone();
    coords.sort();
    if let Some(i) = insts
        .iter()
        .find(|i| i.index.unwrap() as usize >= coords.len())
    {
        return Err(body.reader.fail(
            st,
            "L2.MG.005",
            format!(
                "share=: mg[{}] on a node of {} clusters",
                i.index.unwrap(),
                coords.len()
            ),
        ));
    }
    let where_: HashMap<i64, (i64, i64)> = insts
        .iter()
        .map(|i| (i.index.unwrap(), coords[i.index.unwrap() as usize]))
        .collect();
    let sets: Vec<Vec<i64>> = if mode == "all" {
        vec![insts.iter().map(|i| i.index.unwrap()).collect()]
    } else {
        let mut by_port: std::collections::BTreeMap<(i64, i64), Vec<i64>> = Default::default();
        for i in insts {
            let idx = i.index.unwrap();
            by_port
                .entry(t.nearest_port(where_[&idx]))
                .or_default()
                .push(idx);
        }
        let ports: Vec<Vec<i64>> = by_port.into_values().collect();
        if mode == "port" || ports.len() < 2 {
            ports
        } else {
            let n = ports[0].len();
            if ports.iter().any(|p| p.len() != n) {
                return Err(body.reader.fail(
                    st,
                    "L2.MG.005",
                    "share=cross: every memory port serves as many of the clusters",
                ));
            }
            (0..n)
                .map(|k| ports.iter().map(|p| p[k]).collect())
                .collect()
        }
    };
    Ok(sets
        .into_iter()
        .map(|mut s| {
            s.sort_by_key(|u| (where_[u].1 << 4) | where_[u].0);
            (s[0], s[1..].to_vec())
        })
        .collect())
}

/// The node as the rendezvous of shared fills (L2.MG.005).
pub(super) fn shared(
    body: &Body,
    insts: &[Instance],
    streams: Vec<(i64, Stream)>,
    t: &Target,
) -> Result<Vec<String>> {
    let pst = &insts[0].par().st;
    let mut cut: IndexMap<i64, Vec<Vec<Line>>> =
        streams.iter().map(|(i, s)| (*i, steps(&s.lines))).collect();
    let keys: Vec<Vec<Option<ShareKey>>> = cut
        .values()
        .map(|st| st[1..].iter().map(|s| s[0].key.clone()).collect())
        .collect();
    if keys.iter().any(|k| *k != keys[0]) {
        return Err(body.reader.fail(
            pst,
            "L2.MG.019",
            "share=: every instance fills the same shared views, in the same order",
        ));
    }
    let modes: BTreeSet<String> = streams
        .iter()
        .flat_map(|(_, s)| s.lines.iter().filter_map(|l| l.share.clone()))
        .collect();
    if modes.len() != 1 {
        return Err(body
            .reader
            .fail(pst, "L2.MG.019", "share=: one mode per par"));
    }
    let grps = groups(body, pst, insts, modes.iter().next().unwrap(), t)?;
    let mut peers: HashMap<i64, Vec<i64>> = HashMap::new();
    for (lead, follow) in &grps {
        let mut members = vec![*lead];
        members.extend(follow.iter().copied());
        for &u in &members {
            peers.insert(u, members.iter().copied().filter(|p| *p != u).collect());
        }
    }
    for (idx, st) in cut.iter_mut() {
        for step in st.iter_mut().skip(1) {
            for ln in step.iter_mut() {
                if ln.share.is_some() {
                    ln.peers = peers[idx].clone();
                }
            }
        }
    }
    let mut out = Vec::new();
    let send = |out: &mut Vec<String>, idx: i64, lines: &[Line]| {
        if !lines.is_empty() {
            out.push(format!("send mg[{idx}]"));
            out.extend(lines.iter().map(|l| format!("{IND}{}", l.render())));
        }
    };
    for (idx, st) in &cut {
        send(&mut out, *idx, &st[0]);
    }
    for j in 1..=keys[0].len() {
        for (_, follow) in &grps {
            for f in follow {
                let step = &cut[f][j];
                // ISA.MG.024: a later fill cannot cross a sweep that retires on issue.
                let prefix = step
                    .iter()
                    .take_while(|l| on("l2.lower/share-hoist-fills") || l.fill)
                    .count();
                let (own, rest): (Vec<_>, Vec<_>) = step
                    .iter()
                    .enumerate()
                    .partition(|(i, l)| *i < prefix && l.fill && l.share.is_none());
                let own: Vec<Line> = own.into_iter().map(|(_, l)| l.clone()).collect();
                let rest: Vec<Line> = rest.into_iter().map(|(_, l)| l.clone()).collect();
                send(&mut out, *f, &own);
                out.push(format!("t{f} = mark mg[{f}]"));
                send(&mut out, *f, &rest);
            }
        }
        for (lead, follow) in &grps {
            for f in follow {
                out.push(format!("wait mg[{f}] upto=t{f}"));
            }
            send(&mut out, *lead, &cut[lead][j]);
        }
    }
    Ok(out)
}
