//! Work across nodes: the same `par` on every node, accumulator adds into
//! another node (L2.MG.014) and cross-node order through shared memory
//! (L2.ORDER.009).

use super::*;

fn node_vars(stmts: &[Arc<Node>]) -> HashSet<String> {
    let mut out = HashSet::new();
    for s in stmts {
        if let Node::Par(p) = s.as_ref() {
            if p.kind == "node" {
                out.insert(p.var.clone());
            }
        }
        out.extend(node_vars(s.body()));
    }
    out
}

pub(super) type StageKey = (usize, Vec<(String, i64)>);

/// The same `par` run on every node: its statement and the loop values
/// around it, the node's own left out.
pub(super) fn stage_key(body: &Body, insts: &[Instance]) -> StageKey {
    let mut skip = node_vars(&body.stmts);
    skip.insert(insts[0].par().var.clone());
    let mut env: Vec<(String, i64)> = insts[0]
        .env
        .iter()
        .filter(|(k, _)| !skip.contains(*k))
        .map(|(k, v)| (k.clone(), if let Val::Int(i) = v { *i as i64 } else { 0 }))
        .collect();
    env.sort();
    (insts[0].par.uid(), env)
}

/// `{(target node, stage key): [(source node, target index)]}`.
type RemoteAdds = HashMap<(i64, StageKey), Vec<(i64, i64)>>;

/// The [`RemoteAdds`] of every add across nodes (L2.MG.014).
pub(super) fn remote_adds(body: &Body, stages: &[crate::ir::Stage]) -> Result<RemoteAdds> {
    let have: HashSet<(i64, StageKey)> = stages
        .iter()
        .filter_map(|s| match &s.x {
            StageX::Par(x) => Some((s.node, stage_key(body, x))),
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
            let key = (tgt, stage_key(body, x));
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
    Ok(out)
}

/// Signal slots of the cross-node handshakes: `ORDER + n` counts node n's
/// memory releases, `ADD + n` node n's opened accumulator tiles.
const ORDER: i64 = 0;

pub(super) fn add_slot(t: &Target) -> i64 {
    t.il_slots / 2
}

/// A ring to a waiting node: that node, and the memory accesses released to it.
type Released = (i64, Vec<Access>);

/// Cross-node order through the shared memory (L2.ORDER.009).
pub(super) fn order_nodes(built_by: &mut IndexMap<i64, Vec<Stage>>) {
    let mem: IndexMap<i64, Vec<Vec<Access>>> = built_by
        .iter()
        .map(|(n, b)| {
            (
                *n,
                b.iter()
                    .map(|s| {
                        s.accesses
                            .iter()
                            .filter(|a| a.key.0 == "mem")
                            .cloned()
                            .collect()
                    })
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
                // `l2.lower/handshake-always`: a node orders against itself
                // as against a peer reading its writes.
                if a == b
                    && !(built_by.len() == 1 && crate::mutant::on("l2.lower/handshake-always"))
                {
                    continue;
                }
                let mut hit: Option<usize> = None;
                for (i, si) in other.iter().enumerate() {
                    if si.phase < 0 || sj.phase < 0 || si.phase >= sj.phase {
                        continue;
                    }
                    if mem[&a][i]
                        .iter()
                        .any(|x| mem[&b][j].iter().any(|y| sync::clash(x, y)))
                    {
                        hit = Some(i);
                    }
                }
                let Some(hit) = hit else { continue };
                if seen.get(&a).copied().unwrap_or(-1) >= hit as i64 {
                    continue;
                }
                seen.insert(a, hit as i64);
                rings
                    .entry((a, hit))
                    .or_default()
                    .push((b, mem[&a][hit].clone()));
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
                    .map(|a| format!("wait slot {} n=1 consume", ORDER + a))
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
                    .map(|(b, _)| format!("ring mesh={b} slot={}", ORDER + n))
                    .collect();
                let acc = to
                    .iter()
                    .flat_map(|(_, xs)| xs.iter())
                    .map(|x| Access::new(FENCE, true, (&x.key.0, &x.key.1), x.lo, x.hi))
                    .collect();
                let mut f = Stage::new(lines, vec![], acc);
                f.fence = true;
                f.phase = phase;
                out.push(f);
            }
        }
        built_by.insert(n, out);
    }
}
