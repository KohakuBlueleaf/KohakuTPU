//! Work across nodes: the same `par` on every node, accumulator adds into
//! another node (L2.MG.014) and cross-node order through shared memory
//! (L2.ORDER.009).

use super::*;

/// The variables of the `par`s over nodes.
pub(super) fn node_vars(stmts: &[Arc<Node>]) -> HashSet<String> {
    fn walk(stmts: &[Arc<Node>], out: &mut HashSet<String>) {
        for s in stmts {
            if let Node::Par(p) = s.as_ref() {
                if p.kind == "node" {
                    out.insert(p.var.clone());
                }
            }
            walk(s.body(), out);
        }
    }
    let mut out = HashSet::new();
    walk(stmts, &mut out);
    out
}

pub(super) type StageKey = (usize, Vec<(String, i64)>);

/// The same `par` run on every node: its statement and the loop values
/// around it, the node's own left out.
pub(super) fn stage_key(node_vars: &HashSet<String>, insts: &[Instance]) -> StageKey {
    let own = &insts[0].par().var;
    let mut env: Vec<(String, i64)> = insts[0]
        .env
        .iter()
        .filter(|(k, _)| *k != own && !node_vars.contains(*k))
        .map(|(k, v)| (k.clone(), if let Val::Int(i) = v { *i as i64 } else { 0 }))
        .collect();
    env.sort();
    (insts[0].par.uid(), env)
}

/// `{(target node, stage key): [(source node, target index)]}`.
type RemoteAdds = HashMap<(i64, StageKey), Vec<(i64, i64)>>;

/// The [`RemoteAdds`] of every add across nodes (L2.MG.014).
pub(super) fn remote_adds(
    body: &Body,
    stages: &[crate::ir::Stage],
    node_vars: &HashSet<String>,
) -> Result<RemoteAdds> {
    let have: HashSet<(i64, StageKey)> = stages
        .iter()
        .filter_map(|s| match &s.x {
            StageX::Par(x) => Some((s.node, stage_key(node_vars, x))),
            _ => None,
        })
        .collect();
    let mut out: RemoteAdds = HashMap::new();
    for s in stages {
        let StageX::Par(x) = &s.x else { continue };
        for (src, _, tgt, j) in adds(body, x)? {
            if tgt == src {
                continue;
            }
            let key = (tgt, stage_key(node_vars, x));
            if !have.contains(&key) {
                return Err(body.reader.fail(
                    &x[0].par().st,
                    "L2.MG.014",
                    format!("node {src} adds into node {tgt}, which runs no instance of this par"),
                ));
            }
            out.entry(key).or_default().push((src, j.unwrap_or(0)));
        }
    }
    // Receivers consume shallower partials before a dependent subtotal.
    type Unit = (i64, i64);
    let mut edges: HashMap<StageKey, Vec<(Unit, Unit)>> = HashMap::new();
    for s in stages {
        let StageX::Par(x) = &s.x else { continue };
        let key = stage_key(node_vars, x);
        for (src, unit, dst, target) in adds(body, x)? {
            edges
                .entry(key.clone())
                .or_default()
                .push(((src, unit), (dst, target.unwrap_or(0))));
        }
    }
    let mut depths = HashMap::new();
    for (key, edges) in &edges {
        let mut pending = edges.clone();
        let mut depth = HashMap::new();
        while !pending.is_empty() {
            let (ready, later): (Vec<_>, Vec<_>) = pending
                .iter()
                .copied()
                .partition(|(src, _)| !pending.iter().any(|(_, target)| target == src));
            if ready.is_empty() {
                return Err(crate::ir::Error::new(
                    "L2.MG.014",
                    "cyclic accumulator reduction",
                ));
            }
            for (src, dst) in ready {
                let d = depth.get(&src).copied().unwrap_or(0usize) + 1;
                let entry = depth.entry(dst).or_insert(0);
                *entry = (*entry).max(d);
            }
            pending = later;
        }
        depths.insert(key.clone(), depth);
    }
    for ((receiver, key), sources) in &mut out {
        let depth = &depths[key];
        sources.sort_by_key(|(source, target)| {
            edges[key]
                .iter()
                .filter(|(src, dst)| src.0 == *source && *dst == (*receiver, *target))
                .map(|(src, _)| depth.get(src).copied().unwrap_or(0))
                .max()
                .unwrap_or(0)
        });
    }
    Ok(out)
}

/// A ring to a waiting node: that node, and the memory accesses released to it.
type Released = (i64, Vec<Access>);

