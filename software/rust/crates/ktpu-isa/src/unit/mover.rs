//! The node's memory mover: one move as its 64-bit command-register writes
//! (`docs/spec/stack/isa/mover.md`, RTL `mm_mover.v`, `mx_tdesc.v`;
//! `kohakuaccel/compiler/package/mover.py`, `kohakutpu/compiler/encode/mover.py`).
//!
//! A walker is a [`Walk`]: a base, one [`Level`] per level OUTERMOST FIRST
//! (strides in bytes, whole words), and two windows bounding axes 1 and 2 as
//! `(base, extent)`; an extent of 0 disables the axis.

use crate::layout::{mask, W256};
use crate::reloc::{AddressField, Segment};
use ktpu_machine::{refuse, Machine, Refusal};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

pub const R_CTRL: u32 = 0x00;
pub const R_HDR: u32 = 0x10;
pub const R_DIM: u32 = 0x18;
pub const R_AXIS: u32 = 0x20;
pub const R_WINDOW: u32 = 0x28;
pub const R_INDEX: u32 = 0x30;
pub const R_SEED: u32 = 0x38;
pub const R_IMM: u32 = 0x40;
pub const R_ROW: u32 = 0x50;

/// Element widths of the immediate.
pub const W8: u32 = 0;
pub const W16: u32 = 1;
pub const W32: u32 = 2;
/// flags[3]: coalesce writes into bursts.
pub const FLAG_WCOAL: u32 = 1 << 3;
/// flags[7:5]: a burst cap of 2^(n-1) beats, 0 the engine's own maximum.
pub const BURST_LSB: u32 = 5;
pub const GO: u64 = 1 << 16;
/// CTRL[17]: the index list holds fp32 values.
pub const INDEX_FP: u64 = 1 << 17;
/// Words one move carries at most (the stack's move bound, ISA.MV.007): a
/// MOVER step's wait is bounded by progress, so one move must finish inside
/// the node's timeout at the slowest transform's rate.
pub const MOVE_WORDS: u64 = 1 << 14;
/// HDR bits: base at [43:4], level count [46:44], transform id [50:47],
/// transform mode [58:55].
pub const BASE_LSB: u32 = 4;
pub const LEVELS_LSB: u32 = 44;
pub const XID_LSB: u32 = 47;
pub const XMODE_LSB: u32 = 55;

/// Transform ids of KohakuTPU's slot bank (`xform_bank.v`).
pub const XF_BYPASS: u32 = 0;
pub const XF_QUANT: u32 = 1;
pub const XF_GT4: u32 = 2;

/// One command-register write: (register offset, 64-bit value).
pub type RegWrite = (u32, u64);
/// A patchable field: (register, name such as `src.base`).
pub type FieldKey = (u32, String);
/// Where it sits: (write index, bit in the value, width).
pub type FieldAt = (usize, u32, u32);

/// The machine facts the mover encoding depends on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MoverIsa {
    pub addr_bits: u32,
    pub word_bytes: u64,
    pub levels: usize,
    pub max_index: u32,
    pub modes: ktpu_machine::MoverModes,
    /// Words one move carries at most ([`MOVE_WORDS`]).
    pub move_words: u64,
    /// CTRL element-width codes of W8, W16, W32 (ISA.MV.041).
    pub ewidths: [u32; 3],
    /// Words of a quantiser entry in (lanes x values FP16) and out (a word a
    /// lane), and of a GT4 group (`mover.xform`).
    pub f16_entry_words: i64,
    pub mx7_entry_words: i64,
    pub gt4_words: i64,
}

/// A move's mode; its CTRL code is the machine's (`mover.modes`).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MoveMode {
    Copy,
    Transpose,
    Gather,
    Generate,
    Fill,
    Xform,
    Scatter,
}

/// One walker level: `count` elements `stride` bytes apart, stepping bound
/// axis `axis` (0 none, 1, 2) by `astep`. Written `[count, stride]` or
/// `[count, stride, axis, astep]`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Level {
    pub count: i64,
    pub stride: i64,
    pub axis: i64,
    pub astep: i64,
}

impl Level {
    pub fn new(count: i64, stride: i64) -> Level {
        Level {
            count,
            stride,
            axis: 0,
            astep: 0,
        }
    }
}

