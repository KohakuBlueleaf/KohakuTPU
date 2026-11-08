//! Counted raw-word DMA streams over disjoint destination word spans.

use super::*;
use crate::lower::sync::Stage;

pub(super) fn candidates(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    images: &mut Vec<String>,
) -> Result<Vec<(Stage, Vec<String>)>> {
    let queued = match op.st.kw("queued_copy") {
        None => None,
        Some(value) => match const_of(body, &op.st, value, env)? {
            0 => Some(false),
            1 => Some(true),
            _ => return Err(fail(body, &op.st, "L2.MOV.015", "queued_copy is 0 or 1")),
        },
    };
    let tag = images.len();
    let mut candidates = Vec::new();
    for policy in [false, true] {
        if queued.is_some_and(|q| q != policy) {
            continue;
        }
        if let Some(stage) = emit(body, op, env, tg, images, policy)? {
            candidates.push((stage, images.split_off(tag)));
        }
    }
    Ok(candidates)
}

fn emit(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    images: &mut Vec<String>,
    queued: bool,
) -> Result<Option<Stage>> {
    let ((dn, doff, dl), (sn, soff, sl)) = copy_walks(body, &op.st, env, tg)?;
    let dest = merge(&squeeze(&dl));
    let line = i64::from(tg.machine.card.dram.line_bytes);
    if sn == dn || dest.len() != 1 || dest[0].1 != tg.word_bytes || doff % line != 0 {
        return Ok(None);
    }
    let limit = (tg.l1_words / 2).min(i64::from(tg.machine.vc.walk_max));
    let mut dims = tg.clone();
    dims.levels = tg.machine.vc.descriptor_dims as usize;
    let pieces = super::lone::held(cut(&[dl.clone(), sl.clone()], limit, None, 0, &dims));
    if pieces.iter().any(|(offs, walks)| {
        let d = merge(&squeeze(&walks[0]));
        d.len() != 1
            || d[0].1 != tg.word_bytes
            || (doff + offs[0]) % line != 0
            || count(&walks[0]) * tg.word_bytes % line != 0
    }) {
        return Ok(None);
    }
    let cores = tg.vc.len().min(pieces.len());
    let units: Vec<_> = (0..cores).map(|v| format!("vc[{v}]")).collect();
    let mut lines = Vec::new();
    let mut reused = std::collections::BTreeMap::<([i64; 2], bool, bool), String>::new();
    for (v, unit) in units.iter().enumerate() {
        let mut at = pieces.len() * v / cores;
        let end = pieces.len() * (v + 1) / cores;
        while at < end {
            let (offsets, walks) = &pieces[at];
            let mut next = at + 1;
            let mut step = [0, 0];
            if next < end && pieces[next].1 == *walks {
                step = [
                    pieces[next].0[0] - offsets[0],
                    pieces[next].0[1] - offsets[1],
                ];
                if step.iter().all(|s| -tg.step() <= *s && *s < tg.step()) {
                    while next < end
                        && pieces[next].1 == *walks
                        && (0..2).all(|axis| {
                            pieces[next].0[axis] == offsets[axis] + (next - at) as i64 * step[axis]
                        })
                    {
                        next += 1;
                    }
                } else {
                    step = [0, 0];
                }
            }
            let run = next - at;
            let paired = run >= 2;
            let trips = if paired { run / 2 } else { 1 };
            let consumed = if paired { trips * 2 } else { 1 };
            let steady = trips > 1;
            let key = (step, paired, steady);
            let name = if let Some(name) = reused.get(&key) {
                name.clone()
            } else {
                let name = format!("{}.word_stream_{}", body.name, images.len());
                let mut code = vec![format!("image {name}(trips)")];
                code.extend([
                    format!("    ainc a0 += {}", step[1]),
                    format!("    ainc a1 += {}", step[0]),
                    "    vfill a0 -> l1[0] rel".into(),
                ]);
                if paired {
                    if steady {
                        code.extend(["    seti s0 = trips - 1".into(), "    loop s0".into()]);
                        for (src, dst) in [(limit, 0), (0, limit)] {
                            if queued {
                                code.extend([
                                    "        vsync fill on=drain".into(),
                                    format!("        vfill a0 -> l1[{src}] rel"),
                                    "        vsync drain on=fill slack=1".into(),
                                    format!("        vdrain a1 <- l1[{dst}] rel"),
                                ]);
                            } else {
                                code.extend([
                                    "        vsync fe on=fill".into(),
                                    "        vsync fe on=drain".into(),
                                    format!("        vfill a0 -> l1[{src}] rel"),
                                    format!("        vdrain a1 <- l1[{dst}] rel"),
                                ]);
                            }
                        }
                    }
                    if queued {
                        code.extend([
                            "    vsync fill on=drain".into(),
                            format!("    vfill a0 -> l1[{limit}] rel"),
                            "    vsync drain on=fill slack=1".into(),
                        ]);
                    } else {
                        code.extend([
                            "    vsync fe on=fill".into(),
                            "    vsync fe on=drain".into(),
                            format!("    vfill a0 -> l1[{limit}] rel"),
                        ]);
                    }
                    code.extend([
                        "    vdrain a1 <- l1[0] rel".into(),
                        "    vsync drain on=fill".into(),
                        format!("    vdrain a1 <- l1[{limit}] rel"),
                    ]);
                } else {
                    code.extend([
                        "    vsync drain on=fill".into(),
                        "    vdrain a1 <- l1[0] rel".into(),
                    ]);
                }
                code.extend(["    vsync fe on=drain".into(), "    halt".into()]);
                images.push(code.join("\n"));
                reused.insert(key, name.clone());
                name
            };
            lines.extend([
                format!("send {unit}"),
                format!(
                    "    desc a0 = {sn} + {} walk={}",
                    soff + offsets[1],
                    super::lone::desc_walk(&walks[1])
                ),
                format!(
                    "    desc a1 = {dn} + {} walk={}",
                    doff + offsets[0],
                    super::lone::desc_walk(&walks[0])
                ),
                format!("    run {name}({trips})"),
            ]);
            at += consumed;
        }
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
        accesses.push(Access::new(unit, true, (unit, "l1"), 0, 2 * limit));
    }
    Ok(Some(Stage::new(lines, units, accesses)))
}
