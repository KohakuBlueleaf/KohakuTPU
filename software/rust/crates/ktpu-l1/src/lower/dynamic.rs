//! Fixed-placement residency for runtime control flow and payload patches.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use ktpu_isa::unit::vector::OP_IMEM;
use ktpu_l0::format::{Op, Step, F_MEM};
use ktpu_l0::{Package, Word, WordOps};

use super::imem::Cores;
use crate::error::{Error, Result};

type Queues = BTreeMap<u64, VecDeque<bool>>;

/// Runtime paths share only immutable physical IMEM cells; mutable cells stay inline.
pub fn package(pkg: &Package, cores: &mut Cores) -> Result<Package> {
    let shift = cores.isa.cfg.payload_bits - 4;
    let mut opaque = BTreeSet::new();
    let mut patched: BTreeSet<_> = pkg
        .steps
        .iter()
        .filter(|s| s.op == Op::Patch && s.arg >> 24 & 1 == 0)
        .map(|s| s.count as usize)
        .collect();
    for s in &pkg.steps {
        if s.op == Op::Patch && s.arg >> 24 & 1 == 0 {
            patched.insert(s.count as usize);
        }
        if s.op == Op::Engine {
            opaque.extend(0..pkg.units.len() as u64);
        }
        if s.op == Op::Repeat && vc(pkg, s.unit) {
            let first = (s.arg & 0xffff_ffff) as usize;
            for k in 0..s.arg >> 48 {
                let record_at = first + s.count as usize + (k / 2) as usize;
                if pkg.steps.iter().any(|p| {
                    p.op == Op::Patch && p.arg >> 24 & 1 == 0 && p.count as usize == record_at && {
                        let bit = p.arg & 255;
                        let end = bit + ((p.arg >> 8) & 255);
                        bit < 64 || (bit < 192 && end > 128)
                    }
                }) {
                    opaque.insert(s.unit);
                }
                let record = pkg.payloads[record_at];
                let h = record.limb((k % 2) as u32 * 128);
                let word = pkg.payloads[first + (h & 0xffff_ffff) as usize];
                if word.get(shift, 4) == OP_IMEM {
                    opaque.insert(s.unit);
                }
            }
        }
    }
    for s in &pkg.steps {
        if !known_send(s) || !vc(pkg, s.unit) {
            continue;
        }
        let first = if matches!(s.op, Op::Send | Op::Repeat) {
            s.arg & 0xffff_ffff
        } else {
            s.arg
        };
        for i in first as usize..(first + s.count) as usize {
            let w = *pkg
                .payloads
                .get(i)
                .ok_or_else(|| Error::new("IMR.DYNAMIC", "dispatch outside payloads"))?;
            if patched.contains(&i) {
                for patch in pkg
                    .steps
                    .iter()
                    .filter(|p| p.op == Op::Patch && p.arg >> 24 & 1 == 0 && p.count as usize == i)
                {
                    let bit = patch.arg & 255;
                    let width = (patch.arg >> 8) & 255;
                    if (w.get(shift, 4) == OP_IMEM && bit + width > 32)
                        || bit + width > shift as u64
                    {
                        opaque.insert(s.unit);
                    }
                }
            }
        }
    }
    // Runtime patches of a dispatch's extent must not change the stream topology.
    for s in &pkg.steps {
        if s.op == Op::Patch && s.arg >> 24 & 1 != 0 {
            if s.arg & 255 < 32 {
                opaque.extend(0..pkg.units.len() as u64);
            }
            if let Some(target) = pkg.steps.get(s.count as usize) {
                if matches!(target.op, Op::Dispatch | Op::Repeat | Op::Send) {
                    opaque.insert(target.unit);
                }
            }
        }
        if s.op == Op::Send && s.arg >> 32 == 5 && s.flags & F_MEM != 0 {
            opaque.insert(s.unit);
        }
    }
    let (keep, final_memory) = super::known::analyze(pkg, cores, &opaque, &patched)?;
    let counts = await_counts(pkg, &keep, &opaque)?;
    let mut out = pkg.clone();
    let mut prelude = Vec::new();
    let mut aliases: BTreeMap<u64, Vec<u64>> = BTreeMap::new();
    let mut repeats = BTreeMap::new();
    for (&i, flags) in &keep {
        let s = pkg.steps[i];
        if s.op != Op::Repeat || flags.iter().all(|&v| v) {
            continue;
        }
        let first = s.arg & 0xffff_ffff;
        let start = out.payloads.len() as u64;
        let mut positions = BTreeMap::new();
        for (j, &kept) in flags.iter().enumerate() {
            if kept {
                positions.insert(j as u64, out.payloads.len() as u64 - start);
                aliases
                    .entry(first + j as u64)
                    .or_default()
                    .push(out.payloads.len() as u64);
                out.payloads.push(pkg.payloads[first as usize + j]);
            }
        }
        let count = out.payloads.len() as u64 - start;
        let mut increments = Vec::new();
        for k in 0..s.arg >> 48 {
            let w = pkg.payloads[(first + s.count + k / 2) as usize];
            let at = (k % 2) as u32 * 128;
            let h = w.limb(at);
            if let Some(&index) = positions.get(&(h & 0xffff_ffff)) {
                increments.push(((h & !0xffff_ffff) | index, w.limb(at + 64)));
            }
        }
        for (group_index, group) in increments.chunks(2).enumerate() {
            aliases
                .entry(first + s.count + group_index as u64)
                .or_default()
                .push(out.payloads.len() as u64);
            let mut word = Word::ZERO;
            for (j, &(h, v)) in group.iter().enumerate() {
                word |= Word::placed(h, j as u32 * 128) | Word::placed(v, j as u32 * 128 + 64);
            }
            out.payloads.push(word);
        }
        let arg = start | (s.arg & (65535 << 32)) | (increments.len() as u64) << 48;
        repeats.insert(
            i,
            if count == 0 {
                Step::bare(Op::Settle)
            } else {
                Step::new(Op::Repeat, s.unit, count, arg, s.flags)
            },
        );
    }
    let mut step_map = BTreeMap::new();
    let mut targets = Vec::new();
    for (i, original) in pkg.steps.iter().enumerate() {
        step_map.insert(i as u64, prelude.len() as u64);
        let start = prelude.len();
        let mut s = *original;
        if known_send(&s) && opaque.contains(&s.unit) {
            prelude.push(s);
        } else if s.op == Op::Repeat {
            prelude.push(repeats.get(&i).copied().unwrap_or(s));
        } else if let Some(flags) = keep.get(&i) {
            let mut j = 0;
            while j < flags.len() {
                if !flags[j] {
                    j += 1;
                    continue;
                }
                let first = j;
                while j < flags.len() && flags[j] {
                    j += 1;
                }
                prelude.push(Step::new(
                    s.op,
                    s.unit,
                    (j - first) as u64,
                    s.arg + first as u64,
                    s.flags,
                ));
            }
        } else {
            if let Some(&count) = counts.get(&i) {
                s.count = count;
                if count == 0 {
                    s = Step::new(Op::Settle, 0, 0, 0, 0);
                }
            }
            if s.op == Op::Jump || (s.op == Op::Patch && s.arg >> 24 & 1 != 0) {
                targets.push(prelude.len());
            }
            prelude.push(s);
            if s.op == Op::Patch && s.arg >> 24 & 1 == 0 {
                if let Some(copies) = aliases
                    .get(&s.count)
                    .filter(|_| !crate::mutant::on("imr/forget-patch-alias"))
                {
                    for &copy in copies {
                        prelude.push(Step { count: copy, ..s });
                    }
                }
            }
        }
        if prelude.len() == start {
            prelude.push(Step::new(Op::Settle, 0, 0, 0, 0));
        }
    }
    step_map.insert(pkg.steps.len() as u64, prelude.len() as u64);
    for i in targets {
        let s = &mut prelude[i];
        if s.op == Op::Jump {
            s.unit = step_map[&s.unit];
        } else {
            s.count = step_map[&s.count];
        }
    }
    out.steps = prelude;
    compact(&mut out, pkg)?;
    // Only cells equal on every reachable exit remain predicted.
    cores.cores.clear();
    for ((unit, addr), value) in final_memory {
        let u = pkg.units[unit as usize];
        cores
            .cores
            .entry((u.x as u32, u.y as u32))
            .or_insert_with(|| super::imem::Resident::new(cores.words, cores.run_reach))
            .mem
            .insert(addr, value);
    }
    Ok(out)
}

