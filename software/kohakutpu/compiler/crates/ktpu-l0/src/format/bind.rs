//! Host binding: each buffer's address, every relocated field rewritten
//! (docs/spec/stack/l0/binding.md L0.BIND.009-L0.BIND.011, L0.BIND.034;
//! RUL.039, RUL.040).

use super::{Buffer, Package, Reloc, UNBOUND};
use crate::error::Result;
use crate::mutant;
use crate::refuse;
use crate::word::{mask64, Word, WordOps};

/// Each buffer's address: its binding when one is given and not [`UNBOUND`],
/// else its default (L0.BIND.009; address 0 binds like any other, RUL.040).
pub fn bind_addresses(buffers: &[Buffer], bindings: &[u64]) -> Vec<u64> {
    buffers
        .iter()
        .enumerate()
        .map(|(i, b)| match bindings.get(i) {
            _ if mutant::on("l0.bind/default_wins") => b.default,
            Some(&a) if mutant::on("l0.bind/zero_binds") => a,
            Some(&0) if mutant::on("l0.bind/zero_unbound") => b.default,
            Some(&a) if a != UNBOUND => a,
            _ => b.default,
        })
        .collect()
}

/// Driver MMU entries: virtual base, physical base and accessible bytes.
pub type Translation = (u64, u64, u64);

/// Resolve package-local buffer handles without accepting physical fallbacks.
pub fn translate(buffers: &[Buffer], handles: &[u64], table: &[Translation]) -> Result<Vec<u64>> {
    if mutant::on("l0.mmu/identity") {
        return Ok(handles.to_vec());
    }
    if handles.len() != buffers.len() {
        return refuse!("RUL.023", "one handle is required per buffer-table entry");
    }
    for (i, &(va, pa, size)) in table.iter().enumerate() {
        if size == 0 || va.checked_add(size).is_none() || pa.checked_add(size).is_none() {
            return refuse!("RUL.023", "invalid translation extent");
        }
        if table[..i]
            .iter()
            .any(|&(v, _, n)| va < v + n && v < va + size)
        {
            return refuse!("RUL.023", "overlapping virtual translations");
        }
    }
    buffers
        .iter()
        .zip(handles)
        .map(|(buffer, &handle)| {
            let Some(&(va, pa, size)) = table
                .iter()
                .find(|&&(v, _, n)| handle >= v && handle - v < n)
            else {
                return refuse!(
                    "RUL.023",
                    "unknown or retired allocation handle {handle:#x}"
                );
            };
            let offset = handle - va;
            if buffer.size > size - offset {
                return refuse!("RUL.023", "buffer {} extent {} exceeds allocation handle {handle:#x} with {} remaining bytes", buffer.name, buffer.size, size - offset);
            }
            Ok(pa + offset)
        })
        .collect()
}

/// Pairwise buffer overlap/alias relations `[i, j, addr_j - addr_i]` on each mesh.
pub fn environment(buffers: &[Buffer], addresses: &[u64]) -> Result<Vec<[i64; 3]>> {
    if addresses.len() != buffers.len() {
        return refuse!("L0.BIND.044", "one bound address is required per buffer");
    }
    let mut aliases = Vec::new();
    for i in 0..buffers.len() {
        let (ai, si) = (addresses[i] as i128, buffers[i].size as i128);
        for j in (i + 1)..buffers.len() {
            if buffers[i].mesh != buffers[j].mesh {
                continue;
            }
            let (aj, sj) = (addresses[j] as i128, buffers[j].size as i128);
            if ai == aj || (ai < aj + sj && aj < ai + si) {
                let delta = i64::try_from(aj - ai)
                    .map_err(|_| crate::Refusal::new("L0.BIND.044", "alias offset exceeds i64"))?;
                aliases.push([i as i64, j as i64, delta]);
            }
        }
    }
    Ok(aliases)
}

/// `(base + addend) >> shift`, the value `r`'s field takes before truncation.
fn field_value(r: &Reloc, base: u64) -> i128 {
    (base as i128 + r.addend) >> r.shift.min(127)
}