impl Serialize for Level {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        if self.axis == 0 && self.astep == 0 {
            [self.count, self.stride].serialize(s)
        } else {
            [self.count, self.stride, self.axis, self.astep].serialize(s)
        }
    }
}

impl<'de> Deserialize<'de> for Level {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Level, D::Error> {
        match Vec::<i64>::deserialize(d)?.as_slice() {
            [c, s] => Ok(Level::new(*c, *s)),
            [c, s, a, t] => Ok(Level {
                count: *c,
                stride: *s,
                axis: *a,
                astep: *t,
            }),
            other => Err(serde::de::Error::custom(format!(
                "a level is [count, stride] or [count, stride, axis, astep], not {other:?}"
            ))),
        }
    }
}

/// One walker.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Walk {
    pub base: i64,
    pub dims: Vec<Level>,
    #[serde(default)]
    pub windows: [(i64, i64); 2],
}

impl Walk {
    /// A walker without bound windows.
    pub fn new(base: i64, dims: &[(i64, i64)]) -> Walk {
        Walk {
            base,
            dims: dims.iter().map(|(c, s)| Level::new(*c, *s)).collect(),
            windows: [(0, 0); 2],
        }
    }

    /// Elements the walk visits.
    pub fn words(&self) -> i64 {
        self.dims.iter().map(|d| d.count).product()
    }
}

/// Every field of one move ([`MoverIsa::move_writes`]).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Move {
    pub mode: MoveMode,
    #[serde(default)]
    pub src: Option<Walk>,
    #[serde(default)]
    pub dst: Option<Walk>,
    #[serde(default = "w32")]
    pub ewidth: u32,
    #[serde(default = "wcoal")]
    pub flags: u32,
    /// Transform (id, mode) on the source header.
    #[serde(default)]
    pub xform: (i64, i64),
    #[serde(default)]
    pub imm: Option<i64>,
    #[serde(default)]
    pub seed: Option<u64>,
    /// Index list (base, count).
    #[serde(default)]
    pub index: Option<(i64, i64)>,
    /// Gather / scatter rows (pitch, words).
    #[serde(default)]
    pub row: Option<(i64, i64)>,
    #[serde(default)]
    pub fp: bool,
}

fn w32() -> u32 {
    W32
}
fn wcoal() -> u32 {
    FLAG_WCOAL
}
fn quant() -> i64 {
    XF_QUANT as i64
}

/// One mover operation, as the stack writes it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum MoverOp {
    Move(Move),
    /// `nbytes` (whole words) from `src` to `dst`, contiguous; more than
    /// [`MOVE_WORDS`] is several moves.
    Copy {
        src: i64,
        dst: i64,
        nbytes: i64,
    },
    /// `entries` source entries of `in_words` words through transform
    /// `xform_id`, each written as `out_words` words; several moves past
    /// [`MOVE_WORDS`] source words.
    Convert {
        src: i64,
        dst: i64,
        entries: i64,
        #[serde(default = "quant")]
        xform_id: i64,
        #[serde(default)]
        mode: i64,
        /// The machine's FP16 entry words when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        in_words: Option<i64>,
        /// The machine's MXFP7 entry words when absent.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        out_words: Option<i64>,
    },
    /// A converting move over explicit walkers: `src` counts source words,
    /// `dst` steps once per entry.
    ConvertWalk {
        src: Walk,
        dst: Walk,
        #[serde(default = "quant")]
        xform_id: i64,
        #[serde(default)]
        mode: i64,
    },
    /// `value` over `dst`'s walk, replicated by `ewidth`.
    Fill {
        dst: Walk,
        #[serde(default)]
        value: i64,
        #[serde(default = "w32")]
        ewidth: u32,
    },
    /// Pseudo-random words keyed by `seed` and each word's address.
    Generate {
        dst: Walk,
        seed: u64,
        #[serde(default = "w32")]
        ewidth: u32,
    },
    /// Row r of `dst` := the `row.1` words at `src_base + index[r] * row.0`.
    Gather {
        src_base: i64,
        index: (i64, i64),
        dst: Walk,
        row: (i64, i64),
        #[serde(default)]
        fp: bool,
    },
    /// Row r of `src` -> the `row.1` words at `dst_base + index[r] * row.0`.
    Scatter {
        src: Walk,
        dst_base: i64,
        index: (i64, i64),
        row: (i64, i64),
        #[serde(default)]
        fp: bool,
    },
    /// `entries` FP16 entries (eight words each) to MXFP7, the B packing
    /// with `b`.
    Quantise {
        src: i64,
        dst: i64,
        entries: i64,
        #[serde(default)]
        b: bool,
    },
    /// `groups` groups of four words, each a 4 x 4 array of 64-bit granules,
    /// transposed.
    Transpose4 {
        src: i64,
        dst: i64,
        groups: i64,
    },
}

