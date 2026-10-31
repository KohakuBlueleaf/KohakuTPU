//! Must-know physical instruction cells at control-flow joins.
use super::imem::Cores;
use crate::error::{Error, Result};
use ktpu_isa::unit::vector::OP_IMEM;
use ktpu_l0::format::F_MEM;
use ktpu_l0::{Op, Package, Step};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

type Memory = BTreeMap<(u64, u32), u64>;
pub type Masks = BTreeMap<usize, Vec<bool>>;

fn send(s: &Step) -> bool {
    matches!(s.op, Op::Dispatch | Op::Repeat)
        || (s.op == Op::Send && s.arg >> 32 == 5 && s.flags & F_MEM == 0)
}
fn first(s: &Step) -> usize {
    (if matches!(s.op, Op::Send | Op::Repeat) {
        s.arg & 0xffff_ffff
    } else {
        s.arg
    }) as usize
}

pub fn analyze(
    pkg: &Package,
    cores: &Cores,
    opaque: &BTreeSet<u64>,
    patched: &BTreeSet<usize>,
) -> Result<(Masks, Memory)> {
    let mut initial = Memory::new();
    for (i, u) in pkg.units.iter().enumerate() {
        if let Some(r) = cores.cores.get(&(u.x as u32, u.y as u32)) {
            for (&a, &w) in &r.mem {
                initial.insert((i as u64, a), w);
            }
        }
    }
    initial.retain(|(u, _), _| !opaque.contains(u));
    let shift = cores.isa.cfg.payload_bits - 4;
    let transfer = |i: usize, mem: &mut Memory| -> Result<Vec<bool>> {
        let s = &pkg.steps[i];
        if s.op == Op::Fetch || (s.op == Op::Send && s.flags & F_MEM != 0) {
            mem.retain(|(u, _), _| *u != s.unit);
        }
        if !send(s) || pkg.units[s.unit as usize].type_code != 0x5643 {
            return Ok(vec![]);
        }
        let start = first(s);
        let words = pkg
            .payloads
            .get(start..start + s.count as usize)
            .ok_or_else(|| Error::new("IMR.CFG", "instruction range outside payloads"))?;
        let mut keep = vec![false; words.len()];
        // Constant writes reach their fixed point after one body. The second
        // pass includes the implicit backedge in the per-word removal mask.
        let passes = if s.op == Op::Repeat && !crate::mutant::on("imr/first-iteration-only") {
            ((s.arg >> 32) & 65535).min(2)
        } else {
            1
        };
        for _ in 0..passes {
            for (j, &w) in words.iter().enumerate() {
                if w.get(shift, 4) != OP_IMEM {
                    keep[j] = true;
                    continue;
                }
                let key = (s.unit, cores.isa.imem_addr(w));
                let value = cores.isa.imem_word(w);
                if opaque.contains(&s.unit)
                    || (patched.contains(&(start + j))
                        && !crate::mutant::on("imr/patch-is-constant"))
                {
                    mem.remove(&key);
                    keep[j] = true;
                } else {
                    keep[j] |= mem.get(&key) != Some(&value);
                    mem.insert(key, value);
                }
            }
        }
        Ok(keep)
    };
    let mut inputs = BTreeMap::from([(0usize, initial)]);
    let mut todo = VecDeque::from([0usize]);
    let mut exits: Option<Memory> = None;
    while let Some(i) = todo.pop_front() {
        let mut out = inputs[&i].clone();
        let Some(s) = pkg.steps.get(i) else {
            meet(&mut exits, out);
            continue;
        };
        transfer(i, &mut out)?;
        if s.op == Op::End {
            meet(&mut exits, out);
            continue;
        }
        let successors = if s.op == Op::Jump {
            vec![i + 1, s.unit as usize]
        } else {
            vec![i + 1]
        };
        for next in successors {
            if let Some(old) = inputs.get_mut(&next) {
                let before = old.len();
                old.retain(|key, value| out.get(key) == Some(value));
                if old.len() != before {
                    todo.push_back(next);
                }
            } else {
                inputs.insert(next, out.clone());
                todo.push_back(next);
            }
        }
    }
    let mut masks = Masks::new();
    for (i, s) in pkg.steps.iter().enumerate() {
        if send(s) && pkg.units[s.unit as usize].type_code == 0x5643 {
            let mut mem = inputs.get(&i).cloned().unwrap_or_default();
            masks.insert(i, transfer(i, &mut mem)?);
        }
    }
    Ok((masks, exits.unwrap_or_default()))
}
fn meet(into: &mut Option<Memory>, value: Memory) {
    if let Some(old) = into {
        old.retain(|key, v| value.get(key) == Some(v));
    } else {
        *into = Some(value);
    }
}