/// `payload` with `r`'s field := `((base + addend) >> shift) & (2^width - 1)`,
/// every other bit kept (L0.BIND.010).
pub fn apply_reloc(payload: Word, r: &Reloc, base: u64) -> Word {
    let base = if mutant::on("l0.bind/addr_off") {
        base + 0x40
    } else {
        base
    };
    let mut v = field_value(r, base) as u64 & mask64(r.width as u32);
    if mutant::on("l0.bind/no_shift") {
        v = (base as i128 + r.addend) as u64 & mask64(r.width as u32);
    }
    let mut out = if mutant::on("l0.bind/clobber") {
        Word::ZERO
    } else {
        payload
    };
    out.set(r.bit as u32, r.width as u32, v);
    out
}

/// RUL.039: the bound address plus the addend lies inside the buffer, and
/// the address fits its field. A field split into segments is checked at its
/// top segment (none continues it at `shift + width`).
fn check_reloc(p: &Package, r: &Reloc, buf: &Buffer, base: u64) -> Result<()> {
    if mutant::on("l0.bind/unchecked") {
        return Ok(());
    }
    if r.addend < 0 || r.addend >= buf.size as i128 {
        return refuse!(
            "RUL.039",
            "relocation addend {} is outside its {}-byte buffer",
            r.addend,
            buf.size
        );
    }
    let address = (base as i128).checked_add(r.addend);
    if !matches!(address, Some(0..=0xffff_ffff_ffff_ffff)) {
        return refuse!(
            "RUL.039",
            "relocation address is outside the 64-bit address space"
        );
    }
    let continued = p.relocs.iter().any(|o| {
        o.payload == r.payload
            && o.buffer == r.buffer
            && o.addend == r.addend
            && o.shift == r.shift + r.width
    });
    let v = field_value(r, base);
    if !continued && (v < 0 || (r.width < 64 && v >> r.width != 0)) {
        return refuse!(
            "RUL.039",
            "address {:#x} does not fit the {}-bit field at bit {} of payload {}",
            base as i128 + r.addend,
            r.width,
            r.bit,
            r.payload
        );
    }
    Ok(())
}

impl Package {
    /// Payloads with every relocation applied; unbound buffers take their
    /// default. Refuses a relocation naming a payload or buffer past its
    /// table, or one RUL.039 refuses.
    pub fn bound(&self, bindings: &[u64]) -> Result<Vec<Word>> {
        let addrs = bind_addresses(&self.buffers, bindings);
        let mut out = self.payloads.clone();
        for r in &self.relocs {
            let (p, b) = (r.payload as usize, r.buffer as usize);
            if p >= out.len() || b >= addrs.len() {
                return refuse!(
                    "L0.PKG.032",
                    "relocation names payload {p} of {}, buffer {b} of {}",
                    out.len(),
                    addrs.len()
                );
            }
            check_reloc(self, r, &self.buffers[b], addrs[b])?;
            out[p] = apply_reloc(out[p], r, addrs[b]);
        }
        self.check_repeat_bindings(&out)?;
        for step in self.steps.iter().filter(|s| {
            s.op == super::Op::Patch && s.arg & (1 << 26) != 0 && s.arg & (1 << 24) == 0
        }) {
            let bit = step.arg & 255;
            let width = (step.arg >> 8) & 255;
            for reloc in self.relocs.iter().filter(|r| {
                r.payload == step.count && r.bit == bit && r.width == width && r.shift == 0
            }) {
                let buffer = &self.buffers[reloc.buffer as usize];
                let base = addrs[reloc.buffer as usize] as u128 & mask64(width as u32) as u128;
                if !mutant::on("l0.bind/unchecked")
                    && (!(1..=64).contains(&width) || base + buffer.size as u128 > 1u128 << width)
                {
                    return refuse!(
                        "L0.STEP.054",
                        "additive address patch buffer crosses its field boundary"
                    );
                }
            }
        }
        Ok(out)
    }

