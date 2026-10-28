//! The package's bytes: a header, then the unit, buffer, step, relocation and
//! payload tables, each 32-byte aligned (L0.PKG.001-L0.PKG.027).

use super::{
    fnv1a, Buffer, Kind, Op, Package, Reloc, Step, Unit, ALIGN, BUFFER_BYTES, CHECK_WORD,
    FLAG_CHECKSUM, FLAG_RX, FNV_BASIS, F_IMM, HEADER_WORDS, PAYLOAD_BYTES, RELOC_BYTES, STEP_BYTES,
    UNIT_BYTES,
};
use crate::error::Result;
use crate::facts::Facts;
use crate::mutant;
use crate::refuse;
use crate::word::Word;

const MASK32: u64 = 0xFFFF_FFFF;

fn u64_at(data: &[u8], at: usize) -> u64 {
    let mut le = [0u8; 8];
    le.copy_from_slice(&data[at..at + 8]);
    u64::from_le_bytes(le)
}

fn put(out: &mut Vec<u8>, words: &[u64]) {
    for w in words {
        out.extend_from_slice(&w.to_le_bytes());
    }
}

fn magic(f: &Facts) -> u64 {
    if mutant::on("l0.pkg/magic") {
        0x4B50_414C
    } else {
        f.package_magic
    }
}

fn version(f: &Facts) -> u64 {
    if mutant::on("l0.pkg/version") {
        3
    } else {
        f.package_version
    }
}

fn header_bytes(f: &Facts) -> u64 {
    if mutant::on("l0.pkg/header_bytes") {
        128
    } else {
        f.header_bytes
    }
}

fn align() -> usize {
    if mutant::on("l0.pkg/align16") {
        16
    } else {
        ALIGN
    }
}

fn flag_checksum() -> u64 {
    if mutant::on("l0.pkg/checksum_flag") {
        4
    } else {
        FLAG_CHECKSUM
    }
}

fn check_word() -> usize {
    if mutant::on("l0.pkg/check_word") {
        11
    } else {
        CHECK_WORD
    }
}

/// The byte-level mutants of the written package (`_wrap_to_bytes`).
fn mutate_bytes(out: &mut [u8]) {
    let h = |out: &[u8], k: usize| u64_at(out, 8 * k);
    let set = |out: &mut [u8], at: usize, v: u64| out[at..at + 8].copy_from_slice(&v.to_le_bytes());
    if mutant::on("l0.pkg/rx_always") {
        out[12] |= 2;
    }
    if mutant::on("l0.pkg/count_fields") {
        let w = h(out, 4);
        let (nstep, nunit) = ((w >> 16) & 0xFFFF, (w >> 32) & 0xFFFF);
        set(
            out,
            32,
            w & !(0xFFFF << 16) & !(0xFFFF << 32) | nunit << 16 | nstep << 32,
        );
    }
    if mutant::on("l0.pkg/payload_order") {
        let (at, n) = (h(out, 9) as usize, (h(out, 3) & MASK32) as usize);
        for k in 0..n {
            out[at + 32 * k..at + 32 * (k + 1)].reverse();
        }
    }
    if mutant::on("l0.pkg/step_fields") {
        let (at, n) = (h(out, 7) as usize, ((h(out, 4) >> 16) & 0xFFFF) as usize);
        for k in 0..n {
            let w = u64_at(out, at + 16 * k);
            set(
                out,
                at + 16 * k,
                (w & 0xFF) << 8 | (w >> 8) & 0xFF | w & !0xFFFF,
            );
        }
    }
    if mutant::on("l0.pkg/table_order") {
        let (nunit, nstep) = (
            ((h(out, 4) >> 32) & 0xFFFF) as usize,
            ((h(out, 4) >> 16) & 0xFFFF) as usize,
        );
        if nunit != 0 && nstep != 0 {
            let (ou, os) = (h(out, 5) as usize, h(out, 7) as usize);
            let units = out[ou..ou + 16 * nunit].to_vec();
            let steps = out[os..os + 16 * nstep].to_vec();
            let fit = |src: &[u8], n: usize| {
                let mut v = src[..src.len().min(n)].to_vec();
                v.resize(n, 0);
                v
            };
            out[ou..ou + 16 * nunit].copy_from_slice(&fit(&steps, 16 * nunit));
            out[os..os + 16 * nstep].copy_from_slice(&fit(&units, 16 * nstep));
        }
    }
}

/// FNV-1a 64 over `data` with the checksum word read as zero (L0.PKG.010).
pub fn checksum(data: &[u8]) -> u64 {
    let lo = check_word() * 8;
    let hi = lo + 8;
    let h = super::fnv1a_from(&data[..lo.min(data.len())], FNV_BASIS);
    let h = super::fnv1a_from(&[0u8; 8], h);
    super::fnv1a_from(&data[hi.min(data.len())..], h)
}

