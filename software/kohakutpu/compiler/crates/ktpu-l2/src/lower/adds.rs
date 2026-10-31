//! Clusters adding their accumulator tiles into others' (L2.MG.013-.014).

use super::*;

/// `(source node, source index, target node, target index)` of an
/// accumulator add.
type Add = (i64, i64, i64, Option<i64>);

/// Every accumulator [`Add`] of the stage.
pub(super) fn adds(body: &Body, insts: &[Instance]) -> Result<Vec<Add>> {
    let mut out = Vec::new();
    for i in insts {
        if i.kind != "mg" {
            continue;
        }
        for (n, env) in &i.items {
            let Node::Op(op) = n.as_ref() else { continue };
            if op.op != "drain"
                || !op
                    .st
                    .positional()
                    .iter()
                    .any(|p| p.as_name() == Some("add"))
            {
                continue;
            }
            let to = value(body, &op.st, op.st.positional()[2], env)?;
            let Term::View(tn, sl) = &to else { continue };
            if tn != "mg" || sl.is_empty() {
                continue;
            }
            let far = match op.st.kw("node") {
                Some(k) => const_of(body, &op.st, k, env)?,
                None => i.node,
            };
            out.push((
                i.node,
                i.index.unwrap(),
                far,
                sl[0].as_int().map(|v| v as i64),
            ));
        }
    }
    Ok(out)
}

/// Clusters of this node another cluster adds its tile into.
pub(super) fn added_into(
    body: &Body,
    insts: &[Instance],
    remote: &[(i64, i64)],
) -> Result<BTreeSet<i64>> {
    let mut out: BTreeSet<i64> = adds(body, insts)?
        .into_iter()
        .filter(|(s, _, n, _)| n == s)
        .filter_map(|(_, _, _, t)| t)
        .collect();
    out.extend(remote.iter().map(|(_, t)| *t));
    Ok(out)
}

/// Clusters adding their tiles into others' (L2.MG.013-.014).
#[allow(clippy::too_many_arguments)]
pub(super) fn added(
    body: &Body,
    streams: &[(i64, Stream)],
    into: &BTreeSet<i64>,
    st: &Stmt,
    remote: &[(i64, i64)],
    node: i64,
    t: &Target,
) -> Result<Vec<String>> {
    let add = add_slot(t);
    // The COUNTed token behind a peer drain is one MXFP7 entry (L2.MG.013).
    let token = t.entry_words();
    let (mut heads, mut tails, mut addl) = (Vec::new(), Vec::new(), Vec::new());
    for (idx, s) in streams {
        let cut = s
            .lines
            .iter()
            .position(|l| l.drain)
            .unwrap_or(s.lines.len());
        let line = s.lines.get(cut);
        let cut_tail = match line {
            Some(l) if l.into.is_some() => {
                addl.push((*idx, l.clone()));
                cut + 1
            }
            _ => cut,
        };
        if into.contains(idx) && line.map(|l| l.into.is_some()).unwrap_or(true) {
            return Err(body.reader.fail(
                st,
                "L2.MG.023",
                format!("mg[{idx}] is added into but drains nothing after"),
            ));
        }
        heads.push((*idx, s.lines[..cut].to_vec()));
        tails.push((*idx, s.lines[cut_tail..].to_vec()));
    }
    let mut out = interleave(&heads);
    let mut marked: IndexMap<i64, i64> = IndexMap::new();
    // Adds into one cluster land one at a time (L1.VER.008 P1): the previous
    // add's entry is taken before the next sender goes.
    let ordered = !on("l2.lower/adds-unordered");
    let release = |out: &mut Vec<String>, marked: &mut IndexMap<i64, i64>, j: i64| {
        let m = marked.entry(j).or_insert(0);
        if *m > 0 && ordered {
            out.push(format!("send mg[{j}]"));
            out.push(format!("{IND}fill A0 recv n=1"));
            *m -= 1;
        }
        out.push(format!("wait mg[{j}]"));
        *m += 1;
    };
    for &(src_node, j) in remote {
        release(&mut out, &mut marked, j);
        out.push(format!("ring mesh={src_node} slot={}", add + node));
    }
    let mut sent_far: Option<i64> = None;
    for (src, line) in &addl {
        let into_j = line.into.unwrap();
        if let Some(far) = line.far {
            if let Some(sf) = sent_far.filter(|_| !on("l2.lower/far-adds-at-once")) {
                out.push(format!("wait mg[{sf}]"));
            }
            out.push(format!("wait slot {} n=1 consume", add + far));
            out.push(format!("send mg[{src}]"));
            out.push(format!("{IND}{}", line.render()));
            out.push(format!(
                "{IND}drain n={token} to=mesh{far}.mg[{into_j}] buf=0 l1=0 count"
            ));
            sent_far = Some(*src);
            continue;
        }
        release(&mut out, &mut marked, into_j);
        out.push(format!("send mg[{src}]"));
        out.push(format!("{IND}{}", line.render()));
        out.push(format!(
            "{IND}drain n={token} to=mg[{into_j}] buf=0 l1=0 count"
        ));
    }
    let tails: Vec<(i64, Vec<Line>)> = tails
        .into_iter()
        .map(|(idx, ls)| match marked.get(&idx) {
            Some(&n) => {
                let seg = ls.first().map(|l| l.seg).unwrap_or(0);
                let mut v = vec![Line::new(format!("fill A0 recv n={n}"), seg)];
                v.extend(ls);
                (idx, v)
            }
            None => (idx, ls),
        })
        .collect();
    out.extend(interleave(&tails));
    Ok(out)
}
