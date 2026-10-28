//! The V2 vector core (`VC`): the three CU instructions that load and start
//! it (IMEM, DESC, RUN; `docs/spec/stack/isa/vector.md` s3) and its 32-bit
//! instruction words ([`core`]).

pub mod core;

use crate::layout::{log2_exact, mask, value, Decoded, Field, Format, InstSet, W256};
use crate::reloc::{AddressField, Segment};
use ktpu_machine::{refuse, Machine, Refusal};
use serde::{Deserialize, Serialize};

pub use self::core::{e8m15, Chunk, CoreFormats, MathOp, PtrReg, Src, StrideReg, VWord, WalkMode};

pub const OP_IMEM: u64 = 1;
pub const OP_DESC: u64 = 2;
pub const OP_RUN: u64 = 3;

const OP_BITS: u32 = 4;
/// IMEM's instruction word bits.
pub const WORD_BITS: u32 = 32;
/// A wide core's word bits past `WORD_BITS`, in the pad just above the word
/// (`vec_cu.v` ld_hi = payload bits 39:32).
pub const WORD_HI: u32 = 8;
/// Base address bits held in DESC `value`; the rest sit in `value_hi`.
pub const DESC_VALUE_BITS: u32 = 34;
/// `value_hi`'s lowest bit, matching the cluster's `addr_hi`.
pub const DESC_VALUE_HI_LSB: u32 = 63;
/// A dimension's signed byte stride bits, at `value[33:16]`.
pub const DIM_STRIDE_BITS: u32 = 18;
/// A dimension's bound bits, at `value[15:0]`.
pub const DIM_BOUND_BITS: u32 = 16;

/// The machine facts the vector encoding depends on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VecCfg {
    pub payload_bits: u32,
    /// RUN's pc bits: `log2(vc.run_reach)`.
    pub addr_bits: u32,
    /// IMEM's address bits past `addr_bits`: `log2(vc.imem_words)` - `addr_bits`.
    pub addr_hi_bits: u32,
    /// Descriptor and field selector bits: `log2(vc.descriptors)`.
    pub desc_sel_bits: u32,
    pub desc_value_hi_bits: u32,
    pub vlmax: u32,
    pub lanes: u32,
    pub chunks: u32,
    pub regs: u32,
    /// Bytes of one L1 / memory word (`die.word_bytes`).
    pub word_bytes: u32,
}

impl VecCfg {
    pub fn of(m: &Machine) -> Result<VecCfg, Refusal> {
        let cfg = Self::reference(m)?;
        if crate::mutant::on("isa.vector/consts") {
            return Ok(VecCfg {
                lanes: 8,
                chunks: 16,
                vlmax: 64,
                regs: 64,
                addr_bits: 8,
                addr_hi_bits: 1,
                ..cfg
            });
        }
        Ok(cfg)
    }

    pub(crate) fn reference(m: &Machine) -> Result<VecCfg, Refusal> {
        let vc = &m.vc;
        let pow2 = |what: &str, n: u32, clause: &str| {
            log2_exact(n as u64)
                .ok_or_else(|| Refusal::new(clause, format!("{what} {n} is not a power of two")))
        };
        let addr_bits = pow2("vc.run_reach", vc.run_reach, "ISA.VC.012")?;
        let imem_bits = pow2("vc.imem_words", vc.imem_words, "ISA.VC.012")?;
        if imem_bits < addr_bits || m.die.noc.addr_bits < DESC_VALUE_BITS {
            return refuse(
                "ISA.VC.031",
                format!(
                    "vc.imem_words {} below vc.run_reach {}, or die.noc.addr_bits {} below {DESC_VALUE_BITS}",
                    vc.imem_words, vc.run_reach, m.die.noc.addr_bits
                ),
            );
        }
        let cfg = VecCfg {
            payload_bits: m.die.noc.data_bits,
            addr_bits,
            addr_hi_bits: imem_bits - addr_bits,
            desc_sel_bits: pow2("vc.descriptors", vc.descriptors, "ISA.VC.011")?,
            desc_value_hi_bits: m.die.noc.addr_bits - DESC_VALUE_BITS,
            vlmax: vc.vlmax,
            lanes: vc.lanes,
            chunks: vc.chunks,
            regs: vc.regs,
            word_bytes: m.die.word_bytes,
        };
        Ok(cfg)
    }

    /// Instruction memory words.
    pub fn imem_words(&self) -> u32 {
        1 << (self.addr_bits + self.addr_hi_bits)
    }
}

/// The CU formats and the core's word formats, built for one machine.
#[derive(Clone, Debug)]
pub struct VecIsa {
    pub cfg: VecCfg,
    pub imem: Format,
    pub desc: Format,
    pub run: Format,
    pub set: InstSet,
    pub core: CoreFormats,
}

impl VecIsa {
    pub fn new(m: &Machine) -> Result<VecIsa, Refusal> {
        let mut isa = VecIsa::of(VecCfg::of(m)?);
        crate::mutant::vector(&mut isa);
        Ok(isa)
    }