/// A transform id of the slot bank (QUANT and GT4 traded by a mutant); every
/// writer of a named transform resolves its id here.
pub fn xform_id(id: u32) -> i64 {
    match id {
        XF_QUANT
            if crate::mutant::on("isa.mover/xform") || crate::mutant::on("num.mover/quant-id") =>
        {
            XF_GT4 as i64
        }
        XF_GT4 if crate::mutant::on("isa.mover/xform") || crate::mutant::on("num.mover/gt4-id") => {
            XF_QUANT as i64
        }
        _ => id as i64,
    }
}

fn fits(clause: &str, name: &str, value: i64, bits: u32, signed: bool) -> Result<u64, Refusal> {
    let value = if crate::mutant::on("isa.mover/fields") {
        value
            + match name {
                "count" | "axis step" | "window base" | "window extent" => 1,
                "row words" | "immediate" => 1,
                "stride" | "pitch" | "index base" => 32,
                _ => 0,
            }
    } else {
        value
    };
    let (lo, hi) = if signed {
        (-(1i128 << (bits - 1)), 1i128 << (bits - 1))
    } else {
        (0, 1i128 << bits)
    };
    if !(lo..hi).contains(&(value as i128)) {
        return refuse(
            clause,
            format!("mover {name} {value:#x} does not fit {bits} bits"),
        );
    }
    Ok(value as u64 & mask(bits))
}

impl MoverIsa {
    pub fn new(m: &Machine) -> MoverIsa {
        let mut isa = MoverIsa::of(m);
        crate::mutant::mover(&mut isa);
        isa
    }

    /// The mover's encoding as `m` defines it, no mutant applied.
    pub fn of(m: &Machine) -> MoverIsa {
        let wb = m.die.word_bytes as i64;
        let xf = &m.mover.xform;
        MoverIsa {
            f16_entry_words: (xf.quant_lanes * xf.quant_values) as i64 * 2 / wb,
            mx7_entry_words: xf.quant_lanes as i64,
            gt4_words: xf.gt4_rows as i64,
            addr_bits: m.die.noc.addr_bits,
            word_bytes: m.die.word_bytes as u64,
            levels: m.mover.levels as usize,
            max_index: m.mover.max_index,
            modes: m.mover.modes.clone(),
            move_words: MOVE_WORDS,
            ewidths: [W8, W16, W32],
        }
    }

    /// Resolve a CTRL code against the machine's supported mode table.
    pub fn mode(&self, code: u64) -> Option<MoveMode> {
        let modes = [
            MoveMode::Copy,
            MoveMode::Transpose,
            MoveMode::Gather,
            MoveMode::Generate,
            MoveMode::Fill,
            MoveMode::Xform,
            MoveMode::Scatter,
        ];
        modes
            .into_iter()
            .find(|mode| self.mode_code(*mode) as u64 == code)
            .or_else(|| crate::mutant::on("rul.049/reserved-copy").then_some(MoveMode::Copy))
    }

    /// The machine's CTRL code of `mode`.
    pub fn mode_code(&self, mode: MoveMode) -> u32 {
        let m = &self.modes;
        match mode {
            MoveMode::Copy => m.copy,
            MoveMode::Transpose => m.transpose,
            MoveMode::Gather => m.gather,
            MoveMode::Generate => m.generate,
            MoveMode::Fill => m.fill,
            MoveMode::Xform => m.transform,
            MoveMode::Scatter => m.scatter,
        }
    }