fn ceil_div(a: u128, b: u128) -> u128 {
    a.div_ceil(b)
}

impl Package {
    /// The writer's checks (L0.PKG.011, L0.PKG.032).
    pub fn check(&self, f: &Facts) -> Result<()> {
        if self.units.len() > 0xFFFF || self.buffers.len() > 0xFFFF || self.steps.len() > 0xFFFF {
            return refuse!("L0.PKG.011", "a count does not fit its 16-bit header field");
        }
        if self.units.len() > f.unit_bound() && !mutant::on("l0.pkg/unlimited_units") {
            return refuse!(
                "RUL.005",
                "{} units; a package names at most {}",
                self.units.len(),
                f.unit_bound()
            );
        }
        let npay = self.payloads.len() as u128;
        for r in &self.relocs {
            r.check()?;
            if r.payload as u128 >= npay {
                return refuse!(
                    "L0.PKG.032",
                    "relocation names payload {} of {npay}",
                    r.payload
                );
            }
            if r.buffer as usize >= self.buffers.len() {
                return refuse!(
                    "L0.PKG.032",
                    "relocation names buffer {} of {}",
                    r.buffer,
                    self.buffers.len()
                );
            }
        }
        let nunit = self.units.len() as u64;
        for (i, s) in self.steps.iter().enumerate() {
            let (arg, count) = (s.arg as u128, s.count as u128);
            let past = || refuse!("L0.PKG.032", "step {i} reads payloads past {npay}");
            if matches!(s.op, Op::Dispatch | Op::Mover) && arg + count > npay {
                return past();
            }
            if s.op == Op::Engine && arg + ceil_div(count, 3) > npay {
                return past();
            }
            if s.op == Op::Repeat {
                let first = (s.arg & MASK32) as u128;
                let incs = (s.arg >> 48) as u128;
                if first + count + incs.div_ceil(2) > npay {
                    return past();
                }
            }
            if matches!(s.op, Op::Dispatch | Op::Repeat | Op::Await | Op::Fetch) && s.unit >= nunit
            {
                return refuse!("L0.PKG.032", "step {i} names unit {} of {nunit}", s.unit);
            }
            if s.op == Op::Fetch && s.flags & super::F_MEM != 0 && arg >= npay {
                return past();
            }
            if s.op == Op::Fetch && !self.units[s.unit as usize].has_port() {
                return refuse!(
                    "L0.PKG.032",
                    "step {i} fetches for unit {}: it has no port",
                    s.unit
                );
            }
            let reads = match s.op {
                Op::Store | Op::Ctrl => Some(count),
                Op::Load | Op::Recv => Some(1),
                Op::Send if s.flags & super::F_MEM != 0 => Some(2),
                Op::Send => Some(count),
                _ => None,
            };
            if let Some(n) = reads {
                if (s.arg & MASK32) as u128 + n > npay {
                    return past();
                }
            }
            if s.op == Op::Send && s.unit >= nunit {
                return refuse!("L0.PKG.032", "step {i} names unit {} of {nunit}", s.unit);
            }
            if s.op == Op::Recv && s.unit != super::ANY_UNIT && s.unit >= nunit {
                return refuse!("L0.PKG.032", "step {i} names unit {} of {nunit}", s.unit);
            }
            if s.op == Op::Jump && s.unit > self.steps.len() as u64 {
                return refuse!(
                    "L0.PKG.032",
                    "step {i} jumps to {} of {}",
                    s.unit,
                    self.steps.len()
                );
            }
            if matches!(s.op, Op::Load | Op::Alu | Op::Patch | Op::AssertZero) && s.unit >= f.regs {
                return refuse!(
                    "L0.PKG.032",
                    "step {i} names register {} of {}",
                    s.unit,
                    f.regs
                );
            }
            if s.op == Op::Load && s.flags & F_IMM != 0 && (s.flags != F_IMM || s.count != 0) {
                return refuse!(
                    "L0.PKG.032",
                    "step {i} immediate LOAD has reserved flags/count"
                );
            }
            if s.op == Op::Await && s.flags != 0 && (s.flags != F_IMM || s.count != 0 || s.arg != 0)
            {
                return refuse!(
                    "L0.PKG.032",
                    "step {i} runtime AWAIT has reserved flags/count/argument"
                );
            }
            if s.op == Op::AssertZero && (s.flags != 0 || s.arg != 0) {
                return refuse!(
                    "L0.PKG.032",
                    "step {i} ASSERT_ZERO has reserved flags/argument"
                );
            }
            if s.op == Op::Patch {
                let n = if s.arg >> 24 & 1 != 0 {
                    self.steps.len() as u64
                } else {
                    self.payloads.len() as u64
                };
                if s.count >= n {
                    return refuse!("L0.PKG.032", "step {i} patches word {} of {n}", s.count);
                }
            }
        }
        Ok(())
    }