/// Cross-node order through the shared memory (L2.ORDER.009).
pub(super) fn order_nodes(built_by: &mut IndexMap<i64, Vec<Stage>>) -> Vec<serde_json::Value> {
    let mut edges = Vec::new();
    let mem: IndexMap<i64, Vec<Vec<Access>>> = built_by
        .iter()
        .map(|(n, b)| {
            (
                *n,
                b.iter()
                    .map(|s| {
                        s.accesses
                            .iter()
                            .filter(|a| a.key.0 == "mem" || a.key.0 == "bound_dram")
                            .cloned()
                            .collect()
                    })
                    .collect(),
            )
        })
        .collect();
    let effects: IndexMap<i64, Vec<_>> = mem
        .iter()
        .map(|(&node, stages)| {
            (
                node,
                stages
                    .iter()
                    .map(|accesses| sync::AccessIndex::new(accesses))
                    .collect(),
            )
        })
        .collect();
    let mut rings: HashMap<(i64, usize), Vec<Released>> = HashMap::new();
    let mut waits: HashMap<(i64, usize), Vec<i64>> = HashMap::new();
    for (&b, built) in built_by.iter() {
        let mut seen: HashMap<i64, i64> = HashMap::new();
        for (j, sj) in built.iter().enumerate() {
            for (&a, other) in built_by.iter() {
                let mut hit: Option<usize> = None;
                for (i, si) in other.iter().enumerate() {
                    if si.phase < 0 || sj.phase < 0 || si.phase >= sj.phase {
                        continue;
                    }
                    if !effects[&a][i]
                        .pending(&effects[&b][j], |_| false)
                        .is_empty()
                    {
                        hit = Some(i);
                    }
                }
                let Some(hit) = hit else { continue };
                if seen.get(&a).copied().unwrap_or(-1) >= hit as i64 {
                    continue;
                }
                seen.insert(a, hit as i64);
                let released = events::release_union(&mem[&a], hit);
                let regions: Vec<_> = released
                    .iter()
                    .flat_map(|x| {
                        mem[&b][j].iter().filter_map(move |y| {
                            if sync::clash(x, y) {
                                let ((xlo, xhi), (ylo, yhi)) = (x.range(), y.range());
                                Some(serde_json::json!({
                                    "allocation": x.key.1,
                                    "lo": xlo.max(ylo),
                                    "hi": xhi.min(yhi),
                                }))
                            } else {
                                None
                            }
                        })
                    })
                    .collect();
                edges.push(serde_json::json!({
                    "producer": a,
                    "consumer": b,
                    "producer_phase": other[hit].phase,
                    "consumer_phase": sj.phase,
                    "regions": regions,
                    "released_regions": released.iter().map(|x| serde_json::json!({
                        "allocation": x.key.1, "lo": x.lo, "hi": x.hi, "write": x.write,
                    })).collect::<Vec<_>>(),
                    "slot": null,
                }));
                if a == b
                    && !(built_by.len() == 1 && crate::mutant::on("l2.lower/handshake-always"))
                {
                    continue;
                }
                rings.entry((a, hit)).or_default().push((b, released));
                waits.entry((b, j)).or_default().push(a);
            }
        }
    }
    let keys: Vec<i64> = built_by.keys().copied().collect();
    for n in keys {
        let built = std::mem::take(built_by.get_mut(&n).unwrap());
        let mut out = Vec::new();
        for (j, s) in built.into_iter().enumerate() {
            if let Some(got) = waits.get(&(n, j)) {
                let lines = got
                    .iter()
                    .map(|a| format!("wait slot {} n=1 consume", events::symbol("order", *a, n)))
                    .collect();
                let mut f = Stage::new(lines, vec![], vec![]);
                f.fence = true;
                f.phase = s.phase;
                out.push(f);
            }
            let phase = s.phase;
            out.push(s);
            if let Some(to) = rings.get(&(n, j)) {
                let lines = to
                    .iter()
                    .map(|(b, _)| format!("ring mesh={b} slot={}", events::symbol("order", n, *b)))
                    .collect();
                let acc = to
                    .iter()
                    .flat_map(|(_, xs)| xs.iter())
                    .map(|x| {
                        let mut f = Access::new(FENCE, true, (&x.key.0, &x.key.1), x.lo, x.hi);
                        f.footprint = x.footprint.clone();
                        f
                    })
                    .collect();
                let mut f = Stage::new(lines, vec![], acc);
                f.fence = true;
                f.phase = phase;
                out.push(f);
            }
        }
        built_by.insert(n, out);
    }
    edges
}
