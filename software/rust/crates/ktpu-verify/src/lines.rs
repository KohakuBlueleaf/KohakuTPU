//! L1.VER.017: two mover/xform GO entries may not partially own one write
//! line. Constant node loops and patches are evaluated; data-dependent write
//! addresses and sub-line scatter rows require an ownership proof.

use std::collections::{BTreeMap, BTreeSet};

use ktpu_l0::format::{F_IMM, MOVER_SKIP};
use ktpu_l0::word::WordOps;
use ktpu_l0::{Op, Package, Word};

use crate::{Finding, Kind, Target};

mod dynamic;

/// Maximum node steps and destination word visits in a static line proof.
pub const LIMIT: usize = 2_000_000;

#[derive(Clone, Copy, Default)]
struct Dim {
    count: u64,
    stride: i64,
    axis: usize,
    step: i64,
}

#[derive(Clone, Default)]
struct Walker {
    base: u64,
    ndim: usize,
    dims: [Dim; 8],
    windows: [(i64, u64); 2],
    xid: u64,
}

#[derive(Default)]
struct Mover {
    walkers: [Walker; 2],
    dim: (usize, usize, Dim),
    selector: usize,
    row: (u64, u64),
    owners: BTreeMap<u64, usize>,
    stream: usize,
    seen: BTreeSet<(usize, usize)>,
    scatter: Vec<(usize, u64, u64)>,
    words: usize,
    full_only: bool,
    policy_only: bool,
}

fn signed(v: u64, width: u32) -> i64 {
    ((v << (64 - width)) as i64) >> (64 - width)
}

fn fail(mesh: u32, text: impl Into<String>) -> Finding {
    Finding::new(Kind::Line, format!("node {mesh}"), text)
}

impl Mover {
    fn put(
        &mut self,
        reg: u64,
        v: u64,
        t: &Target,
        mesh: u32,
        out: &mut Vec<Finding>,
    ) -> Result<(), String> {
        match reg {
            0x10 => {
                self.selector = (v & 1) as usize;
                let w = &mut self.walkers[self.selector];
                w.base = (v >> 4) & ((1 << 40) - 1);
                w.ndim = ((v >> 44) & 7) as usize;
                w.xid = (v >> 47) & 15;
            }
            0x18 => {
                let sel = (v & 1) as usize;
                self.selector = sel;
                let i = ((v >> 1) & 7) as usize;
                self.dim = (
                    sel,
                    i,
                    Dim {
                        count: (v >> 4) & 0xffff,
                        stride: signed(v >> 20, 32),
                        ..Dim::default()
                    },
                );
            }
            0x20 => {
                let (_, i, mut d) = self.dim;
                d.axis = (v & 3) as usize;
                d.step = signed(v >> 2, 16);
                self.walkers[self.selector].dims[i] = d;
            }
            0x28 => {
                self.selector = (v & 1) as usize;
                let i = ((v >> 1) & 1) as usize;
                self.walkers[(v & 1) as usize].windows[i] =
                    (signed(v >> 2, 16), (v >> 18) & 0xffff);
            }
            0x50 => self.row = (v & 0xffff_ffff, (v >> 32) & 0xffff),
            0 if v & (1 << 16) != 0 => self.go(v & 7, t, mesh, out)?,
            _ => {}
        }
        Ok(())
    }

