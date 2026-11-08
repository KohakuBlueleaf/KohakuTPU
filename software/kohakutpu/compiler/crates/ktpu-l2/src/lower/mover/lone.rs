//! Silicon flaw F-001 (`docs/spec/stack/flaws.md`): the mover writes a word
//! before its data FIFO shows it when a one-beat write burst chains off the
//! last beat of the burst before. A copy some mover command of which is not
//! proven free of such bursts (`burst::burst_safe`) runs on the vector cores
//! on a machine with the flaw (L2.MOV.020): each piece fills its source words
//! into L1 and drains them along the destination's walk.

use super::*;
use crate::lower::sync::{clash, Stage};
use ktpu_isa::unit::vector::DIM_STRIDE_MAX;

/// Whether copy `op`, on a machine with F-001 and vector cores, has a mover
/// command not proven free of a chained one-beat write burst; a die without
/// vector cores keeps the copy on the mover (its exposure).
pub fn lone_copy(body: &Body, op: &Op, env: &Env, tg: &Target) -> Result<bool> {
    if op.op != "copy"
        || !tg.mover_lead
        || tg.vc.is_empty()
        || tg.descriptors < 2
        || on("l2.mover/lone-on-mover")
    {
        return Ok(false);
    }
    let ((_, doff, dl), (_, _, sl)) = copy_walks(body, &op.st, env, tg)?;
    let line = i64::from(tg.machine.card.dram.line_bytes);
    let cap = i64::from(tg.machine.mover.burst_max);
    Ok(copy_cuts(&sl, &dl, doff, tg)
        .iter()
        .any(|(o, l)| !burst_safe(doff + o[1], &l[1], tg.word_bytes, line, cap)))
}

/// A raw-word VC copy competes with a safe mover schedule by complete price.
/// Safety-required copies keep their VC schedule regardless of its price.
pub fn chosen_copy(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    images: &mut Vec<String>,
) -> Result<Option<Stage>> {
    let required = lone_copy(body, op, env, tg)?;
    if !required
        && (op.op != "copy"
            || op.st.kw("node_loop").is_some()
            || op.st.kw("classed").is_some()
            || tg.vc.is_empty()
            || tg.descriptors < 2
            || tg.l1_words < 2)
    {
        return Ok(None);
    }
    let streamed = match op.st.kw("stream_copy") {
        None => None,
        Some(value) => match const_of(body, &op.st, value, env)? {
            0 => Some(false),
            1 => Some(true),
            _ => return Err(fail(body, &op.st, "L2.MOV.015", "stream_copy is 0 or 1")),
        },
    };
    let tag = images.len();
    let vector = vector_copy(body, op, env, tg, images)?;
    let mut candidates = vec![(vector, images.split_off(tag))];
    if streamed != Some(false) {
        let streams = super::stream::candidates(body, op, env, tg, images)?;
        if !streams.is_empty() {
            if streamed == Some(true) {
                return super::choose::select(streams, images, tg).map(Some);
            }
            candidates.extend(streams);
        } else if streamed == Some(true) {
            return Err(fail(
                body,
                &op.st,
                "L2.MOV.015",
                "stream_copy needs independent complete destination lines",
            ));
        }
    }
    if required {
        return super::choose::select(candidates, images, tg).map(Some);
    }
    let (copies, accesses) = lower(body, op, env, tg)?;
    let mut lines = vec!["move local=1".into()];
    lines.extend(copies.iter().map(|line| format!("    {line}")));
    let mover = Stage::new(lines, vec![], accesses);
    let mut price = super::choose::price(&mover, &[], tg)?;
    let mut chosen = None;
    for (stage, code) in candidates {
        let cost = super::choose::price(&stage, &code, tg)?;
        if cost < price {
            price = cost;
            chosen = Some((stage, code));
        }
    }
    if let Some((stage, code)) = chosen {
        images.extend(code);
        Ok(Some(stage))
    } else {
        Ok(None)
    }
}

/// `pieces` with every level whose stride a descriptor cannot hold taken
/// out as separate pieces, one a step.
pub(super) fn held(pieces: Vec<(Vec<i64>, Vec<Levels>)>) -> Vec<(Vec<i64>, Vec<Levels>)> {
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
pub(super) fn desc_walk(levels: &[(i64, i64)]) -> String {
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
    let mut pending: Vec<Access> = Vec::new();
    for (i, (offs, walks)) in pieces.iter().enumerate() {
        let unit = &units[i % cores];
        let mut accesses = [
            mem_of(
                false,
                span_of(&sn, soff + offs[1], &walks[1], tg.word_bytes),
            ),
            mem_of(true, span_of(&dn, doff + offs[0], &walks[0], tg.word_bytes)),
        ];
        for a in &mut accesses {
            a.unit = unit.clone();
        }
        for prior in &units {
            let conflict = pending.iter().filter(|a| &a.unit == prior).any(|a| {
                accesses
                    .iter()
                    .any(|b| (a.unit != b.unit || (a.write && !b.write)) && clash(a, b))
            });
            if conflict {
                lines.push(format!("wait {prior}"));
                pending.retain(|a| &a.unit != prior);
            }
        }
        pending.extend(accesses);
        lines.extend([
            format!("send {unit}"),
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