fn known_send(s: &Step) -> bool {
    matches!(s.op, Op::Dispatch | Op::Repeat)
        || (s.op == Op::Send && s.arg >> 32 == 5 && s.flags & F_MEM == 0)
}

fn vc(pkg: &Package, unit: u64) -> bool {
    pkg.units
        .get(unit as usize)
        .is_some_and(|u| u.type_code == 0x5643)
}

/// Completion queues meet exactly at CFG joins, including loop backedges.
fn await_counts(
    pkg: &Package,
    keep: &BTreeMap<usize, Vec<bool>>,
    opaque: &BTreeSet<u64>,
) -> Result<BTreeMap<usize, u64>> {
    let mut incoming = BTreeMap::from([(0usize, Queues::new())]);
    let mut todo = VecDeque::from([0usize]);
    let mut counts = BTreeMap::new();
    while let Some(i) = todo.pop_front() {
        let Some(s) = pkg.steps.get(i) else {
            continue;
        };
        let mut queues = incoming[&i].clone();
        if opaque.contains(&s.unit) {
            // Runtime counts on this core remain exactly as in the input.
        } else if s.op == Op::Dispatch && vc(pkg, s.unit) {
            queues
                .entry(s.unit)
                .or_default()
                .extend(keep[&i].iter().copied());
        } else if s.op == Op::Repeat && vc(pkg, s.unit) {
            for _ in 0..(s.arg >> 32) & 65535 {
                queues
                    .entry(s.unit)
                    .or_default()
                    .extend(keep[&i].iter().copied());
            }
        } else if s.op == Op::Fetch && vc(pkg, s.unit) {
            queues
                .entry(s.unit)
                .or_default()
                .extend(std::iter::repeat_n(true, s.count as usize));
        } else if s.op == Op::Await && s.flags == ktpu_l0::format::F_IMM {
            // The runtime sent frontier already includes rewritten dispatch lengths.
            // Preserve this step; it is not a static zero-count no-op.
            queues.remove(&s.unit);
        } else if s.op == Op::Await && vc(pkg, s.unit) {
            let q = queues.entry(s.unit).or_default();
            let mut count = 0;
            for _ in 0..s.count {
                count += q
                    .pop_front()
                    .ok_or_else(|| Error::new("IMR.CFG", "AWAIT has no dominating sends"))?
                    as u64;
            }
            counts.insert(i, count);
        }
        queues.retain(|_, q| !q.is_empty());
        let successors = if s.op == Op::End {
            vec![]
        } else if s.op == Op::Jump {
            vec![i + 1, s.unit as usize]
        } else {
            vec![i + 1]
        };
        for next in successors {
            match incoming.get(&next) {
                Some(old) if *old != queues => {
                    return Err(Error::new(
                        "IMR.CFG",
                        "completion queues disagree at control-flow join",
                    ))
                }
                Some(_) => {}
                None => {
                    incoming.insert(next, queues.clone());
                    todo.push_back(next);
                }
            }
        }
    }
    Ok(counts)
}

