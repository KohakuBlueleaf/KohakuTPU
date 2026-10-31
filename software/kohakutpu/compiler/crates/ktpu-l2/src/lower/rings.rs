//! Streams into a receiver that takes inside its loop: their rings, the
//! core's opening credits and the sender's closing spend (L2.LOW.006, .014).

use super::*;

/// `{(src, dst, side): ring size}` of every pair whose receiver takes inside
/// a loop of its instance: a stream (L2.LOW.006).
pub(super) fn streams(
    body: &Body,
    stages: &[&crate::ir::Stage],
    t: &Target,
) -> Result<IndexMap<Key, i64>> {
    let mut out: IndexMap<Key, i64> = IndexMap::new();
    for s in stages {
        let StageX::Par(x) = &s.x else { continue };
        for inst in x {
            let Some(&first) = inst.cuts.first() else {
                continue;
            };
            for (i, (n, env)) in inst.items.iter().enumerate() {
                let Node::Op(op) = n.as_ref() else { continue };
                if op.op != "recv" || i < first {
                    continue;
                }
                let src = value(body, &op.st, op.st.positional()[0], env)?;
                let Term::View(sn, sl) = &src else { continue };
                if sl.len() != 1 {
                    continue;
                }
                let me = format!("{}[{}]", inst.kind, inst.index.unwrap_or(0));
                let ty = op.ty.as_ref().unwrap();
                let side = if inst.kind == "vc" {
                    "l1".to_string()
                } else {
                    ty.space.clone()
                };
                let key = (
                    format!("{sn}[{}]", sl[0].as_int().unwrap_or(0)),
                    me,
                    side.clone(),
                );
                let size = if side == "l1" {
                    let size = 2 * ty.shape[0] * ty.shape[1] / t.f16_word();
                    let pool = t.pool();
                    if size > pool {
                        return Err(body.reader.fail(
                            &op.st,
                            "L2.LOW.006",
                            format!("two of these tiles exceed {pool} words"),
                        ));
                    }
                    size
                } else {
                    let size = 2 * t.entries(ty.shape[0], ty.shape[1]);
                    if size > t.ring_entries() {
                        return Err(body.reader.fail(
                            &op.st,
                            "L2.LOW.006",
                            "two of these tiles exceed a side's banks",
                        ));
                    }
                    size
                };
                out.entry(key).or_insert(size);
            }
        }
    }
    Ok(out)
}

/// The largest credit count one POST / VPOST / VWAIT word carries.
const CREDIT_WORD: i64 = ktpu_isa::unit::CREDIT_WORD as i64;

pub(super) fn counts(total: i64) -> Vec<i64> {
    (0..total)
        .step_by(CREDIT_WORD as usize)
        .map(|k| CREDIT_WORD.min(total - k))
        .collect()
}

/// The `credit` drains spending a credit word of `c` credits: one, of
/// exactly `c` (L2.LOW.014).
fn spend_drains(c: i64) -> Vec<i64> {
    if on("l2.lower/spend-split-128") {
        return (0..c).step_by(128).map(|_| c.min(128)).collect();
    }
    vec![c]
}

fn bare(unit: &str) -> String {
    unit.replace(['[', ']'], "")
}

/// Each stream into a core: the core hands its whole ring to the sender.
pub(super) fn open(name: &str, node: i64, links: &Links, images: &mut Vec<String>) -> Stage {
    let (mut lines, mut units) = (Vec::new(), Vec::new());
    let mut posts: IndexMap<String, Vec<&links::Ring>> = IndexMap::new();
    for r in links.rings.values() {
        if r.dst.starts_with("vc") {
            posts.entry(r.dst.clone()).or_default().push(r);
        }
    }
    for (dst, rings) in posts {
        let img = format!("{name}_n{node}_open_{}", bare(&dst));
        let mut words = vec![format!("image {img}()")];
        for r in rings {
            for c in counts(r.granules()) {
                words.push(format!("    vpost {} n={c}", r.src));
            }
        }
        words.push("    halt".into());
        images.push(words.join("\n") + "\n");
        lines.push(format!("send {dst}"));
        lines.push(format!("{IND}run {img}()"));
        units.push(dst);
    }
    Stage::new(lines, units, vec![])
}

/// Each stream into a core: the sending cluster spends the credits of the
/// ring's last lap by drains into the ring.
pub(super) fn spend(links: &Links) -> Stage {
    let (mut lines, mut units) = (Vec::new(), Vec::new());
    let mut spends: IndexMap<String, Vec<&links::Ring>> = IndexMap::new();
    for r in links.rings.values() {
        if r.dst.starts_with("vc") {
            spends.entry(r.src.clone()).or_default().push(r);
        }
    }
    for (src, rings) in spends {
        lines.push(format!("send {src}"));
        for r in rings {
            for c in counts(r.granules()) {
                for n in spend_drains(c) {
                    lines.push(format!("{IND}drain n={n} to={} l1={} credit", r.dst, r.lo));
                }
            }
        }
        units.push(src);
    }
    Stage::new(lines, units, vec![])
}
