//! Memory-mover moves as register writes, for a package's MOVER step
//! (control-registers.md s3, transform-slot.md).
//!
//! A walker is a [`Walk`]: `dims` is one level per entry, outermost first,
//! strides in bytes and whole memory words (`die.word_bytes`); a level may
//! step bound axis 1 or 2.
//! `windows` bounds axes 1 and 2 as `(base, extent)`; an extent of 0 disables
//! the axis.

use std::collections::BTreeMap;

use crate::build::AddrField;
use crate::error::Result;
use crate::facts::Facts;
use crate::refuse;
use crate::word::{mask64, Word, WordOps};

pub const R_CTRL: u64 = 0x00;
pub const R_HDR: u64 = 0x10;
pub const R_DIM: u64 = 0x18;
pub const R_AXIS: u64 = 0x20;
pub const R_WINDOW: u64 = 0x28;
pub const R_INDEX: u64 = 0x30;
pub const R_SEED: u64 = 0x38;
pub const R_IMM: u64 = 0x40;
pub const R_ROW: u64 = 0x50;

pub const COPY: u64 = 0;
pub const GATHER: u64 = 2;
pub const GENERATE: u64 = 3;
pub const FILL: u64 = 4;
pub const XFORM: u64 = 5;
pub const SCATTER: u64 = 6;

pub const W8: u64 = 0;
pub const W16: u64 = 1;
pub const W32: u64 = 2;

/// flags[3]: coalesce writes into bursts.
pub const FLAG_WCOAL: u64 = 1 << 3;
/// flags[7:5]: a burst cap of 2^(n-1) beats, 0 the engine's own maximum.
pub const BURST_LSB: u32 = 5;
pub const GO: u64 = 1 << 16;
/// CTRL[17]: the index list holds fp32 values.
pub const INDEX_FP: u64 = 1 << 17;
/// Words one move carries at most ([`ktpu_isa::unit::mover::MOVE_WORDS`]).
pub const MOVE_WORDS: u64 = ktpu_isa::unit::mover::MOVE_WORDS;
/// Header bits: base at [43:4], dimensions at [46:44], transform id [50:47],
/// transform mode [58:55].
pub const BASE_LSB: u32 = 4;
pub const NDIM_LSB: u32 = 44;
pub const XID_LSB: u32 = 47;
pub const XMODE_LSB: u32 = 55;

/// `(register, value)` writes.
pub type Writes = Vec<(u64, u64)>;

fn fits(name: &str, value: i128, bits: u32, signed: bool) -> Result<u64> {
    let (lo, hi) = if signed {
        (-(1i128 << (bits - 1)), 1i128 << (bits - 1))
    } else {
        (0, 1i128 << bits)
    };
    if !(lo..hi).contains(&value) {
        return refuse!(
            "L0.STEP.024",
            "mover {name} {value:#x} does not fit {bits} bits"
        );
    }
    Ok(value as u64 & mask64(bits))
}

/// One walker level: `count` steps of `stride` bytes; `axis` 1 or 2 is stepped
/// by `astep` each step (0: no bound axis).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Dim {
    pub count: i128,
    pub stride: i128,
    pub axis: u64,
    pub astep: i128,
}

impl Dim {
    pub fn new(count: i128, stride: i128) -> Dim {
        Dim {
            count,
            stride,
            axis: 0,
            astep: 0,
        }
    }
}

/// One walker: a base, its levels outermost first, two bound windows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Walk {
    pub base: i128,
    pub dims: Vec<Dim>,
    pub windows: Vec<(i128, i128)>,
}

impl Walk {
    pub fn new(base: i128, dims: Vec<Dim>) -> Walk {
        Walk {
            base,
            dims,
            windows: vec![(0, 0), (0, 0)],
        }
    }

    pub fn words(&self) -> i128 {
        self.dims.iter().map(|d| d.count).product()
    }
}

/// The writes that load one walker: header, each dimension (staged by 0x18,
/// latched by 0x20), both windows (an omitted extent inherits the previous
/// move's).
pub fn walker(f: &Facts, sel: u64, w: &Walk, xform_id: i128, xform_mode: i128) -> Result<Writes> {
    if w.dims.is_empty() || w.dims.len() > f.mover_levels {
        return refuse!(
            "L0.STEP.024",
            "a walker has 1..{} dimensions, got {}",
            f.mover_levels,
            w.dims.len()
        );
    }
    let word = f.word_bytes as i128;
    if w.base.rem_euclid(word) != 0 || w.dims.iter().any(|d| d.stride.rem_euclid(word) != 0) {
        return refuse!(
            "L0.STEP.024",
            "mover addresses and strides are whole {word}-byte words"
        );
    }
    let mut out = vec![(
        R_HDR,
        sel | fits("base", w.base, f.addr_bits, false)? << BASE_LSB
            | (w.dims.len() as u64) << NDIM_LSB
            | fits("transform id", xform_id, 4, false)? << XID_LSB
            | fits("transform mode", xform_mode, 4, false)? << XMODE_LSB,
    )];
    for (at, d) in w.dims.iter().enumerate() {
        if d.axis > 2 {
            return refuse!(
                "L0.STEP.024",
                "a level binds axis 0 (none), 1 or 2, not {}",
                d.axis
            );
        }
        out.push((
            R_DIM,
            sel | (at as u64) << 1
                | fits("count", d.count, 16, false)? << 4
                | fits("stride", d.stride, 32, true)? << 20,
        ));
        out.push((R_AXIS, d.axis | fits("axis step", d.astep, 16, true)? << 2));
    }
    if w.windows.len() != 2 {
        return refuse!("L0.STEP.024", "a walker has two windows, for axes 1 and 2");
    }
    for (axis, &(abase, extent)) in w.windows.iter().enumerate() {
        out.push((
            R_WINDOW,
            sel | (axis as u64) << 1
                | fits("window base", abase, 16, true)? << 2
                | fits("window extent", extent, 16, false)? << 18,
        ));
    }
    Ok(out)
}