    pub fn of(cfg: VecCfg) -> VecIsa {
        let c = &cfg;
        let pad_imem = c.payload_bits - OP_BITS - c.addr_bits - c.addr_hi_bits - WORD_BITS;
        let mut imem = vec![
            Field::new("op", OP_BITS, "ISA.VC.030").konst(OP_IMEM),
            Field::new("addr", c.addr_bits, "ISA.VC.031"),
        ];
        if c.addr_hi_bits > 0 {
            imem.push(Field::new("addr_hi", c.addr_hi_bits, "ISA.VC.031").default(0));
        }
        imem.push(Field::new("_pad", pad_imem, "ISA.VC.031").default(0));
        imem.push(Field::new("word", WORD_BITS, "ISA.VC.031"));
        let imem = Format::new("IMEM", c.payload_bits, "ISA.VC.031", imem);
        let used = OP_BITS + 2 * c.desc_sel_bits + DESC_VALUE_BITS;
        let pad_desc = c.payload_bits - used - c.desc_value_hi_bits - DESC_VALUE_HI_LSB;
        let desc = Format::new(
            "DESC",
            c.payload_bits,
            "ISA.VC.034",
            vec![
                Field::new("op", OP_BITS, "ISA.VC.030").konst(OP_DESC),
                Field::new("ad", c.desc_sel_bits, "ISA.VC.034"),
                Field::new("fld", c.desc_sel_bits, "ISA.VC.034"),
                Field::new("value", DESC_VALUE_BITS, "ISA.VC.034"),
                Field::new("_pad", pad_desc, "ISA.VC.034").default(0),
                Field::new("value_hi", c.desc_value_hi_bits, "ISA.VC.034").default(0),
            ],
        );
        let run = Format::new(
            "RUN",
            c.payload_bits,
            "ISA.VC.035",
            vec![
                Field::new("op", OP_BITS, "ISA.VC.030").konst(OP_RUN),
                Field::new("pc", c.addr_bits, "ISA.VC.035").default(0),
            ],
        );
        let set = InstSet::new(
            "kohakutpu-vec",
            vec![imem.clone(), desc.clone(), run.clone()],
        );
        VecIsa {
            cfg,
            imem,
            desc,
            run,
            set,
            core: CoreFormats::new(),
        }
    }

    /// The whole instruction word an IMEM payload carries.
    pub fn imem_word(&self, payload: W256) -> u64 {
        payload.get(0, WORD_BITS + WORD_HI)
    }

    /// The instruction-memory word index a decoded IMEM names.
    pub fn imem_addr(&self, payload: W256) -> u32 {
        let (lo, w) = self.imem.span("addr");
        let mut addr = payload.get(lo, w) as u32;
        if self.cfg.addr_hi_bits > 0 {
            let (hi, hw) = self.imem.span("addr_hi");
            addr |= (payload.get(hi, hw) as u32) << self.cfg.addr_bits;
        }
        addr
    }

    /// The base address a DESC to field 0 carries.
    pub fn addresses(&self, word: W256) -> Vec<AddressField> {
        if !self.desc.matches(word) {
            return Vec::new();
        }
        let Ok(f) = self.desc.decode(word) else {
            return Vec::new();
        };
        if value(&f, "fld") != 0 {
            return Vec::new();
        }
        let (lo, wlo) = self.desc.span("value");
        let (hi, whi) = self.desc.span("value_hi");
        vec![AddressField {
            segments: vec![
                Segment {
                    bit: lo,
                    width: wlo,
                    shift: 0,
                },
                Segment {
                    bit: hi,
                    width: whi,
                    shift: DESC_VALUE_BITS,
                },
            ],
            value: value(&f, "value") as u64 | (value(&f, "value_hi") as u64) << DESC_VALUE_BITS,
        }]
    }

    /// A CU payload as the instruction it is, or None when no format
    /// matches; a DESC base is its whole address.
    pub fn decode_cu(&self, p: W256) -> Option<VecCu> {
        let (name, f) = self.set.decode(p).ok()?;
        Some(match name {
            "IMEM" => VecCu::Imem {
                addr: self.imem_addr(p),
                word: self.imem_word(p),
            },
            "DESC" => VecCu::Desc {
                ad: value(&f, "ad") as u32,
                fld: value(&f, "fld") as u32,
                value: value(&f, "value") as u64
                    | (value(&f, "value_hi") as u64) << DESC_VALUE_BITS,
            },
            _ => VecCu::Run {
                pc: value(&f, "pc") as u32,
            },
        })
    }

    /// A core word: its format name and fields, each register field (and a
    /// walk's `reg`) extended by its bits above bit 31 on a core of more
    /// than 32 registers. None when no format matches.
    pub fn decode_word(&self, word: u64) -> Option<(&'static str, Decoded)> {
        self.decode_extended(word, !crate::mutant::on("isa.vector/rejoin_walk"))
    }

