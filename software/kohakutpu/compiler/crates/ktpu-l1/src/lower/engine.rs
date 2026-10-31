//! Recover logical instruction sends from a bound dispatch-engine stream.

use std::collections::{BTreeMap, BTreeSet};

use ktpu_l0::engine::{ARG0, ARG1, ARG2, ARG3, DST, GO, NONE, REL, WAIT};
use ktpu_l0::format::{F_POSTED, MOVER_SKIP};
use ktpu_l0::{Facts, Op, Package, Step, Word, WordOps};

use crate::error::{Error, Result};

pub fn lift(pkg: &Package, f: &Facts) -> Result<Package> {
    let mut out = pkg.clone();
    out.steps.clear();
    let mut waited = BTreeMap::<u64, u64>::new();
    let mut mapping = BTreeMap::new();
    let mut targets = Vec::new();
    for (index, s) in pkg.steps.iter().enumerate() {
        mapping.insert(index as u64, out.steps.len() as u64);
        if s.op != Op::Engine {
            if s.op == Op::Jump || (s.op == Op::Patch && s.arg >> 24 & 1 != 0) {
                targets.push(out.steps.len());
            }
            out.steps.push(*s);
            continue;
        }
        let mut mb = BTreeMap::new();
        let mut relative = false;
        for i in 0..s.count {
            let w = pkg
                .payloads
                .get((s.arg + i / 3) as usize)
                .ok_or_else(|| Error::new("IMR.ENGINE", "entry outside payloads"))?;
            let code = w.get((i % 3) as u32 * 8, 8);
            let value = w.limb((i % 3 + 1) as u32 * 64);
            if code == NONE {
                continue;
            }
            let base = code & !REL;
            if base <= ARG3 {
                mb.insert(base, value);
                if base == ARG3 {
                    relative = code & REL != 0;
                }
            } else if code == GO {
                let dst = *mb
                    .get(&DST)
                    .ok_or_else(|| Error::new("IMR.ENGINE", "GO without destination"))?;
                let a3 = mb.get(&ARG3).copied().unwrap_or(0);
                if dst >> 24 & 1 != 0 {
                    let coord = mb.get(&ARG2).copied().unwrap_or(0) >> 40;
                    let unit = pkg
                        .units
                        .iter()
                        .position(|u| u.x == coord & 15 && u.y == coord >> 4 & 15)
                        .ok_or_else(|| Error::new("IMR.ENGINE", "fetch destination absent"))?;
                    let count = a3 & 255;
                    let at = a3 >> 24;
                    out.steps.push(Step::new(
                        if relative { Op::Dispatch } else { Op::Fetch },
                        unit as u64,
                        count,
                        if relative { at / 32 } else { at },
                        0,
                    ));
                } else {
                    let unit = pkg
                        .units
                        .iter()
                        .position(|u| u.x == dst & 255 && u.y == dst >> 8 & 255)
                        .ok_or_else(|| Error::new("IMR.ENGINE", "mailbox destination absent"))?;
                    let mut word = Word::ZERO;
                    for (j, reg) in [ARG0, ARG1, ARG2, ARG3].iter().enumerate() {
                        word |= Word::placed(mb.get(reg).copied().unwrap_or(0), j as u32 * 64);
                    }
                    let at = out.payloads.len();
                    out.payloads.push(word);
                    out.steps
                        .push(Step::new(Op::Dispatch, unit as u64, 1, at as u64, 0));
                }
            } else if code == WAIT {
                let src = value >> 56;
                let want = value & f.ctr_mask();
                let old = waited.entry(src).or_default();
                let count = want.saturating_sub(*old);
                *old = (*old).max(want);
                if count == 0 {
                    continue;
                }
                if src < pkg.units.len() as u64 {
                    out.steps.push(Step::new(Op::Await, src, count, 0, 0));
                } else if src == f.src_mover() {
                    out.steps.push(Step::new(Op::Mwait, 0, want, 0, 0));
                } else {
                    return Err(Error::new(
                        "IMR.ENGINE",
                        "non-unit engine wait needs explicit source semantics",
                    ));
                }
            } else if (8..24).contains(&code) {
                let at = out.payloads.len();
                out.payloads.push(
                    Word::placed((code - 8) * 8, 0)
                        | Word::placed(value, 64)
                        | Word::placed(MOVER_SKIP, 128),
                );
                out.steps
                    .push(Step::new(Op::Mover, 0, 1, at as u64, F_POSTED));
            } else if code == ktpu_l0::engine::il_code(ktpu_l0::engine::IL_RING) {
                out.steps.push(Step::new(
                    Op::Ring,
                    value & 3,
                    (value >> 8) & 255,
                    ((value >> 16) & 65535) | ((value >> 32) & 1) << 16,
                    0,
                ));
            } else {
                return Err(Error::new(
                    "IMR.ENGINE",
                    format!("unsupported control code {code}"),
                ));
            }
        }
    }
    mapping.insert(pkg.steps.len() as u64, out.steps.len() as u64);
    for i in targets {
        if crate::mutant::on("imr/engine-stale-target") {
            continue;
        }
        let s = &mut out.steps[i];
        if s.op == Op::Jump {
            s.unit = mapping[&s.unit];
        } else {
            s.count = mapping[&s.count];
        }
    }
    Ok(out)
}