    /// The wire form, a multiple of 32 bytes (L0.PKG.001).
    pub fn to_bytes(&self, f: &Facts) -> Result<Vec<u8>> {
        self.check(f)?;
        let mut units = Vec::with_capacity(self.units.len() * UNIT_BYTES);
        for u in &self.units {
            put(
                &mut units,
                &[u.word(), u.credit & MASK32 | (u.fetch & MASK32) << 32],
            );
        }
        let mut buffers = Vec::with_capacity(self.buffers.len() * BUFFER_BYTES);
        for b in &self.buffers {
            put(
                &mut buffers,
                &[
                    fnv1a(b.name.as_bytes()),
                    b.size,
                    b.default,
                    b.kind as u64 | (b.mesh & 0xFF) << 8,
                ],
            );
        }
        let mut steps = Vec::with_capacity(self.steps.len() * STEP_BYTES);
        for s in &self.steps {
            put(
                &mut steps,
                &[
                    s.op as u64
                        | (s.flags as u64) << 8
                        | (s.unit & 0xFFFF) << 16
                        | (s.count & MASK32) << 32,
                    s.arg,
                ],
            );
        }
        let mut sorted: Vec<&Reloc> = self.relocs.iter().collect();
        sorted.sort_by_key(|r| (r.payload, r.bit));
        let mut relocs = Vec::with_capacity(self.relocs.len() * RELOC_BYTES);
        for r in sorted {
            put(
                &mut relocs,
                &[
                    r.payload | r.bit << 32 | r.width << 40 | r.shift << 48 | r.buffer << 56,
                    r.addend as u64,
                ],
            );
        }
        let mut payloads = Vec::with_capacity(self.payloads.len() * PAYLOAD_BYTES);
        for p in &self.payloads {
            payloads.extend_from_slice(&p.to_le_bytes());
        }
        let sections = [units, buffers, steps, relocs, payloads];
        let mut offs = [0u64; 5];
        let mut at = header_bytes(f) as usize;
        for (k, s) in sections.iter().enumerate() {
            offs[k] = at as u64;
            at += s.len().div_ceil(align()) * align();
        }
        let total = at;
        let mut flags = if self.checksum { flag_checksum() } else { 0 };
        if self.steps.iter().any(|s| s.op == Op::Recv) && !mutant::on("l0.pkg/rx_flag") {
            flags |= FLAG_RX;
        }
        let mut out = Vec::with_capacity(total);
        let head: [u64; HEADER_WORDS] = [
            magic(f) | version(f) << 32 | header_bytes(f) << 48,
            self.signature,
            total as u64 | flags << 32,
            self.payloads.len() as u64 | (self.relocs.len() as u64) << 32,
            self.buffers.len() as u64
                | (self.steps.len() as u64) << 16
                | (self.units.len() as u64) << 32
                | (self.ack_reserve & 0xFFFF) << 48,
            offs[0],
            offs[1],
            offs[2],
            offs[3],
            offs[4],
            0,
            0,
        ];
        put(&mut out, &head);
        for (s, o) in sections.iter().zip(offs) {
            out.resize(o as usize, 0);
            out.extend_from_slice(s);
        }
        out.resize(total, 0);
        if self.checksum {
            let sum = checksum(&out);
            let at = check_word() * 8;
            out[at..at + 8].copy_from_slice(&sum.to_le_bytes());
        }
        mutate_bytes(&mut out);
        Ok(out)
    }

