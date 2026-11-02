//! Silicon flaw F-001 (`docs/spec/stack/flaws.md`): the mover writes a word
//! before its data FIFO shows it when a one-beat write burst chains off the
//! last beat of the burst before. A copy whose write side steps one-word runs
//! makes such bursts, so on a machine with the flaw it runs on the vector
//! cores instead (L2.MOV.020): each piece fills its source words into L1 and
//! drains them along the destination's walk.

use super::*;
use crate::lower::sync::Stage;
use ktpu_isa::unit::vector::DIM_STRIDE_MAX;

/// Whether copy `op` writes one-word runs on a machine with F-001 and vector
/// cores; a die without them keeps the copy on the mover (its exposure).
pub fn lone_copy(body: &Body, op: &Op, env: &Env, tg: &Target) -> Result<bool> {
    if op.op != "copy"
        || !tg.mover_lead
        || tg.vc.is_empty()
        || tg.descriptors < 2
        || on("l2.mover/lone-on-mover")
    {
        return Ok(false);
    }
    let ((_, _, dl), _) = copy_walks(body, &op.st, env, tg)?;
    Ok(count(&dl) > 1 && run_words(&dl, tg.word_bytes) == 1)
}

/// `pieces` with every level whose stride a descriptor cannot hold taken
/// out as separate pieces, one a step.
fn held(pieces: Vec<(Vec<i64>, Vec<Levels>)>) -> Vec<(Vec<i64>, Vec<Levels>)> {
    let mut out = Vec::new();
    let mut todo = pieces;
    while let Some((offs, walks)) = todo.pop() {
        let wide = (0..walks[0].len()).find(|&i| {
            walks
                .iter()
                .any(|w| w[i].0 > 1 && w[i].1.abs() > DIM_STRIDE_MAX)
        });
        let Some(i) = wide else {
            out.push((offs, walks));
            continue;
        };
        for k in 0..walks[0][i].0 {
            let o = offs
                .iter()
                .zip(&walks)
                .map(|(o, w)| o + k * w[i].1)
                .collect();
            let w = walks
                .iter()
                .map(|w| {
                    let mut w = w.clone();
                    w[i].0 = 1;
                    w
                })
                .collect();
            todo.push((o, w));
        }
    }
    out.reverse();
    out
}

/// A descriptor's `walk=`: (stride, count) innermost first.
fn desc_walk(levels: &[(i64, i64)]) -> String {
    let lv: Vec<String> = merge(&squeeze(levels))
        .iter()
        .rev()
        .map(|(n, s)| format!("({s}, {n})"))
        .collect();
    match lv.len() {
        1 => lv[0].clone(),
        _ => format!("({})", lv.join(", ")),
    }
}

/// Copy `op` on the vector cores: pieces of at most half a core's L1 and a
/// descriptor's words, dealt round the cores in order.
pub fn vector_copy(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    images: &mut Vec<String>,
) -> Result<Stage> {
    let ((dn, doff, dl), (sn, soff, sl)) = copy_walks(body, &op.st, env, tg)?;
    let vc = &tg.machine.vc;
    let limit = (tg.l1_words / 2).min(vc.walk_max as i64);
    let mut dims = tg.clone();
    dims.levels = vc.descriptor_dims as usize;
    let pieces = held(cut(&[dl.clone(), sl.clone()], limit, None, 0, &dims));
    let name = format!("{}.lone_{}", body.name, images.len());
    images.push(format!(
        "image {name}()\n    vfill a0 -> l1[0]\n    vsync drain on=fill\n    vdrain a1 <- l1[0]\n    vsync fe on=drain\n    halt"
    ));
    let cores = tg.vc.len().min(pieces.len());
    let units: Vec<String> = (0..cores).map(|v| format!("vc[{v}]")).collect();
    let mut lines = Vec::new();
    for (i, (offs, walks)) in pieces.iter().enumerate() {
        lines.extend([
            format!("send {}", units[i % cores]),
            format!(
                "    desc a0 = {sn} + {} walk={}",
                soff + offs[1],
                desc_walk(&walks[1])
            ),
            format!(
                "    desc a1 = {dn} + {} walk={}",
                doff + offs[0],
                desc_walk(&walks[0])
            ),
            format!("    run {name}()"),
        ]);
    }
    lines.extend(units.iter().map(|u| format!("wait {u}")));
    let mut accesses = Vec::new();
    for unit in &units {
        for mut a in [
            mem_of(false, span_of(&sn, soff, &sl, tg.word_bytes)),
            mem_of(true, span_of(&dn, doff, &dl, tg.word_bytes)),
        ] {
            a.unit = unit.clone();
            accesses.push(a);
        }
        accesses.push(Access::new(unit, true, (unit, "l1"), 0, limit));
    }
    Ok(Stage::new(lines, units, accesses))
}
