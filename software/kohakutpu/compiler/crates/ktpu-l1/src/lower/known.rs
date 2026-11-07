//! Must-know physical instruction cells at control-flow joins.
use super::imem::Cores;
use crate::error::{Error, Result};
use ktpu_isa::unit::vector::OP_IMEM;
use ktpu_l0::format::F_MEM;
use ktpu_l0::{Op, Package, Step};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;

type Memory = BTreeMap<(u64, u32), u64>;
type Shared = Arc<Memory>;
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
    let transfer = |i: usize, mem: &mut Shared| -> Result<Vec<bool>> {
        let s = &pkg.steps[i];
        if (s.op == Op::Fetch || (s.op == Op::Send && s.flags & F_MEM != 0))
            && mem.keys().any(|(u, _)| *u == s.unit)
        {
            Arc::make_mut(mem).retain(|(u, _), _| *u != s.unit);
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
                    if mem.contains_key(&key) {
                        Arc::make_mut(mem).remove(&key);
                    }
                    keep[j] = true;
                } else if mem.get(&key) != Some(&value) {
                    keep[j] = true;
                    Arc::make_mut(mem).insert(key, value);
                }
            }
        }
        Ok(keep)
    };
    // Only control-flow entries need retained input states. Linear interiors
    // replay from their block input when deriving the per-word masks.
    let mut heads = BTreeSet::from([0, pkg.steps.len()]);
    for (i, s) in pkg.steps.iter().enumerate() {
        if s.op == Op::Jump {
            heads.insert(i + 1);
            if (s.unit as usize) < pkg.steps.len() {
                heads.insert(s.unit as usize);
            }
        } else if s.op == Op::End {
            heads.insert(i + 1);
        }
    }
    let heads: Vec<_> = heads.into_iter().collect();
    let ends: BTreeMap<_, _> = heads.windows(2).map(|h| (h[0], h[1])).collect();
    let mut inputs = BTreeMap::from([(0usize, Arc::new(initial))]);
    let mut todo = VecDeque::from([0usize]);
    let mut queued = BTreeSet::from([0usize]);
    let mut exits: Option<Shared> = None;
    let stats = std::env::var_os("KTPU_VERIFY_STATS").is_some();
    let mut visits = 0usize;
    while let Some(start) = todo.pop_front() {
        queued.remove(&start);
        visits += 1;
        if stats && visits.is_power_of_two() {
            retained(&inputs, visits, pkg.steps.len());
        }
        let mut out = inputs[&start].clone();
        let Some(&end) = ends.get(&start) else {
            meet(&mut exits, out);
            continue;
        };
        for i in start..end {
            transfer(i, &mut out)?;
        }
        let s = &pkg.steps[end - 1];
        if s.op == Op::End {
            meet(&mut exits, out);
            continue;
        }
        let successors = if s.op == Op::Jump {
            vec![end, s.unit as usize]
        } else {
            vec![end]
        };
        for next in successors {
            let changed = if let Some(old) = inputs.get_mut(&next) {
                intersect(old, &out)
            } else {
                inputs.insert(next, out.clone());
                true
            };
            if changed && queued.insert(next) {
                todo.push_back(next);
            }
        }
    }
    let mut masks = Masks::new();
    for (&start, input) in &inputs {
        let Some(&end) = ends.get(&start) else {
            continue;
        };
        let mut mem = input.clone();
        for i in start..end {
            let keep = transfer(i, &mut mem)?;
            let s = &pkg.steps[i];
            if send(s) && pkg.units[s.unit as usize].type_code == 0x5643 {
                masks.insert(i, keep);
            }
        }
    }
    for (i, s) in pkg.steps.iter().enumerate() {
        if send(s) && pkg.units[s.unit as usize].type_code == 0x5643 && !masks.contains_key(&i) {
            masks.insert(i, transfer(i, &mut Arc::new(Memory::new()))?);
        }
    }
    if stats {
        retained(&inputs, visits, pkg.steps.len());
    }
    Ok((
        masks,
        exits.map_or_else(Memory::new, |memory| (*memory).clone()),
    ))
}

fn intersect(into: &mut Shared, value: &Shared) -> bool {
    if Arc::ptr_eq(into, value) || into.iter().all(|(key, v)| value.get(key) == Some(v)) {
        return false;
    }
    Arc::make_mut(into).retain(|key, v| value.get(key) == Some(v));
    true
}

fn meet(into: &mut Option<Shared>, value: Shared) {
    if let Some(old) = into {
        intersect(old, &value);
    } else {
        *into = Some(value);
    }
}

fn retained(inputs: &BTreeMap<usize, Shared>, visits: usize, steps: usize) {
    let mut memories = BTreeMap::new();
    for memory in inputs.values() {
        memories
            .entry(Arc::as_ptr(memory) as usize)
            .or_insert(memory.len());
    }
    eprintln!(
        "verify residency: steps={steps} visits={visits} block_inputs={} memory_roots={} cells={}",
        inputs.len(),
        memories.len(),
        memories.values().sum::<usize>()
    );
}