    /// The writes that load one walker (`sel` 0 source, 1 destination):
    /// header, DIM then AXIS for each level, both windows (an omitted one
    /// keeps the previous move's).
    pub fn walker(&self, sel: u64, w: &Walk, xform: (i64, i64)) -> Result<Vec<RegWrite>, Refusal> {
        if !(1..=self.levels).contains(&w.dims.len()) {
            return refuse(
                "ISA.MV.003",
                format!(
                    "a walker has 1..{} dimensions, got {}",
                    self.levels,
                    w.dims.len()
                ),
            );
        }
        let wb = self.word_bytes as i64;
        if !crate::mutant::on("rul.050/unaligned")
            && (w.base % wb != 0 || w.dims.iter().any(|d| d.stride % wb != 0))
        {
            return refuse(
                "ISA.MV.023",
                "mover addresses and strides are whole 32-byte words",
            );
        }
        let (base_lsb, xid_lsb, xmode_lsb) = if crate::mutant::on("isa.mover/hdr") {
            (5, 48, 54)
        } else {
            (BASE_LSB, XID_LSB, XMODE_LSB)
        };
        let mut out = vec![(
            R_HDR,
            sel | fits("ISA.MV.014", "base", w.base, self.addr_bits, false)? << base_lsb
                | (w.dims.len() as u64) << LEVELS_LSB
                | fits("ISA.MV.014", "transform id", xform.0, 4, false)? << xid_lsb
                | fits("ISA.MV.014", "transform mode", xform.1, 4, false)? << xmode_lsb,
        )];
        for (at, d) in w.dims.iter().enumerate() {
            if !(0..=2).contains(&d.axis) {
                return refuse(
                    "ISA.MV.015",
                    format!("a level binds axis 0 (none), 1 or 2, not {}", d.axis),
                );
            }
            out.push((
                R_DIM,
                sel | (at as u64) << 1
                    | fits("ISA.MV.015", "count", d.count, 16, false)? << 4
                    | fits("ISA.MV.015", "stride", d.stride, 32, true)? << 20,
            ));
            out.push((
                R_AXIS,
                d.axis as u64 | fits("ISA.MV.015", "axis step", d.astep, 16, true)? << 2,
            ));
        }
        for (axis, (abase, extent)) in w.windows.iter().enumerate() {
            out.push((
                R_WINDOW,
                sel | (axis as u64) << 1
                    | fits("ISA.MV.016", "window base", *abase, 16, true)? << 2
                    | fits("ISA.MV.016", "window extent", *extent, 16, false)? << 18,
            ));
        }
        Ok(out)
    }

    /// The flags byte: write coalescing, and a burst cap of 2^(burst-1)
    /// beats (0: the engine's own maximum).
    pub fn flag_byte(&self, wcoal: bool, burst: i64) -> Result<u32, Refusal> {
        let (flag, lsb) = if crate::mutant::on("isa.mover/ctrl") {
            (1 << 2, 4)
        } else {
            (FLAG_WCOAL, BURST_LSB)
        };
        Ok((if wcoal { flag } else { 0 })
            | (fits("ISA.MV.042", "burst cap", burst, 3, false)? as u32) << lsb)
    }