/// Preserve aliases of patched words while removing all unreferenced payloads.
fn compact(pkg: &mut Package, original: &Package) -> Result<()> {
    let mut used = BTreeSet::new();
    for s in &pkg.steps {
        let (first, count) = match s.op {
            Op::Dispatch | Op::Mover | Op::Store | Op::Ctrl => (s.arg, s.count),
            Op::Load | Op::Recv => (s.arg, 1),
            Op::Repeat => (s.arg & 0xffff_ffff, s.count + (s.arg >> 48).div_ceil(2)),
            Op::Send => (
                s.arg & 0xffff_ffff,
                if s.flags & F_MEM != 0 { 2 } else { s.count },
            ),

            _ => continue,
        };
        used.extend(first..first + count);
    }
    if original
        .steps
        .iter()
        .any(|s| s.op == Op::Patch && s.arg >> 24 & 1 != 0)
    {
        // Runtime payload indices/counts may reach any original word.
        used.extend(0..original.payloads.len() as u64);
    }
    for s in &mut pkg.steps {
        if s.op == Op::Patch && s.arg >> 24 & 1 == 0 && !used.contains(&s.count) {
            *s = Step::bare(Op::Settle);
        }
    }
    let mut map = BTreeMap::new();
    let mut words: Vec<Word> = Vec::new();
    for old in used {
        map.insert(old, words.len() as u64);
        words.push(
            *pkg.payloads
                .get(old as usize)
                .ok_or_else(|| Error::new("IMR.PAYLOAD", "dynamic payload outside storage"))?,
        );
    }
    for s in &mut pkg.steps {
        match s.op {
            Op::Dispatch | Op::Mover | Op::Store | Op::Ctrl | Op::Load | Op::Recv => {
                s.arg = map.get(&s.arg).copied().unwrap_or(0);
            }
            Op::Repeat => {
                s.arg = (s.arg & !0xffff_ffff) | map[&(s.arg & 0xffff_ffff)];
            }
            Op::Send => {
                s.arg = (s.arg & !0xffff_ffff) | map[&(s.arg & 0xffff_ffff)];
            }
            Op::Patch if s.arg >> 24 & 1 == 0 => {
                s.count = map[&s.count];
            }
            _ => {}
        }
    }
    pkg.payloads = words;
    Ok(())
}