/// The flags byte: write coalescing, and a burst cap of 2^(burst-1) beats.
pub fn flag_byte(wcoal: bool, burst: i128) -> Result<u64> {
    Ok(if wcoal { FLAG_WCOAL } else { 0 } | fits("burst cap", burst, 3, false)? << BURST_LSB)
}

/// One move. A FILL or GENERATE has no source; `imm` is the fill value and
/// the padding, `seed` the generator's; `index` is `(base, count)` and `row`
/// `(pitch, words)` for a gather or scatter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Move {
    pub mode: i128,
    pub src: Option<Walk>,
    pub dst: Option<Walk>,
    pub ewidth: u64,
    pub flags: u64,
    pub xform: (i128, i128),
    pub imm: Option<i128>,
    pub seed: Option<i128>,
    pub index: Option<(i128, i128)>,
    pub row: Option<(i128, i128)>,
    pub fp: bool,
}

impl Move {
    pub fn new(mode: u64) -> Move {
        Move {
            mode: mode as i128,
            src: None,
            dst: None,
            ewidth: W32,
            flags: FLAG_WCOAL,
            xform: (0, 0),
            imm: None,
            seed: None,
            index: None,
            row: None,
            fp: false,
        }
    }

    /// The move's writes, GO last.
    pub fn writes(&self, f: &Facts) -> Result<Writes> {
        let mut out = Vec::new();
        if let Some(src) = &self.src {
            out.extend(walker(f, 0, src, self.xform.0, self.xform.1)?);
        }
        if let Some(dst) = &self.dst {
            out.extend(walker(f, 1, dst, 0, 0)?);
        }
        if let Some((base, count)) = self.index {
            if base.rem_euclid(f.word_bytes as i128) != 0
                || !(1..=f.mover_max_index as i128).contains(&count)
            {
                return refuse!("L0.STEP.024", "an index list of {count} at {base:#x}");
            }
            out.push((
                R_INDEX,
                fits("index base", base, f.addr_bits, false)? | (count as u64) << 40,
            ));
        }
        if let Some((pitch, words)) = self.row {
            out.push((
                R_ROW,
                fits("pitch", pitch, 32, false)? | fits("row words", words, 16, false)? << 32,
            ));
        }
        if let Some(seed) = self.seed {
            out.push((R_SEED, fits("seed", seed, 64, false)?));
        }
        if let Some(imm) = self.imm {
            out.push((R_IMM, fits("immediate", imm, 32, false)?));
        }
        if self.ewidth > W32 {
            return refuse!(
                "L0.STEP.024",
                "element width {} is not 8, 16 or 32 bits",
                self.ewidth
            );
        }
        let ctl =
            fits("mode", self.mode, 3, false)? | self.ewidth << 3 | (self.flags & 0xFF) << 8 | GO;
        out.push((R_CTRL, ctl | if self.fp { INDEX_FP } else { 0 }));
        Ok(out)
    }
}

fn contiguous(base: i128, n: i128, stride: i128) -> Walk {
    Walk::new(base, vec![Dim::new(n, stride)])
}

/// `nbytes` (whole words) from `src` to `dst`, contiguous; more than
/// [`MOVE_WORDS`] is several moves.
pub fn copy(f: &Facts, src: i128, dst: i128, nbytes: i128) -> Result<Writes> {
    let wb = f.word_bytes as i128;
    let words = nbytes.div_euclid(wb);
    let step = MOVE_WORDS as i128;
    let mut out = Vec::new();
    let mut at = 0;
    while at < words {
        let n = step.min(words - at);
        let off = at * wb;
        let mut m = Move::new(COPY);
        m.src = Some(contiguous(src + off, n, wb));
        m.dst = Some(contiguous(dst + off, n, wb));
        out.extend(m.writes(f)?);
        at += step;
    }
    Ok(out)
}