    /// One move, GO last.
    pub fn move_writes(&self, mv: &Move) -> Result<Vec<RegWrite>, Refusal> {
        let mut out = Vec::new();
        if let Some(src) = &mv.src {
            out.extend(self.walker(0, src, mv.xform)?);
        }
        if let Some(dst) = &mv.dst {
            out.extend(self.walker(1, dst, (0, 0))?);
        }
        if let Some((base, count)) = mv.index {
            if !crate::mutant::on("rul.050/unaligned") && base % self.word_bytes as i64 != 0 {
                return refuse("ISA.MV.054", format!("an index list at {base:#x}"));
            }
            if !(1..=self.max_index as i64).contains(&count) {
                return refuse(
                    "ISA.MV.053",
                    format!("an index list of {count} at {base:#x}"),
                );
            }
            out.push((
                R_INDEX,
                fits("ISA.MV.017", "index base", base, self.addr_bits, false)?
                    | (count as u64) << 40,
            ));
        }
        if let Some((pitch, words)) = mv.row {
            if !crate::mutant::on("rul.050/unaligned") && pitch % self.word_bytes as i64 != 0 {
                return refuse(
                    "RUL.050",
                    format!("a row pitch of {pitch} bytes is not whole words"),
                );
            }
            out.push((
                R_ROW,
                fits("ISA.MV.017", "pitch", pitch, 32, false)?
                    | fits("ISA.MV.017", "row words", words, 16, false)? << 32,
            ));
        }
        if let Some(seed) = mv.seed {
            out.push((
                R_SEED,
                seed.wrapping_add(crate::mutant::on("isa.mover/fields") as u64),
            ));
        }
        if let Some(imm) = mv.imm {
            out.push((R_IMM, fits("ISA.MV.017", "immediate", imm, 32, false)?));
        }
        if !self.ewidths.contains(&mv.ewidth) {
            return refuse(
                "ISA.MV.041",
                format!("element width {} is not 8, 16 or 32 bits", mv.ewidth),
            );
        }
        let ctl = fits(
            "ISA.MV.013",
            "mode",
            self.mode_code(mv.mode) as i64,
            3,
            false,
        )? | (mv.ewidth as u64) << 3
            | ((mv.flags & 0xFF) as u64) << 8;
        let (go, index_fp) = if crate::mutant::on("isa.mover/ctrl") {
            (1 << 15, 1 << 18)
        } else {
            (GO, INDEX_FP)
        };
        out.push((R_CTRL, ctl | go | if mv.fp { index_fp } else { 0 }));
        if crate::mutant::on("isa.mover/regs") {
            for (r, _) in out.iter_mut() {
                *r = match *r {
                    R_HDR => 0x18,
                    R_DIM => 0x10,
                    R_WINDOW => 0x30,
                    R_INDEX => 0x28,
                    R_IMM => 0x38,
                    R_SEED => 0x40,
                    R_ROW => 0x48,
                    R_AXIS => 0x08,
                    other => other,
                };
            }
        }
        Ok(out)
    }