/// Remove lowering's original payloads not reached by entries or relative fetches.
pub fn compact(pkg: &Package) -> Result<Package> {
    if crate::mutant::on("imr/dead-payload") {
        return Ok(pkg.clone());
    }
    if !pkg.steps.iter().any(|s| s.op == Op::Engine) {
        return Ok(pkg.clone());
    }
    let mut used = BTreeSet::new();
    let mut relative = Vec::new();
    for s in &pkg.steps {
        let count = match s.op {
            Op::Engine => s.count.div_ceil(3),
            Op::Dispatch | Op::Mover | Op::Store | Op::Ctrl => s.count,
            Op::Load | Op::Recv => 1,
            _ => 0,
        };
        used.extend(s.arg..s.arg + count);
        if s.op == Op::Send {
            used.extend(
                (s.arg & 0xffff_ffff)
                    ..(s.arg & 0xffff_ffff)
                        + if s.flags & ktpu_l0::format::F_MEM != 0 {
                            2
                        } else {
                            s.count
                        },
            );
        }
        if s.op != Op::Engine {
            continue;
        }
        for i in 0..s.count {
            let at = s.arg + i / 3;
            let slot = (i % 3) as u32;
            let word = pkg.payloads[at as usize];
            if word.get(slot * 8, 8) == ARG3 | REL {
                let value = word.limb((slot + 1) * 64);
                let first = (value >> 24) / 32;
                used.extend(first..first + (value & 255));
                relative.push((at, slot, first, value));
            }
        }
    }
    let mut out = pkg.clone();
    out.payloads.clear();
    let mut map = BTreeMap::new();
    for old in used {
        map.insert(old, out.payloads.len() as u64);
        out.payloads.push(
            *pkg.payloads
                .get(old as usize)
                .ok_or_else(|| Error::new("IMR.ENGINE", "relative fetch outside payloads"))?,
        );
    }
    for (at, slot, first, value) in relative {
        let new = ((map[&first] * 32) << 24) | (value & 0xffffff);
        out.payloads[map[&at] as usize].set((slot + 1) * 64, 64, new);
    }
    for s in &mut out.steps {
        match s.op {
            Op::Engine | Op::Dispatch | Op::Mover | Op::Store | Op::Ctrl | Op::Load | Op::Recv => {
                s.arg = map.get(&s.arg).copied().unwrap_or(0)
            }
            Op::Send => s.arg = (s.arg & !0xffff_ffff) | map[&(s.arg & 0xffff_ffff)],
            _ => {}
        }
    }
    Ok(out)
}