    fn go(
        &mut self,
        mode: u64,
        t: &Target,
        mesh: u32,
        out: &mut Vec<Finding>,
    ) -> Result<(), String> {
        if mode == t.isa.mover.modes.transform as u64
            && self.walkers[0].xid == 1
            && !t.facts.mover_quant_allowed
            && !crate::mutant::on("verify.conversion/ignore-policy")
        {
            return Err("conversion.fp16_mx7 forbids mover_quant GO".into());
        }
        if self.policy_only {
            return Ok(());
        }
        let (line, word) = (t.facts.line_bytes as u64, t.facts.word_bytes as u64);
        if line == 0 || word == 0 || line % word != 0 {
            return Err("write line must be a positive multiple of the mover word".into());
        }
        let id = self.stream;
        self.stream += 1;
        let dst = &self.walkers[1];
        if mode == t.isa.mover.modes.scatter as u64 {
            let src = &self.walkers[0];
            let count: u128 = src.dims[..src.ndim]
                .iter()
                .map(|d| u128::from(d.count.max(1)))
                .product();
            if self.row.1 != 0
                && count.is_multiple_of(u128::from(self.row.1))
                && src.windows.iter().all(|(_, extent)| *extent == 0)
                && dst.base.is_multiple_of(line)
                && self.row.0.is_multiple_of(line)
                && (self.row.1 * word).is_multiple_of(line)
            {
                return Ok(());
            }
            if self.full_only {
                return Err("scatter does not cover whole write lines".into());
            }
            let lo = dst.base / line;
            let hi = dst
                .base
                .saturating_add(u64::from(u32::MAX).saturating_mul(self.row.0))
                .saturating_add(self.row.1 * word)
                / line;
            if out.len() < 32
                && (self.owners.range(lo..=hi).next().is_some()
                    || self.scatter.iter().any(|&(_, a, b)| a <= hi && lo <= b))
            {
                out.push(fail(mesh, format!("stream {id}: sub-line scatter may share a partially written line with another stream; index-data ownership is not proven")));
            }
            self.scatter.push((id, lo, hi));
            return Ok(());
        }
        let nword = if mode == t.isa.mover.modes.transform as u64 {
            match self.walkers[0].xid {
                // xform_bank.v P_OUT_W: bypass emits four words per entry.
                0 => 4,
                1 => t.isa.mover.mx7_entry_words as u64,
                2 => t.isa.mover.gt4_words as u64,
                xid => {
                    return Err(format!(
                        "stream {id}: unknown transform {xid} write footprint"
                    ))
                }
            }
        } else {
            1
        };
        if mode == t.isa.mover.modes.transform as u64 {
            let src = &self.walkers[0];
            let input = if src.xid == 1 {
                t.isa.mover.f16_entry_words as u128
            } else {
                4
            };
            let count = |w: &Walker| {
                w.dims[..w.ndim]
                    .iter()
                    .map(|d| u128::from(d.count.max(1)))
                    .product::<u128>()
            };
            if input == 0
                || !count(src).is_multiple_of(input)
                || count(src) / input != count(dst)
                || src.windows.iter().any(|(_, extent)| *extent != 0)
            {
                return Err(format!(
                    "stream {id}: transform source entries do not prove the destination footprint"
                ));
            }
        }
        let mut points = vec![(dst.base as i128, [0i64; 2])];
        for d in &dst.dims[..dst.ndim] {
            if d.count == 0 || points.len().saturating_mul(d.count as usize) > LIMIT {
                return Err(format!(
                    "stream {id}: destination walk exceeds the static line-proof budget"
                ));
            }
            let mut next = Vec::new();
            for (at, axes) in points {
                for i in 0..d.count {
                    let mut a = axes;
                    if (1..=2).contains(&d.axis) {
                        a[d.axis - 1] += i as i64 * d.step;
                    }
                    next.push((at + i as i128 * d.stride as i128, a));
                }
            }
            points = next;
        }
        self.words = self
            .words
            .saturating_add(points.len().saturating_mul(nword as usize));
        if self.words > LIMIT {
            return Err("package exceeds the static write-line proof budget".into());
        }
        let mut coverage: BTreeMap<u64, BTreeSet<u64>> = BTreeMap::new();
        for (at, axes) in points {
            if dst.windows.iter().zip(axes).any(|(&(base, extent), a)| {
                extent != 0 && !(0..extent as i64).contains(&(base + a))
            }) {
                continue;
            }
            let at = u64::try_from(at).map_err(|_| format!("stream {id}: negative destination"))?;
            if at % word != 0 {
                return Err(format!("stream {id}: destination is not word aligned"));
            }
            for k in 0..nword {
                let a = at.checked_add(k * word).ok_or("destination overflow")?;
                coverage.entry(a / line).or_default().insert(a % line);
            }
        }
        for (at, covered) in coverage {
            if covered.len() as u64 == line / word {
                continue;
            }
            if self.full_only {
                return Err("stream does not cover whole write lines".into());
            }
            if self.scatter.iter().any(|&(_, lo, hi)| lo <= at && at <= hi) && out.len() < 32 {
                out.push(fail(mesh, format!("stream {id}: partial line {:#x} may overlap a sub-line scatter; index-data ownership is not proven", at * line)));
            }
            if let Some(&other) = self.owners.get(&at) {
                if self.seen.insert((other, id)) && out.len() < 32 {
                    out.push(fail(mesh, format!("write line {:#x} ({line} bytes) is partially written by streams {other} and {id}", at * line)));
                }
            } else {
                self.owners.insert(at, id);
            }
        }
        Ok(())
    }
}

fn alu(op: u64, a: u64, b: u64) -> Option<u64> {
    Some(match op {
        0 => b,
        1 => a.wrapping_add(b),
        2 => a.wrapping_sub(b),
        3 => a.wrapping_mul(b),
        4 => a & b,
        5 => a | b,
        6 => a ^ b,
        7 => a.wrapping_shl((b & 63) as u32),
        8 => a >> (b & 63),
        9 => a.min(b),
        10 => a.max(b),
        11 => a.checked_div(b).unwrap_or(u64::MAX),
        12 => a.checked_rem(b).unwrap_or(a),
        _ => return None,
    })
}

