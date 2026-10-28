//! Bound packages rewritten against the node's submitted IMEM state.

use std::collections::BTreeMap;

use ktpu_isa::layout::value;
use ktpu_isa::unit::vector::core::{VHALT, VLOOP, VSETI, VSKIPZ};
use ktpu_isa::unit::vector::{VecCu, OP_IMEM, OP_RUN};
use ktpu_l0::format::{type_name, Op};
use ktpu_l0::{Package, Word, WordOps};

use super::imem::{rewrite, Cores, Resident};
use crate::error::{Error, Result};

/// A host-only image manifest, normalized independently of its source placement.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Image {
    pub source_base: u32,
    pub words: Vec<(u32, u64)>,
}

impl Image {
    pub fn at(source: &BTreeMap<u32, u64>, pc: u32) -> Result<Self> {
        if crate::mutant::on("imr/union-image") {
            return Ok(Self {
                source_base: pc,
                words: source.iter().map(|(&a, &w)| (a, w)).collect(),
            });
        }
        let mut words = Vec::new();
        let mut immediate = false;
        let mut reach = pc;
        for addr in pc.. {
            let word = *source.get(&addr).ok_or_else(|| {
                Error::new("IMR.IMAGE", "image has no contiguous terminating HALT")
            })?;
            words.push((addr - pc, word));
            if immediate {
                immediate = false;
            } else {
                let op = (word >> 27) & 31;
                if op == VSKIPZ {
                    reach = reach.max(addr + 1 + (word & 255) as u32);
                }
                if op == VLOOP {
                    reach = reach.max(addr + 1 + ((word >> 7) & 1023) as u32);
                }
                if op == VHALT && (addr >= reach || crate::mutant::on("imr/first-halt")) {
                    break;
                }
                immediate = op == VSETI;
            }
        }
        Ok(Self {
            source_base: pc,
            words,
        })
    }
}