    pub(super) fn check_repeat_bindings(&self, words: &[Word]) -> Result<()> {
        if mutant::on("l0.bind/repeat-wrap") {
            return Ok(());
        }
        for step in self.steps.iter().filter(|s| s.op == super::Op::Repeat) {
            let first = (step.arg & 0xffff_ffff) as usize;
            if !self
                .relocs
                .iter()
                .any(|r| r.payload >= first as u64 && r.payload - (first as u64) < step.count)
            {
                continue;
            }
            let repeats = (step.arg >> 32) & 0xffff;
            let increments = step.arg >> 48;
            if repeats == 0 {
                return refuse!("L0.STEP.041", "zero repeat count at binding");
            }
            for index in 0..increments {
                let table_index = first
                    .checked_add(step.count as usize)
                    .and_then(|i| i.checked_add((index / 2) as usize))
                    .ok_or_else(|| {
                        crate::Refusal::new("L0.STEP.041", "repeat table index overflow")
                    })?;
                let table = words.get(table_index).ok_or_else(|| {
                    crate::Refusal::new("L0.STEP.041", "repeat table outside payloads")
                })?;
                let at = (index % 2) as u32 * 128;
                let relative = table.get(at, 32) as usize;
                let bit = table.get(at + 32, 8) as u32;
                let width = table.get(at + 40, 8) as u32;
                if relative as u64 >= step.count || !(1..=64).contains(&width) || bit + width > 256
                {
                    return refuse!("L0.STEP.041", "invalid repeat field at binding");
                }
                let payload = first.checked_add(relative).ok_or_else(|| {
                    crate::Refusal::new("L0.STEP.041", "repeat payload index overflow")
                })?;
                let word = words.get(payload).ok_or_else(|| {
                    crate::Refusal::new("L0.STEP.041", "repeat payload outside package")
                })?;
                let delta = table.get(at + 64, 64) as u128 * (repeats - 1) as u128;
                if word.get(bit, width) as u128 + delta >= 1u128 << width {
                    return refuse!("L0.STEP.041", "bound repeat increment wraps its field");
                }
                for reloc in self.relocs.iter().filter(|r| {
                    r.payload as usize == payload
                        && (r.bit as u32) < bit + width
                        && bit < r.bit as u32 + r.width as u32
                }) {
                    if reloc.bit as u32 != bit || reloc.width as u32 != width {
                        return refuse!(
                            "L0.STEP.041",
                            "repeat increment partially overlaps a relocation"
                        );
                    }
                    let end = 1u128
                        .checked_shl(reloc.shift as u32)
                        .and_then(|scale| delta.checked_mul(scale))
                        .and_then(|v| v.checked_add(reloc.addend as u128));
                    let size = self.buffers[reloc.buffer as usize].size as u128;
                    if end.is_none_or(|v| v >= size) {
                        return refuse!("RUL.039", "repeated address leaves its bound buffer");
                    }
                }
            }
        }
        Ok(())
    }

    /// This package with every relocation applied: no buffer, no relocation,
    /// the bytes a run at `bindings` reads (L0.BIND.011, L0.BIND.034).
    pub fn bind(&self, bindings: &[u64]) -> Result<Package> {
        for fetch in self
            .steps
            .iter()
            .filter(|s| s.op == super::Op::Fetch && s.flags & super::F_MEM != 0)
        {
            if self
                .steps
                .iter()
                .any(|s| s.op == super::Op::Patch && s.arg & (1 << 24) == 0 && s.count == fetch.arg)
            {
                return refuse!(
                    "L0.STEP.054",
                    "fetch address patches target Step.arg, not its host address payload"
                );
            }
        }
        let payloads = self.bound(bindings)?;
        let mut steps = self.steps.clone();
        for step in steps
            .iter_mut()
            .filter(|s| s.op == super::Op::Fetch && s.flags & super::F_MEM != 0)
        {
            let Some(word) = payloads.get(step.arg as usize) else {
                return refuse!("L0.PKG.032", "fetch address payload outside table");
            };
            step.arg = word.0[0];
            step.flags &= !super::F_MEM;
        }
        Ok(Package {
            payloads,
            steps,
            relocs: if mutant::on("l0.bind/keeps_relocs") {
                self.relocs.clone()
            } else {
                Vec::new()
            },
            buffers: Vec::new(),
            buffer_names: Vec::new(),
            ..self.clone()
        })
    }
}
