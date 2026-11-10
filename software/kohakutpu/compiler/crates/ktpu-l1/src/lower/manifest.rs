//! Host-only image source sites, retained beside relocatable package bytes.

use std::collections::BTreeMap;

use ktpu_isa::layout::value;
use ktpu_isa::unit::vector::{VecIsa, OP_IMEM, OP_RUN};
use ktpu_l0::{Op, Package};
use serde_json::{json, Value};

use super::residency::Image;
use crate::error::{Error, Result};

#[derive(Default)]
struct Memory {
    words: BTreeMap<u32, u64>,
    sites: BTreeMap<u32, usize>,
    /// A write changes both the instruction and its source site, even when
    /// the bits are identical. Cached failures are also invalidated by writes.
    runs: BTreeMap<u32, Option<Vec<usize>>>,
}

/// Each RUN's source payload sites; bound values are read only after relocation.
pub fn describe(pkg: &Package, isa: &VecIsa) -> Value {
    let mut memory: BTreeMap<u64, Memory> = BTreeMap::new();
    let mut entries = Vec::new();
    for (step, s) in pkg.steps.iter().enumerate() {
        if matches!(s.op, Op::Jump | Op::Fetch | Op::Engine) {
            memory.clear();
        }
        if !matches!(s.op, Op::Dispatch | Op::Repeat) {
            continue;
        }
        if !pkg
            .units
            .get(s.unit as usize)
            .is_some_and(|u| u.type_code == 0x5643)
        {
            continue;
        }
        let first = if s.op == Op::Repeat {
            s.arg & 0xffff_ffff
        } else {
            s.arg
        };
        let Some(words) = pkg.payloads.get(first as usize..(first + s.count) as usize) else {
            continue;
        };
        let source = memory.entry(s.unit).or_default();
        for (offset, &word) in words.iter().enumerate() {
            let index = first as usize + offset;
            match word.get(isa.cfg.payload_bits - 4, 4) {
                OP_IMEM => {
                    let address = isa.imem_addr(word);
                    source.words.insert(address, isa.imem_word(word));
                    source.sites.insert(address, index);
                    source.runs.clear();
                }
                OP_RUN => {
                    let Ok(fields) = isa.run.decode(word) else {
                        continue;
                    };
                    let pc = value(&fields, "pc") as u32;
                    let sites = source.runs.entry(pc).or_insert_with(|| {
                        Image::at(&source.words, pc).ok().map(|image| {
                            image
                                .words
                                .iter()
                                .map(|&(a, _)| source.sites[&(pc + a)])
                                .collect()
                        })
                    });
                    let Some(sites) = sites else {
                        continue;
                    };
                    entries.push(json!({"step": step, "run": index, "unit": s.unit,
                                        "source_base": pc, "sites": sites}));
                }
                _ => {}
            }
        }
    }
    json!({"version": 1, "images": entries})
}

/// Validate topology against the unbound package, never trust metadata as bytes.
pub fn validate(pkg: &Package, isa: &VecIsa, manifest: &Value) -> Result<()> {
    if *manifest != describe(pkg, isa) {
        return Err(Error::new(
            "IMR.MANIFEST",
            "image manifest does not describe this package",
        ));
    }
    Ok(())
}