/// Mandatory reliability admission, independent of optional checker gates.
pub fn admit(pkg: &Package, mesh: u32, t: &Target) -> Result<(), String> {
    if !t.facts.vector_mx7_allowed {
        let facts = ktpu_l0::Facts::of_mesh(&t.machine, mesh).map_err(|e| e.to_string())?;
        let lifted = ktpu_l1::lower::engine::lift(pkg, &facts).map_err(|e| e.to_string())?;
        crate::check::schedule(
            &crate::check::Nodes::from([(mesh, lifted)]),
            &crate::check::Held::new(),
            t,
        )
        .map_err(|e| format!("vector conversion admission: {:?}", e.findings))?;
    }
    if t.facts.mover_quant_allowed {
        return Ok(());
    }
    match execute(pkg, mesh, t, &mut Vec::new(), true) {
        Err(_) if dynamic::proves(pkg, t) => Ok(()),
        result => result,
    }
}

/// Check one bound package, independent of dispatch scheduling and waits.
/// Unknown address computations refuse rather than silently proving isolation.
pub fn check(pkg: &Package, mesh: u32, t: &Target) -> Vec<Finding> {
    if crate::mutant::on("verify.lines/off") {
        return Vec::new();
    }
    let mut out = Vec::new();
    if let Err(e) = admit(pkg, mesh, t) {
        return vec![fail(mesh, format!("conversion admission: {e}"))];
    }
    if let Err(e) = execute(pkg, mesh, t, &mut out, false) {
        if dynamic::proves(pkg, t) {
            return Vec::new();
        }
        out.push(fail(mesh, e));
    }
    out
}

/// Payload patches cannot introduce a mover step, but can change ENGINE codes.
/// Step patches keep the footprint unproven. RECV stores words to DRAM;
/// unlike PATCH it does not replace package payload or ENGINE code.
fn may_write(pkg: &Package) -> bool {
    if pkg
        .steps
        .iter()
        .any(|s| s.op == Op::Mover || (s.op == Op::Patch && s.arg & (1 << 24) != 0))
    {
        return true;
    }
    let mut codes = BTreeSet::new();
    for s in pkg.steps.iter().filter(|s| s.op == Op::Engine) {
        if s.count > LIMIT as u64 {
            return true;
        }
        for k in 0..s.count {
            let Some(i) = s
                .arg
                .checked_add(k / 3)
                .and_then(|i| usize::try_from(i).ok())
            else {
                return true;
            };
            let Some(w) = pkg.payloads.get(i) else {
                return true;
            };
            if (8..24).contains(&(w.get((k % 3 * 8) as u32, 8) & !64)) {
                return true;
            }
            codes.insert(i as u64);
        }
    }
    !codes.is_empty()
        && pkg
            .steps
            .iter()
            .any(|s| s.op == Op::Patch && codes.contains(&s.count) && s.arg & 255 < 24)
}