/// Reconstruct each individual bound image before applying the slot policy.
/// Runtime control flow uses fixed-placement must-know analysis instead of relocation.
pub fn package(pkg: &Package, cores: &mut Cores) -> Result<Package> {
    if let Some(known) = super::counts::specialize(pkg) {
        return package(&known, cores);
    }
    if !pkg.relocs.is_empty() {
        return Err(Error::new(
            "IMR.BOUND",
            "residency requires a bound package",
        ));
    }
    if pkg
        .steps
        .iter()
        .enumerate()
        .any(|(i, s)| s.op == Op::Repeat && repeat_known(pkg, i))
    {
        let mut expanded = pkg.clone();
        for (i, s) in expanded.steps.iter_mut().enumerate() {
            if s.op != Op::Repeat || !repeat_known(pkg, i) {
                continue;
            }
            let first = (s.arg & 0xffff_ffff) as usize;
            let repeats = (s.arg >> 32) & 0xffff;
            let increments = s.arg >> 48;
            let mut changes = Vec::new();
            for k in 0..increments {
                let w = *pkg
                    .payloads
                    .get(first + s.count as usize + (k / 2) as usize)
                    .ok_or_else(|| Error::new("IMR.REPEAT", "increment leaves payloads"))?;
                let at = (k % 2) as u32 * 128;
                let h = w.limb(at);
                changes.push((
                    (h & 0xffff_ffff) as usize,
                    ((h >> 32) & 255) as u32,
                    ((h >> 40) & 255) as u32,
                    w.limb(at + 64),
                ));
            }
            let start = expanded.payloads.len();
            for r in 0..repeats {
                for j in 0..s.count as usize {
                    let mut word = *pkg
                        .payloads
                        .get(first + j)
                        .ok_or_else(|| Error::new("IMR.REPEAT", "template leaves payloads"))?;
                    for &(index, bit, width, delta) in &changes {
                        if index == j {
                            word.set(
                                bit,
                                width,
                                word.get(bit, width).wrapping_add(r.wrapping_mul(delta)),
                            );
                        }
                    }
                    expanded.payloads.push(word);
                }
            }
            s.op = Op::Dispatch;
            s.count *= repeats;
            s.arg = start as u64;
        }
        return package(&expanded, cores);
    }
    if pkg.steps.iter().any(|s| {
        matches!(
            s.op,
            Op::Repeat | Op::Jump | Op::Patch | Op::Send | Op::Engine
        )
    }) {
        return super::dynamic::package(pkg, cores);
    }
    let manifests = super::manifest::describe(pkg, &cores.isa);
    let mut next = cores.clone();
    let mut out = pkg.clone();
    out.steps.clear();
    let mut source: BTreeMap<u64, BTreeMap<u32, u64>> = BTreeMap::new();
    let mut prefixes: BTreeMap<u64, Vec<u64>> = BTreeMap::new();
    let mut waits: BTreeMap<u64, (usize, u64)> = BTreeMap::new();
    for (step_index, s) in pkg.steps.iter().enumerate() {
        let mut step = *s;
        if s.op == Op::Fetch {
            let unit = pkg
                .units
                .get(s.unit as usize)
                .ok_or_else(|| Error::new("L0.PKG.032", "FETCH names an absent unit"))?;
            if type_name(unit.type_code) == "VC" {
                if !crate::mutant::on("imr/predict-external") {
                    next.cores.remove(&(unit.x as u32, unit.y as u32));
                }
                source.remove(&s.unit);
                let prefix = prefixes.entry(s.unit).or_insert_with(|| vec![0]);
                for _ in 0..s.count {
                    prefix.push(prefix.last().copied().unwrap() + 1);
                }
            }
        } else if s.op == Op::Dispatch {
            let unit = pkg.units.get(s.unit as usize).ok_or_else(|| {
                Error::new("L0.PKG.032", "residency dispatch names an absent unit")
            })?;
            if type_name(unit.type_code) == "VC" {
                let coord = (unit.x as u32, unit.y as u32);
                let memory = next
                    .cores
                    .entry(coord)
                    .or_insert_with(|| Resident::new(next.words, next.run_reach));
                let image = source.entry(s.unit).or_default();
                let prefix = prefixes.entry(s.unit).or_insert_with(|| vec![0]);
                step.arg = out.payloads.len() as u64;
                let words = pkg
                    .payloads
                    .get(s.arg as usize..(s.arg + s.count) as usize)
                    .ok_or_else(|| {
                        Error::new("L0.PKG.032", "residency dispatch leaves payloads")
                    })?;
                for (offset, w) in words.iter().enumerate() {
                    let code = w.get(next.isa.cfg.payload_bits - 4, 4);
                    let emitted = if code == OP_IMEM {
                        image.insert(next.isa.imem_addr(*w), next.isa.imem_word(*w));
                        Vec::new()
                    } else if code == OP_RUN {
                        if image.is_empty() {
                            return Err(Error::new("IMR.IMAGE", "RUN without package-local IMEM"));
                        }
                        let pc = value(&next.isa.run.decode(*w)?, "pc") as u32;
                        let entry = manifests["images"].as_array().and_then(|entries| {
                            entries.iter().find(|e| {
                                e["step"].as_u64() == Some(step_index as u64)
                                    && e["run"].as_u64() == Some(s.arg + offset as u64)
                            })
                        });
                        let manifest = match entry {
                            Some(e) => Image {
                                source_base: pc,
                                words: e["sites"]
                                    .as_array()
                                    .unwrap()
                                    .iter()
                                    .enumerate()
                                    .map(|(i, p)| {
                                        (
                                            i as u32,
                                            next.isa.imem_word(
                                                pkg.payloads[p.as_u64().unwrap() as usize],
                                            ),
                                        )
                                    })
                                    .collect(),
                            },
                            None => Image::at(image, pc)?,
                        };
                        let mut stream: Vec<Word> = manifest
                            .words
                            .iter()
                            .map(|&(addr, word)| VecCu::Imem { addr, word }.encode(&next.isa))
                            .collect::<std::result::Result<_, _>>()?;
                        stream.push(VecCu::Run { pc: 0 }.encode(&next.isa)?);
                        rewrite(&next.isa, &stream, memory)?
                    } else {
                        rewrite(&next.isa, &[*w], memory)?
                    };
                    prefix.push(prefix.last().copied().unwrap() + emitted.len() as u64);
                    out.payloads.extend(emitted);
                }
                step.count = out.payloads.len() as u64 - step.arg;
                if step.count == 0 {
                    continue;
                }
            }
        } else if s.op == Op::Await {
            if let Some(prefix) = prefixes.get(&s.unit) {
                let (old, new) = waits.entry(s.unit).or_default();
                *old += s.count as usize;
                let target = *prefix.get(*old).ok_or_else(|| {
                    Error::new("IMR.WAIT", "AWAIT exceeds the package's dispatched words")
                })?;
                step.count = target - *new;
                *new = target;
                if step.count == 0 {
                    continue;
                }
            }
        }
        out.steps.push(step);
    }
    if !crate::mutant::on("imr/dead-payload") {
        compact(&mut out)?;
    }
    *cores = next;
    Ok(out)
}

fn repeat_known(pkg: &Package, index: usize) -> bool {
    let s = &pkg.steps[index];
    let start = s.arg & 0xffff_ffff;
    let end = start + s.count + (s.arg >> 48).div_ceil(2);
    !pkg.steps.iter().any(|p| {
        p.op == Op::Patch
            && if p.arg >> 24 & 1 != 0 {
                p.count as usize == index
            } else {
                (start..end).contains(&p.count)
            }
    })
}

/// Copy only referenced payload ranges, in step order.
fn compact(pkg: &mut Package) -> Result<()> {
    let source = std::mem::take(&mut pkg.payloads);
    for step in &mut pkg.steps {
        let count = match step.op {
            Op::Dispatch | Op::Mover | Op::Store | Op::Ctrl => step.count,
            Op::Load | Op::Recv => 1,
            _ => continue,
        };
        let first = step.arg as usize;
        let words = source
            .get(first..first + count as usize)
            .ok_or_else(|| Error::new("IMR.PAYLOAD", "payload reference exceeds storage"))?;
        step.arg = pkg.payloads.len() as u64;
        pkg.payloads.extend_from_slice(words);
    }
    Ok(())
}