    fn plain(&self, mode: MoveMode, src: Option<Walk>, dst: Option<Walk>) -> Move {
        Move {
            mode,
            src,
            dst,
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

    fn convert(
        &self,
        (src, dst, entries): (i64, i64, i64),
        xform: (i64, i64),
        in_words: i64,
        out_words: i64,
    ) -> Result<Vec<RegWrite>, Refusal> {
        let wb = self.word_bytes as i64;
        let step = self.move_words as i64 / in_words;
        let mut out = Vec::new();
        let mut at = 0;
        while at < entries {
            let n = step.min(entries - at);
            let mut mv = self.plain(
                MoveMode::Xform,
                Some(Walk::new(src + at * in_words * wb, &[(n * in_words, wb)])),
                Some(Walk::new(dst + at * out_words * wb, &[(n, out_words * wb)])),
            );
            mv.ewidth = self.ewidths[1];
            mv.xform = xform;
            out.extend(self.move_writes(&mv)?);
            at += step;
        }
        Ok(out)
    }

    /// The register writes of `op`.
    pub fn writes(&self, op: &MoverOp) -> Result<Vec<RegWrite>, Refusal> {
        let wb = self.word_bytes as i64;
        match op {
            MoverOp::Move(mv) => self.move_writes(mv),
            MoverOp::Copy { src, dst, nbytes } => {
                let words = nbytes.div_euclid(wb);
                let mut out = Vec::new();
                let mut at = 0;
                while at < words {
                    let n = (self.move_words as i64).min(words - at);
                    let off = at * wb;
                    out.extend(self.move_writes(&self.plain(
                        MoveMode::Copy,
                        Some(Walk::new(src + off, &[(n, wb)])),
                        Some(Walk::new(dst + off, &[(n, wb)])),
                    ))?);
                    at += self.move_words as i64;
                }
                Ok(out)
            }
            MoverOp::Convert {
                src,
                dst,
                entries,
                xform_id,
                mode,
                in_words,
                out_words,
            } => {
                let in_words = in_words.unwrap_or(self.f16_entry_words);
                if in_words < 1 {
                    return refuse("ISA.MV.061", format!("an entry of {in_words} source words"));
                }
                self.convert(
                    (*src, *dst, *entries),
                    (*xform_id, *mode),
                    in_words,
                    out_words.unwrap_or(self.mx7_entry_words),
                )
            }
            MoverOp::ConvertWalk {
                src,
                dst,
                xform_id,
                mode,
            } => {
                let mut mv = self.plain(MoveMode::Xform, Some(src.clone()), Some(dst.clone()));
                mv.ewidth = self.ewidths[1];
                mv.xform = (*xform_id, *mode);
                self.move_writes(&mv)
            }
            MoverOp::Fill { dst, value, ewidth } => {
                let mut mv = self.plain(MoveMode::Fill, None, Some(dst.clone()));
                mv.ewidth = *ewidth;
                mv.imm = Some(*value);
                self.move_writes(&mv)
            }
            MoverOp::Generate { dst, seed, ewidth } => {
                let mut mv = self.plain(MoveMode::Generate, None, Some(dst.clone()));
                mv.ewidth = *ewidth;
                mv.seed = Some(*seed);
                self.move_writes(&mv)
            }
            MoverOp::Gather {
                src_base,
                index,
                dst,
                row,
                fp,
            } => {
                let mut mv = self.plain(
                    MoveMode::Gather,
                    Some(Walk::new(*src_base, &[(1, wb)])),
                    Some(dst.clone()),
                );
                mv.index = Some(*index);
                mv.row = Some(*row);
                mv.fp = *fp;
                self.move_writes(&mv)
            }
            MoverOp::Scatter {
                src,
                dst_base,
                index,
                row,
                fp,
            } => {
                let mut mv = self.plain(
                    MoveMode::Scatter,
                    Some(src.clone()),
                    Some(Walk::new(*dst_base, &[(1, wb)])),
                );
                mv.index = Some(*index);
                mv.row = Some(*row);
                mv.fp = *fp;
                self.move_writes(&mv)
            }
            MoverOp::Quantise {
                src,
                dst,
                entries,
                b,
            } => self.convert(
                (*src, *dst, *entries),
                (
                    xform_id(XF_QUANT),
                    (*b && !crate::mutant::on("num.mover/quant-b-dropped")) as i64,
                ),
                self.f16_entry_words,
                self.mx7_entry_words,
            ),
            MoverOp::Transpose4 { src, dst, groups } => self.convert(
                (*src, *dst, *groups),
                (xform_id(XF_GT4), 0),
                self.gt4_words,
                self.gt4_words,
            ),
        }
    }

    /// `(register, field) -> (write index, bit in the value, width)` of the
    /// fields a register expression may set in `writes`: bases, counts,
    /// index base and count, pitch, row words, seed, immediate. The last
    /// write of each register names it.
    pub fn fields(&self, writes: &[RegWrite]) -> Vec<(FieldKey, FieldAt)> {
        let mut out: Vec<(FieldKey, FieldAt)> = Vec::new();
        let mut put = |key: FieldKey, v: FieldAt| match out.iter_mut().find(|(k, _)| *k == key) {
            Some(slot) => slot.1 = v,
            None => out.push((key, v)),
        };
        for (i, (reg, value)) in writes.iter().enumerate() {
            let side = if value & 1 == 1 { "dst" } else { "src" };
            let names: &[(&str, u32, u32)] = match *reg {
                R_HDR => &[("base", BASE_LSB, 0)],
                R_INDEX => &[("index", 0, 0), ("count", 40, 16)],
                R_ROW => &[("pitch", 0, 32), ("words", 32, 16)],
                R_SEED => &[("seed", 0, 64)],
                R_IMM => &[("imm", 0, 32)],
                _ => &[],
            };
            for (name, bit, width) in names {
                let width = if *width == 0 { self.addr_bits } else { *width };
                let key = if *reg == R_HDR {
                    format!("{side}.{name}")
                } else {
                    name.to_string()
                };
                put((*reg, key), (i, *bit, width));
            }
            if *reg == R_DIM {
                put(
                    (*reg, format!("{side}.count{}", (value >> 1) & 7)),
                    (i, 4, 16),
                );
            }
        }
        out
    }

    /// The address fields of one MOVER payload (two register / value pairs):
    /// every walker header's base and an index list's base.
    pub fn addresses(&self, word: W256) -> Vec<AddressField> {
        let mut out = Vec::new();
        for half in [0u32, 128] {
            let reg = word.get(half, 64);
            let lsb = match reg {
                r if r == R_HDR as u64 => BASE_LSB,
                r if r == R_INDEX as u64 => 0,
                _ => continue,
            };
            let bit = half + 64 + lsb;
            out.push(AddressField {
                segments: vec![Segment {
                    bit,
                    width: self.addr_bits,
                    shift: 0,
                }],
                value: word.get(bit, self.addr_bits),
            });
        }
        out
    }
}