fn execute(
    pkg: &Package,
    mesh: u32,
    t: &Target,
    out: &mut Vec<Finding>,
    policy_only: bool,
) -> Result<(), String> {
    if !may_write(pkg) {
        return Ok(());
    }
    let mut payloads = pkg.payloads.clone();
    let mut unknown = vec![Word::ZERO; payloads.len()];
    let mut steps = pkg.steps.clone();
    let mut regs = [Some(0u64); 16];
    let mut mover = Mover {
        policy_only,
        ..Mover::default()
    };
    let (mut pc, mut budget) = (0usize, 0usize);
    while let Some(s) = steps.get(pc).copied() {
        budget += 1;
        if budget > LIMIT {
            return Err("node control flow exceeds the static line-proof budget".into());
        }
        let r = s.unit as usize;
        let a = regs[((s.count >> 8) & 15) as usize];
        let b = if s.flags & F_IMM != 0 {
            Some(s.arg)
        } else {
            regs[((s.count >> 12) & 15) as usize]
        };
        match s.op {
            Op::End => break,
            Op::Load => {
                *regs.get_mut(r).ok_or("invalid LOAD register")? = if s.flags == F_IMM {
                    let idx = s.arg as usize;
                    if unknown.get(idx).ok_or("LOAD payload out of bounds")?.0[0] != 0 {
                        None
                    } else {
                        Some(payloads[idx].0[0])
                    }
                } else {
                    None
                }
            }
            Op::Alu => {
                *regs.get_mut(r).ok_or("invalid ALU register")? =
                    a.zip(b).and_then(|(a, b)| alu(s.count & 255, a, b))
            }
            Op::Jump => {
                let go = if s.count & 255 == 0 {
                    true
                } else {
                    let (a, b) = a
                        .zip(b)
                        .ok_or("data-dependent node loop has no static write-line proof")?;
                    match s.count & 255 {
                        1 => a == b,
                        2 => a != b,
                        3 => a < b,
                        4 => a >= b,
                        5 => (a as i64) < (b as i64),
                        6 => (a as i64) >= (b as i64),
                        _ => return Err("invalid node branch".into()),
                    }
                };
                if go {
                    pc = r;
                    continue;
                }
            }
            Op::Patch => {
                let bit = (s.arg & 255) as u32;
                let width = ((s.arg >> 8) & 255) as u32;
                let shift = ((s.arg >> 16) & 255) as u32;
                if width == 0 || width > 64 || shift >= 64 || bit + width > 256 {
                    return Err("invalid PATCH field".into());
                }
                let is_step = s.arg & (1 << 24) != 0;
                let mut w = if is_step {
                    let st = steps
                        .get(s.count as usize)
                        .ok_or("PATCH step out of bounds")?;
                    Word([
                        st.op as u64 | (st.flags as u64) << 8 | st.unit << 16 | st.count << 32,
                        st.arg,
                        0,
                        0,
                    ])
                } else {
                    *payloads
                        .get(s.count as usize)
                        .ok_or("PATCH payload out of bounds")?
                };
                let mut v = regs.get(r).copied().flatten().map(|v| v >> shift);
                if !is_step
                    && s.arg & (1 << 26) != 0
                    && unknown[s.count as usize].get(bit, width) != 0
                {
                    v = None;
                }
                if let Some(v) = v {
                    let v = if s.arg & (1 << 26) != 0 {
                        w.get(bit, width).wrapping_add(v)
                    } else {
                        v
                    };
                    w.set(bit, width, v);
                }
                if is_step {
                    if v.is_none() {
                        return Err("data-dependent step patch has no write-line proof".into());
                    }
                    steps[s.count as usize] = ktpu_l0::Step::new(
                        Op::from_code(w.0[0] & 255).ok_or("invalid patched opcode")?,
                        (w.0[0] >> 16) & 0xffff,
                        w.0[0] >> 32,
                        w.0[1],
                        (w.0[0] >> 8) as u8,
                    );
                } else {
                    payloads[s.count as usize] = w;
                    unknown[s.count as usize].set(
                        bit,
                        width,
                        if v.is_none() { u64::MAX } else { 0 },
                    );
                }
            }
            // Firmware recv() copies RX words with CPU st64 to the address in
            // payload[arg]. It changes neither mover state nor package payload.
            // A later LOAD from DRAM remains unknown above, including RX data.
            Op::Recv => {
                if crate::mutant::on("verify.lines/recv-is-payload") {
                    return Err("received payload data has no static write-line proof".into());
                }
            }
            Op::Mover => {
                for k in 0..s.count {
                    let i = (s.arg + k) as usize;
                    let w = payloads.get(i).ok_or("MOVER payload out of bounds")?;
                    for half in [0, 128] {
                        let reg = w.limb(half);
                        if reg == MOVER_SKIP {
                            continue;
                        }
                        if unknown[i].limb(half) != 0
                            || (unknown[i].limb(half + 64) != 0 && !matches!(reg, 0x38 | 0x40))
                        {
                            return Err(
                                "data-dependent mover address has no write-line proof".into()
                            );
                        }
                        mover.put(reg, w.limb(half + 64), t, mesh, out)?;
                    }
                }
            }
            Op::Engine => {
                for k in 0..s.count {
                    let i = (s.arg + k / 3) as usize;
                    let w = payloads.get(i).ok_or("ENGINE payload out of bounds")?;
                    let j = (k % 3) as u32;
                    let code = w.get(8 * j, 8);
                    if unknown[i].get(8 * j, 8) != 0 {
                        return Err("data-dependent ENGINE opcode has no write-line proof".into());
                    }
                    if code & 64 != 0 && (8..24).contains(&(code & !64)) {
                        return Err(
                            "relative ENGINE mover entry has no static write-line proof".into()
                        );
                    }
                    if (8..24).contains(&code) {
                        if unknown[i].limb(64 * (j + 1)) != 0 {
                            return Err(
                                "data-dependent ENGINE write has no write-line proof".into()
                            );
                        }
                        mover.put((code - 8) * 8, w.limb(64 * (j + 1)), t, mesh, out)?;
                    }
                }
            }
            _ => {}
        }
        pc += 1;
    }
    Ok(())
}