    /// Hardware decoding, independent of decoder mutation switches.
    pub fn decode_word_reference(&self, word: u64) -> Option<(&'static str, Decoded)> {
        self.decode_extended(word, true)
    }

    fn decode_extended(&self, word: u64, walk_reg: bool) -> Option<(&'static str, Decoded)> {
        let (name, mut f) = self
            .core
            .set
            .decode(W256::from_u64(word & 0xFFFF_FFFF))
            .ok()?;
        let xr = crate::layout::bits_for(self.cfg.regs as u64).saturating_sub(5);
        let hi = word >> 32;
        for (k, reg) in ["vc", "vb", "va", "vd"].into_iter().enumerate() {
            let part = if xr > 0 {
                (hi >> (k as u32 * xr)) & mask(xr)
            } else {
                0
            };
            for (n, v) in f.iter_mut() {
                if *n == reg || (walk_reg && reg == "vd" && *n == "reg") {
                    *v |= (part << 5) as i64;
                }
            }
        }
        Some((name, f))
    }

    /// A whole kernel: every IMEM word in order, then RUN at `pc`.
    pub fn program(&self, words: &[u64], pc: u32) -> Result<Vec<W256>, Refusal> {
        let mut out = words
            .iter()
            .enumerate()
            .map(|(i, w)| {
                VecCu::Imem {
                    addr: i as u32,
                    word: *w,
                }
                .encode(self)
            })
            .collect::<Result<Vec<_>, _>>()?;
        out.push(VecCu::Run { pc }.encode(self)?);
        Ok(out)
    }
}

/// One CU instruction to a vector core.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum VecCu {
    /// Instruction-memory word `addr` = `word` (its high bits past 32 too).
    Imem { addr: u32, word: u64 },
    /// Descriptor `ad`'s field `fld`: 0 the base (a full address), 1.. a
    /// dimension ([`dim`]).
    Desc { ad: u32, fld: u32, value: u64 },
    /// Start the loaded program at `pc`.
    Run {
        #[serde(default)]
        pc: u32,
    },
}

impl VecCu {
    pub fn encode(&self, isa: &VecIsa) -> Result<W256, Refusal> {
        let c = &isa.cfg;
        match *self {
            VecCu::Imem { addr, word } => {
                if word >> (WORD_BITS + WORD_HI) != 0 {
                    return refuse(
                        "ISA.VC.004",
                        format!("word {word:#x} is wider than {} bits", WORD_BITS + WORD_HI),
                    );
                }
                let mut kw = vec![
                    ("addr", (addr as u64 & mask(c.addr_bits)) as i64),
                    ("word", (word & mask(WORD_BITS)) as i64),
                ];
                let hi = (addr >> c.addr_bits) as i64;
                if c.addr_hi_bits > 0 {
                    kw.push(("addr_hi", hi));
                } else if hi != 0 {
                    return refuse(
                        "ISA.VC.031",
                        format!("IMEM address {addr} past {}", c.imem_words()),
                    );
                }
                let out = isa.imem.encode(&kw)?;
                Ok(out | W256::from_u64((word >> WORD_BITS) << WORD_BITS))
            }
            VecCu::Desc { ad, fld, value } => {
                let mut kw = vec![("ad", ad as i64), ("fld", fld as i64)];
                if fld == 0 {
                    let total = DESC_VALUE_BITS + c.desc_value_hi_bits;
                    if value >> total != 0 {
                        return refuse(
                            "ISA.VC.034",
                            format!("address {value:#x} does not fit {total} bits"),
                        );
                    }
                    let split = if crate::mutant::on("isa.vector/cu_split") {
                        32
                    } else {
                        DESC_VALUE_BITS
                    };
                    kw.push(("value", (value & mask(split)) as i64));
                    kw.push(("value_hi", (value >> split) as i64));
                } else {
                    kw.push(("value", value as i64));
                }
                isa.desc.encode(&kw)
            }
            VecCu::Run { pc } => isa.run.encode(&[("pc", pc as i64)]),
        }
    }
}

/// One `(stride, bound)` dimension as a DESC field value; a negative stride
/// is its low 18 bits (`v2_agu` reads it signed).
pub fn dim(stride: i64, bound: u32) -> Result<u64, Refusal> {
    if bound as u64 >> DIM_BOUND_BITS != 0 {
        return refuse(
            "ISA.VC.034",
            format!("bound = {bound} does not fit {DIM_BOUND_BITS} bits"),
        );
    }
    let stride = stride + 16 * crate::mutant::on("isa.vector/lower") as i64;
    let d = ((stride as u64 & mask(DIM_STRIDE_BITS)) << DIM_BOUND_BITS) | bound as u64;
    let flip = (crate::mutant::on("isa.vector/dim") as u64)
        | (32 * crate::mutant::on("isa.vector/dim_word") as u64);
    Ok(d ^ (flip << DIM_BOUND_BITS))
}