/// A converting move: `entries` source entries of `in_words` words each,
/// through transform `xform_id`, each written as `out_words` words. The
/// source walker defines the iteration space; the destination steps once per
/// entry. More than [`MOVE_WORDS`] source words is several moves.
#[allow(clippy::too_many_arguments)]
pub fn convert(
    f: &Facts,
    src: i128,
    dst: i128,
    entries: i128,
    xform_id: i128,
    mode: i128,
    in_words: i128,
    out_words: i128,
) -> Result<Writes> {
    let step = MOVE_WORDS as i128 / in_words;
    let wb = f.word_bytes as i128;
    let mut out = Vec::new();
    let mut at = 0;
    while at < entries {
        let n = step.min(entries - at);
        let mut m = Move::new(XFORM);
        m.src = Some(contiguous(src + at * in_words * wb, n * in_words, wb));
        m.dst = Some(contiguous(dst + at * out_words * wb, n, out_words * wb));
        m.ewidth = W16;
        m.xform = (xform_id, mode);
        out.extend(m.writes(f)?);
        at += step;
    }
    Ok(out)
}

/// A converting move over explicit walkers.
pub fn convert_walk(f: &Facts, src: Walk, dst: Walk, xform_id: i128, mode: i128) -> Result<Writes> {
    let mut m = Move::new(XFORM);
    m.src = Some(src);
    m.dst = Some(dst);
    m.ewidth = W16;
    m.xform = (xform_id, mode);
    m.writes(f)
}

/// `value` written over `dst`'s walk, reading nothing.
pub fn fill(f: &Facts, dst: Walk, value: i128, ewidth: u64) -> Result<Writes> {
    let mut m = Move::new(FILL);
    m.dst = Some(dst);
    m.ewidth = ewidth;
    m.imm = Some(value);
    m.writes(f)
}

/// Pseudo-random words over `dst`'s walk, keyed by `seed` and each word's
/// destination address.
pub fn generate(f: &Facts, dst: Walk, seed: i128, ewidth: u64) -> Result<Writes> {
    let mut m = Move::new(GENERATE);
    m.dst = Some(dst);
    m.ewidth = ewidth;
    m.seed = Some(seed);
    m.writes(f)
}

/// Row r of `dst`'s walk := the `row.1` words at `src_base + index[r] * row.0`.
pub fn gather(
    f: &Facts,
    src_base: i128,
    index: (i128, i128),
    dst: Walk,
    row: (i128, i128),
    fp: bool,
) -> Result<Writes> {
    let mut m = Move::new(GATHER);
    m.src = Some(contiguous(src_base, 1, f.word_bytes as i128));
    m.dst = Some(dst);
    m.index = Some(index);
    m.row = Some(row);
    m.fp = fp;
    m.writes(f)
}

/// Row r of `src`'s walk -> the `row.1` words at `dst_base + index[r] * row.0`.
pub fn scatter(
    f: &Facts,
    src: Walk,
    dst_base: i128,
    index: (i128, i128),
    row: (i128, i128),
    fp: bool,
) -> Result<Writes> {
    let mut m = Move::new(SCATTER);
    m.src = Some(src);
    m.dst = Some(contiguous(dst_base, 1, f.word_bytes as i128));
    m.index = Some(index);
    m.row = Some(row);
    m.fp = fp;
    m.writes(f)
}

/// `{(register, name): (write index, bit in the value, width)}` of the fields
/// a register expression may set in `writes`; the last write of each names it.
pub fn fields(f: &Facts, writes: &[(u64, u64)]) -> BTreeMap<(u64, String), (usize, u32, u32)> {
    let mut out = BTreeMap::new();
    let side = |v: u64| if v & 1 != 0 { "dst" } else { "src" };
    for (i, &(reg, value)) in writes.iter().enumerate() {
        let named: &[(&str, u32, u32)] = match reg {
            R_HDR => &[("base", BASE_LSB, 0)],
            R_INDEX => &[("index", 0, 0), ("count", 40, 16)],
            R_ROW => &[("pitch", 0, 32), ("words", 32, 16)],
            R_SEED => &[("seed", 0, 64)],
            R_IMM => &[("imm", 0, 32)],
            _ => &[],
        };
        for &(name, bit, width) in named {
            let width = if width == 0 { f.addr_bits } else { width };
            let key = if reg == R_HDR {
                format!("{}.{name}", side(value))
            } else {
                name.to_string()
            };
            out.insert((reg, key), (i, bit, width));
        }
        if reg == R_DIM {
            out.insert(
                (reg, format!("{}.count{}", side(value), (value >> 1) & 7)),
                (i, 4, 16),
            );
        }
    }
    out
}

/// Address fields of one MOVER payload (two register/value pairs): every
/// walker header's base and an index list's.
pub fn addresses(f: &Facts, word: &Word) -> Vec<AddrField> {
    let mut out = Vec::new();
    for half in [0u32, 128] {
        let lsb = match word.limb(half) {
            R_HDR => BASE_LSB,
            R_INDEX => 0,
            _ => continue,
        };
        let bit = half + 64 + lsb;
        out.push(AddrField {
            segments: vec![(bit, f.addr_bits, 0)],
            value: word.get(bit, f.addr_bits),
        });
    }
    out
}