    /// Parse the wire form; each table is found by its header offset
    /// (L0.PKG.002). Refuses anything malformed (L0.PKG.010, .012, .013).
    pub fn from_bytes(data: &[u8], f: &Facts) -> Result<Package> {
        let hb = header_bytes(f) as usize;
        if data.len() < hb.max(HEADER_WORDS * 8) {
            return refuse!(
                "L0.PKG.013",
                "{} bytes is shorter than the header",
                data.len()
            );
        }
        let mut h: Vec<u64> = (0..HEADER_WORDS).map(|k| u64_at(data, 8 * k)).collect();
        if mutant::on("l0.pkg/reader_in_order") {
            let sizes = [
                ((h[4] >> 32) & 0xFFFF) * 16,
                (h[4] & 0xFFFF) * 32,
                ((h[4] >> 16) & 0xFFFF) * 16,
                (h[3] >> 32) * 16,
                (h[3] & MASK32) * 32,
            ];
            let mut at = 96;
            for (i, n) in sizes.iter().enumerate() {
                h[5 + i] = at;
                at += n.div_ceil(32) * 32;
            }
        }
        if h[0] & MASK32 != magic(f) {
            return refuse!("L0.PKG.012", "magic {:#x} is not KAPK", h[0] & MASK32);
        }
        if (h[0] >> 32) & 0xFFFF != version(f) || h[0] >> 48 != header_bytes(f) {
            return refuse!(
                "L0.PKG.012",
                "version/header {:#x} is not this reader's",
                h[0] >> 32
            );
        }
        let (total, flags) = (h[2] & MASK32, h[2] >> 32);
        if total as usize > data.len() {
            return refuse!(
                "L0.PKG.013",
                "header says {total} bytes, {} present",
                data.len()
            );
        }
        let (npay, nrel) = (h[3] & MASK32, h[3] >> 32);
        let (nbuf, nstep, nunit) = (h[4] & 0xFFFF, (h[4] >> 16) & 0xFFFF, (h[4] >> 32) & 0xFFFF);
        let (ou, ob, os, orl, op) = (h[5], h[6], h[7], h[8], h[9]);
        for (off, n, size) in [
            (ou, nunit, UNIT_BYTES),
            (ob, nbuf, BUFFER_BYTES),
            (os, nstep, STEP_BYTES),
            (orl, nrel, RELOC_BYTES),
            (op, npay, PAYLOAD_BYTES),
        ] {
            if off as u128 + n as u128 * size as u128 > total as u128 {
                return refuse!(
                    "L0.PKG.013",
                    "a section at {off:#x} runs past {total} bytes"
                );
            }
        }
        let body = &data[..total as usize];
        if flags & flag_checksum() != 0 && checksum(body) != h[check_word()] {
            return refuse!("L0.PKG.010", "checksum mismatch");
        }
        let (ou, ob, os, orl, op) = (
            ou as usize,
            ob as usize,
            os as usize,
            orl as usize,
            op as usize,
        );
        let mut units = Vec::with_capacity(nunit as usize);
        for i in 0..nunit as usize {
            let w = u64_at(body, ou + i * UNIT_BYTES);
            let credit = u64_at(body, ou + i * UNIT_BYTES + 8);
            units.push(Unit {
                type_code: (w >> 32) & 0xFFFF,
                x: w & 0xFF,
                y: (w >> 8) & 0xFF,
                mesh: (w >> 16) & 0xFF,
                credit: credit & MASK32,
                fetch: credit >> 32,
            });
        }
        let mut buffers = Vec::with_capacity(nbuf as usize);
        for i in 0..nbuf as usize {
            let at = ob + i * BUFFER_BYTES;
            let kind = u64_at(body, at + 24);
            let Some(k) = Kind::from_code(kind & 0xFF) else {
                return refuse!("L0.PKG.024", "buffer {i} kind {} is not one", kind & 0xFF);
            };
            buffers.push(Buffer {
                name: format!("#{:016x}", u64_at(body, at)),
                size: u64_at(body, at + 8),
                default: u64_at(body, at + 16),
                kind: k,
                mesh: (kind >> 8) & 0xFF,
            });
        }
        let mut steps = Vec::with_capacity(nstep as usize);
        for i in 0..nstep as usize {
            let w = u64_at(body, os + i * STEP_BYTES);
            let arg = u64_at(body, os + i * STEP_BYTES + 8);
            let Some(o) = Op::from_code(w & 0xFF) else {
                return refuse!("L0.STEP.003", "step {i} opcode {} is not one", w & 0xFF);
            };
            let flags = if mutant::on("l0.pkg/read_step_fields") {
                (w >> 9) & 0xFF
            } else {
                (w >> 8) & 0xFF
            };
            steps.push(Step::new(o, (w >> 16) & 0xFFFF, w >> 32, arg, flags as u8));
        }
        let mut relocs = Vec::with_capacity(nrel as usize);
        for i in 0..nrel as usize {
            let w = u64_at(body, orl + i * RELOC_BYTES);
            let add = u64_at(body, orl + i * RELOC_BYTES + 8);
            relocs.push(Reloc {
                payload: w & MASK32,
                bit: (w >> 32) & 0xFF,
                width: (w >> 40) & 0xFF,
                buffer: w >> 56,
                addend: add as i64 as i128,
                shift: (w >> 48) & 0xFF,
            });
        }
        let payloads = (0..npay as usize)
            .map(|i| {
                let at = op + i * PAYLOAD_BYTES;
                Word::from_le_bytes(body[at..at + PAYLOAD_BYTES].try_into().unwrap())
            })
            .collect();
        Ok(Package {
            units,
            buffers,
            steps,
            relocs,
            payloads,
            signature: h[1],
            ack_reserve: h[4] >> 48,
            checksum: flags & flag_checksum() != 0,
            buffer_names: Vec::new(),
        })
    }
}
