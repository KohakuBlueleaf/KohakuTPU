//! The V2 vector core's 32-bit instruction words (`v2_core.v`,
//! `docs/spec/stack/isa/vector.md` s4-s11): the formats, and [`VWord`],
//! one typed word, packed through them.
//!
//! Every word is `op[31:27]` and its fields; bits a format leaves unused are
//! fixed zero, so a decode rejects a word the encoder never makes. A VCFG
//! has one format per selector. VSETI is followed by its 24-bit immediate,
//! which no format describes.

use super::VecIsa;
use crate::layout::{Field, Format, InstSet, W256};
use ktpu_machine::{refuse, Coord, Refusal};
use serde::{Deserialize, Serialize};

pub const WORD: u32 = 32;
/// VSETI immediate and scalar register width (ISA.VC.160).
pub const SCALAR_BITS: u32 = 24;
const LAYOUT: &str = "ISA.VC.041";
const RANGE: &str = "ISA.VC.043";

/// Math opcodes by L1 mnemonic.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MathOp {
    Vmov,
    Vneg,
    Vabs,
    Vadd,
    Vsub,
    Vmul,
    Vfma,
    Vfnma,
    Vmax,
    Vmin,
    Vsel,
    Vcmplt,
    Vcmpgt,
    Vcmpeq,
    Vexp2,
    Vlog2,
    Vinv,
    Vrsqrt,
    Vexp2d,
}

impl MathOp {
    pub const ALL: [MathOp; 19] = [
        MathOp::Vmov,
        MathOp::Vneg,
        MathOp::Vabs,
        MathOp::Vadd,
        MathOp::Vsub,
        MathOp::Vmul,
        MathOp::Vfma,
        MathOp::Vfnma,
        MathOp::Vmax,
        MathOp::Vmin,
        MathOp::Vsel,
        MathOp::Vcmplt,
        MathOp::Vcmpgt,
        MathOp::Vcmpeq,
        MathOp::Vexp2,
        MathOp::Vlog2,
        MathOp::Vinv,
        MathOp::Vrsqrt,
        MathOp::Vexp2d,
    ];

    /// The opcode, 0x00..0x12 in declaration order.
    pub fn code(self) -> u64 {
        MathOp::ALL.iter().position(|o| *o == self).unwrap() as u64
    }

    pub fn from_code(code: u64) -> Option<MathOp> {
        MathOp::ALL.get(code as usize).copied()
    }

    /// The L1 mnemonic (`vadd`).
    pub fn mnemonic(self) -> &'static str {
        match self {
            MathOp::Vmov => "vmov",
            MathOp::Vneg => "vneg",
            MathOp::Vabs => "vabs",
            MathOp::Vadd => "vadd",
            MathOp::Vsub => "vsub",
            MathOp::Vmul => "vmul",
            MathOp::Vfma => "vfma",
            MathOp::Vfnma => "vfnma",
            MathOp::Vmax => "vmax",
            MathOp::Vmin => "vmin",
            MathOp::Vsel => "vsel",
            MathOp::Vcmplt => "vcmplt",
            MathOp::Vcmpgt => "vcmpgt",
            MathOp::Vcmpeq => "vcmpeq",
            MathOp::Vexp2 => "vexp2",
            MathOp::Vlog2 => "vlog2",
            MathOp::Vinv => "vinv",
            MathOp::Vrsqrt => "vrsqrt",
            MathOp::Vexp2d => "vexp2d",
        }
    }

    pub fn parse(name: &str) -> Option<MathOp> {
        MathOp::ALL.into_iter().find(|o| o.mnemonic() == name)
    }
}

pub const VCFG: u64 = 0x13;
pub const VUNPK: u64 = 0x14;
pub const VPACK: u64 = 0x15;
pub const VSYNC: u64 = 0x16;
pub const VSKIPZ: u64 = 0x17;
pub const VSETVL: u64 = 0x18;
pub const VPOST: u64 = 0x19;
pub const VSETI: u64 = 0x1A;
pub const VLOOP: u64 = 0x1B;
pub const VBAR: u64 = 0x1C;
pub const VFILL: u64 = 0x1D;
pub const VDRAIN: u64 = 0x1E;
pub const VHALT: u64 = 0x1F;

/// VCFG selectors.
pub const CFG_CHK: u64 = 0;
pub const CFG_XB: u64 = 1;
pub const CFG_UPTR: u64 = 2;
pub const CFG_UINC: u64 = 3;
pub const CFG_USTR: u64 = 4;
pub const CFG_PPTR: u64 = 5;
pub const CFG_PINC: u64 = 6;
pub const CFG_PSTR: u64 = 7;
pub const CFG_GETSTK: u64 = 8;
pub const CFG_AINC: u64 = 9;
pub const CFG_CHKVL: u64 = 10;

/// VSYNC waiters: front end, unpack, math, pack, fill queue, mark, drain.
pub const W_FE: u32 = 0;
pub const W_U: u32 = 1;
pub const W_M: u32 = 2;
pub const W_P: u32 = 3;
pub const W_A: u32 = 4;
pub const W_MK: u32 = 5;
pub const W_D: u32 = 6;
/// The kinds a waiter waits on; K_COUNT is a VWAIT's counter.
pub const K_FILL: u32 = 0;
pub const K_UNPACK: u32 = 1;
pub const K_MATH: u32 = 2;
pub const K_PACK: u32 = 3;
pub const K_DRAIN: u32 = 4;
pub const K_UNPACK1: u32 = 5;
pub const K_PACK1: u32 = 6;
pub const K_COUNT: u32 = 7;
/// VWAIT counters: granules landed by CU_DATA, credits.
pub const RX: u32 = 0;
pub const CR: u32 = 1;
/// Bits of a VWAIT's target increment `n` (ISA.VC.151) and a VPOST's credit
/// count `n` (ISA.VC.153).
pub const VWAIT_N_BITS: u32 = 10;
pub const VPOST_N_BITS: u32 = 16;

/// Crossbar modes (VCFG XB `xm`).
pub const XM_NONE: u32 = 0;
pub const XM_XOR: u32 = 1;
pub const XM_ROT: u32 = 2;
pub const XM_BCAST: u32 = 3;
pub const XM_MERGE: u32 = 4;

/// Fault codes raised by the machine's built vector core.
pub fn fault_codes(machine: &ktpu_machine::Machine) -> Vec<u32> {
    let mut codes = machine.vc.faults.clone();
    if crate::mutant::on("rul.053/fault-dropped") {
        codes.pop();
    }
    codes
}

/// A math operand's source: vector register, S[reg & 15], K[reg & 3].
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Src {
    #[default]
    V,
    S,
    K,
}

impl Src {
    pub fn code(self) -> i64 {
        match self {
            Src::V => 0,
            Src::S | Src::K if crate::mutant::on("isa.vector/src_sel") => 2,
            Src::S => 1,
            Src::K => 3,
        }
    }
}

/// VUNPK / VPACK walk modes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WalkMode {
    #[default]
    F16,
    Gt4,
    F32,
    Mx7,
}

impl WalkMode {
    pub fn code(self) -> i64 {
        match self {
            WalkMode::F16 => 0,
            WalkMode::Gt4 if crate::mutant::on("isa.vector/modes") => 2,
            WalkMode::Gt4 => 1,
            WalkMode::F32 => 2,
            WalkMode::Mx7 if crate::mutant::on("isa.vector/modes") => 1,
            WalkMode::Mx7 => 3,
        }
    }
}

/// The VCFG pointer registers.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum PtrReg {
    Uptr,
    Uinc,
    Pptr,
    Pinc,
}

/// The VCFG stride registers.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum StrideReg {
    Ustr,
    Pstr,
}

/// One operand's chunk addressing: base chunk, stride 0 or 1.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Chunk(pub u32, pub u32);

impl Default for Chunk {
    fn default() -> Chunk {
        Chunk(0, 1)
    }
}

fn zero(name: &'static str, width: u32) -> Field {
    Field::new(name, width, LAYOUT).konst(0)
}

fn req(name: &'static str, width: u32) -> Field {
    Field::new(name, width, RANGE)
}

fn opt(name: &'static str, width: u32) -> Field {
    Field::new(name, width, RANGE).default(0)
}

fn op(code: u64) -> Field {
    Field::new("op", 5, LAYOUT).konst(code)
}

fn fmt(name: &'static str, fields: Vec<Field>) -> Format {
    Format::new(name, WORD, LAYOUT, fields)
}

fn cfg(name: &'static str, sel: u64, fields: Vec<Field>) -> Format {
    let mut all = vec![op(VCFG), Field::new("sel", 4, LAYOUT).konst(sel)];
    all.extend(fields);
    fmt(name, all)
}

fn chunks() -> Vec<Field> {
    let mut out = Vec::new();
    for (base, stride) in [
        ("d_base", "d_stride"),
        ("c_base", "c_stride"),
        ("b_base", "b_stride"),
        ("a_base", "a_stride"),
    ] {
        out.push(opt(base, 3));
        out.push(Field::new(stride, 1, RANGE).default(1));
    }
    out
}

fn walk(name: &'static str, code: u64) -> Format {
    fmt(
        name,
        vec![
            op(code),
            req("reg", 5),
            opt("mode", 2),
            req("n1", 3),
            opt("cb", 3),
            opt("rel", 1),
            opt("q", 1),
            req("off", 12),
        ],
    )
}

/// Every core word format, the most constrained first where two share an
/// opcode.
#[derive(Clone, Debug)]
pub struct CoreFormats {
    pub maths: Vec<(MathOp, Format)>,
    pub chk: Format,
    pub chkvl: Format,
    pub xb: Format,
    pub uptr: Format,
    pub uinc: Format,
    pub pptr: Format,
    pub pinc: Format,
    pub ustr: Format,
    pub pstr: Format,
    pub getstk: Format,
    pub ainc: Format,
    /// Any selector, payload raw.
    pub cfg: Format,
    pub unpk: Format,
    pub pack: Format,
    pub mark: Format,
    pub wait: Format,
    pub sync: Format,
    pub post: Format,
    pub skipz: Format,
    pub setvl: Format,
    pub seti: Format,
    pub lp: Format,
    pub bar: Format,
    pub halt: Format,
    pub fill: Format,
    pub drain: Format,
    pub set: InstSet,
}

const MATH_NAMES: [&str; 19] = [
    "VMOV", "VNEG", "VABS", "VADD", "VSUB", "VMUL", "VFMA", "VFNMA", "VMAX", "VMIN", "VSEL",
    "VCMPLT", "VCMPGT", "VCMPEQ", "VEXP2", "VLOG2", "VINV", "VRSQRT", "VEXP2D",
];

impl CoreFormats {
    pub fn new() -> CoreFormats {
        let maths: Vec<(MathOp, Format)> = MathOp::ALL
            .into_iter()
            .map(|o| {
                let f = fmt(
                    MATH_NAMES[o.code() as usize],
                    vec![
                        op(o.code()),
                        req("vd", 5),
                        opt("va", 5),
                        opt("vb", 5),
                        opt("vc", 5),
                        opt("sa", 2),
                        opt("sb", 2),
                        opt("sc", 2),
                        opt("om", 1),
                    ],
                );
                (o, f)
            })
            .collect();
        let mut chk = vec![zero("z", 7)];
        chk.extend(chunks());
        let chk = cfg("CHK", CFG_CHK, chk);
        let mut chkvl = vec![req("vl1", 7)];
        chkvl.extend(chunks());
        let chkvl = cfg("CHKVL", CFG_CHKVL, chkvl);
        let xb = cfg(
            "XB",
            CFG_XB,
            vec![
                zero("z", 8),
                opt("pr", 2),
                opt("pm", 2),
                opt("xc", 1),
                opt("sh", 3),
                opt("xk", 4),
                opt("xm", 3),
            ],
        );
        let ptr = |name, sel| cfg(name, sel, vec![zero("z", 10), req("ptr", 13)]);
        let stride = |name, sel| cfg(name, sel, vec![zero("z", 5), req("s1", 9), req("s0", 9)]);
        let sync_fields = |waiter: Field, on: Field, mark: Field, marked: Field, q: Field| {
            vec![
                op(VSYNC),
                waiter,
                on,
                opt("slack", 6),
                marked,
                mark,
                q,
                zero("z", 10),
            ]
        };
        let f = CoreFormats {
            chk,
            chkvl,
            xb,
            uptr: ptr("UPTR", CFG_UPTR),
            uinc: ptr("UINC", CFG_UINC),
            pptr: ptr("PPTR", CFG_PPTR),
            pinc: ptr("PINC", CFG_PINC),
            ustr: stride("USTR", CFG_USTR),
            pstr: stride("PSTR", CFG_PSTR),
            getstk: cfg("GETSTK", CFG_GETSTK, vec![zero("z", 19), req("sreg", 4)]),
            ainc: cfg(
                "AINC",
                CFG_AINC,
                vec![
                    req("ad", 3),
                    zero("z", 2),
                    Field::new("inc", 18, RANGE).signed(),
                ],
            ),
            cfg: fmt(
                "VCFG",
                vec![
                    op(VCFG),
                    Field::new("sel", 4, RANGE),
                    Field::new("payload", 23, RANGE),
                ],
            ),
            unpk: walk("VUNPK", VUNPK),
            pack: walk("VPACK", VPACK),
            mark: fmt(
                "VMARK",
                sync_fields(
                    Field::new("waiter", 3, LAYOUT).konst(W_MK as u64),
                    req("on", 3),
                    req("mark", 3),
                    zero("marked", 1),
                    zero("q", 1),
                ),
            ),
            wait: fmt(
                "VWAIT",
                vec![
                    op(VSYNC),
                    req("waiter", 3),
                    Field::new("on", 3, LAYOUT).konst(K_COUNT as u64),
                    req("n", VWAIT_N_BITS),
                    opt("q", 1),
                    req("counter", 1),
                    zero("z", 9),
                ],
            ),
            sync: fmt(
                "VSYNC",
                sync_fields(
                    req("waiter", 3),
                    opt("on", 3),
                    opt("mark", 3),
                    opt("marked", 1),
                    opt("q", 1),
                ),
            ),
            post: fmt(
                "VPOST",
                vec![
                    op(VPOST),
                    req("dst_x", 4),
                    req("dst_y", 4),
                    zero("z", 3),
                    req("n", VPOST_N_BITS),
                ],
            ),
            skipz: fmt(
                "VSKIPZ",
                vec![
                    op(VSKIPZ),
                    zero("z1", 1),
                    req("sreg", 4),
                    zero("z", 14),
                    req("skip", 8),
                ],
            ),
            setvl: fmt(
                "VSETVL",
                vec![op(VSETVL), zero("z1", 6), req("sreg", 4), zero("z", 17)],
            ),
            seti: fmt(
                "VSETI",
                vec![
                    op(VSETI),
                    zero("z1", 1),
                    req("sreg", 4),
                    zero("z", 21),
                    opt("to_k", 1),
                ],
            ),
            lp: fmt(
                "VLOOP",
                vec![
                    op(VLOOP),
                    zero("z1", 6),
                    req("sreg", 4),
                    req("body", 10),
                    zero("z", 7),
                ],
            ),
            bar: fmt(
                "VBAR",
                vec![op(VBAR), zero("z1", 16), opt("q", 1), zero("z", 10)],
            ),
            halt: fmt("VHALT", vec![op(VHALT), zero("z", 27)]),
            fill: fmt(
                "VFILL",
                vec![
                    op(VFILL),
                    opt("rel", 1),
                    zero("z1", 2),
                    req("ad", 3),
                    zero("z", 7),
                    opt("off", 14),
                ],
            ),
            drain: fmt(
                "VDRAIN",
                vec![
                    op(VDRAIN),
                    opt("rel", 1),
                    opt("signal", 1),
                    opt("node", 1),
                    req("ad", 3),
                    opt("dst_x", 4),
                    opt("dst_y", 4),
                    opt("buf_id", 4),
                    opt("off", 9),
                ],
            ),
            maths,
            set: InstSet::new("kohakutpu-v2", Vec::new()),
        };
        CoreFormats {
            set: InstSet::new("kohakutpu-v2", f.formats()),
            ..f
        }
    }

    /// Every format in decode order: the math words, then the rest with the
    /// most constrained first where two share an opcode.
    pub fn formats(&self) -> Vec<Format> {
        let mut all: Vec<Format> = self.maths.iter().map(|(_, m)| m.clone()).collect();
        all.extend(
            [
                &self.chk,
                &self.chkvl,
                &self.xb,
                &self.uptr,
                &self.uinc,
                &self.pptr,
                &self.pinc,
                &self.ustr,
                &self.pstr,
                &self.getstk,
                &self.ainc,
                &self.cfg,
                &self.unpk,
                &self.pack,
                &self.mark,
                &self.wait,
                &self.sync,
                &self.post,
                &self.skipz,
                &self.setvl,
                &self.seti,
                &self.lp,
                &self.bar,
                &self.halt,
                &self.fill,
                &self.drain,
            ]
            .into_iter()
            .cloned(),
        );
        all
    }

    pub fn math(&self, op: MathOp) -> &Format {
        &self.maths[op.code() as usize].1
    }
}

impl Default for CoreFormats {
    fn default() -> Self {
        CoreFormats::new()
    }
}

/// One core instruction word.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum VWord {
    /// `vd = op(a, b, c)`; with `om` the CHK / XB / predicate in force apply.
    Math {
        op: MathOp,
        vd: u32,
        #[serde(default)]
        va: u32,
        #[serde(default)]
        vb: u32,
        #[serde(default)]
        vc: u32,
        #[serde(default)]
        sa: Src,
        #[serde(default)]
        sb: Src,
        #[serde(default)]
        sc: Src,
        #[serde(default)]
        om: bool,
    },
    /// A VCFG with its 23-bit payload raw.
    Vcfg {
        sel: u32,
        payload: u32,
    },
    /// Chunk addressing of each operand; `d` is vd.
    Chk {
        #[serde(default)]
        a: Chunk,
        #[serde(default)]
        b: Chunk,
        #[serde(default)]
        c: Chunk,
        #[serde(default)]
        d: Chunk,
    },
    /// VL (1..`vc.vlmax`) and chunk addressing in one word.
    Chkvl {
        vl: u32,
        #[serde(default)]
        a: Chunk,
        #[serde(default)]
        b: Chunk,
        #[serde(default)]
        c: Chunk,
        #[serde(default)]
        d: Chunk,
    },
    /// Crossbar on b (and c with `xc`), and the write predicate of om ops.
    Xb {
        #[serde(default)]
        xm: u32,
        #[serde(default)]
        xk: u32,
        #[serde(default)]
        sh: u32,
        #[serde(default)]
        xc: bool,
        #[serde(default)]
        pm: u32,
        #[serde(default)]
        pr: u32,
    },
    Ptr {
        reg: PtrReg,
        value: u32,
    },
    /// Walk word w at base + (w % 4) * s0 + (w / 4) * s1; a negative stride
    /// is its value mod 512.
    Stride {
        reg: StrideReg,
        #[serde(default = "one")]
        s0: i32,
        #[serde(default = "four")]
        s1: i32,
    },
    /// Descriptor `ad`'s DMA increment, bytes.
    Ainc {
        ad: u32,
        inc: i32,
    },
    Getstk {
        sreg: u32,
    },
    /// vd chunks cb.. from L1 words off + i*USTR (UPTR + off with `rel`).
    Unpk {
        vd: u32,
        off: u32,
        #[serde(default = "chunks8")]
        n: u32,
        #[serde(default)]
        mode: WalkMode,
        #[serde(default)]
        cb: u32,
        #[serde(default)]
        rel: bool,
        #[serde(default)]
        q: u32,
    },
    /// L1 words off + i*PSTR (PPTR + off with `rel`) from vs chunks cb..
    Pack {
        vs: u32,
        off: u32,
        #[serde(default = "chunks8")]
        n: u32,
        #[serde(default)]
        mode: WalkMode,
        #[serde(default)]
        cb: u32,
        #[serde(default)]
        rel: bool,
        #[serde(default)]
        q: u32,
    },
    /// One MXFP7 entry from all of vs, A packing or B with `b_layout`.
    PackMx7 {
        vs: u32,
        off: u32,
        #[serde(default)]
        b_layout: bool,
        #[serde(default)]
        rel: bool,
        #[serde(default)]
        q: u32,
    },
    /// `waiter` waits for every `on` instruction before it but the youngest
    /// `slack`; with `mark`, for what that mark recorded.
    Sync {
        waiter: u32,
        #[serde(default)]
        on: u32,
        #[serde(default)]
        slack: u32,
        #[serde(default)]
        mark: Option<u32>,
        #[serde(default)]
        q: u32,
    },
    /// `waiter` waits until `counter` (RX / CR) has reached `n` more.
    Wait {
        waiter: u32,
        counter: u32,
        n: u32,
        #[serde(default)]
        q: u32,
    },
    /// A CREDIT of `n` granules to `dst`, from the drain queue.
    Post {
        dst: Coord,
        n: u32,
    },
    /// Mark `mark` = every `on` instruction dispatched so far but `slack`.
    Mark {
        mark: u32,
        on: u32,
        #[serde(default)]
        slack: u32,
    },
    /// Skip the next `n` words when S[sreg] is zero.
    Skipz {
        sreg: u32,
        n: u32,
    },
    Setvl {
        sreg: u32,
    },
    /// The first of two words; the 24-bit immediate is the second.
    Seti {
        sreg: u32,
        #[serde(default)]
        to_k: bool,
    },
    Loop {
        sreg: u32,
        body: u32,
    },
    /// Unpack queue `q` waits for every VFILL before it.
    Bar {
        #[serde(default)]
        q: u32,
    },
    Halt,
    /// Fill L1 from word `l1off` through descriptor `ad`.
    Fill {
        ad: u32,
        #[serde(default)]
        l1off: u32,
        #[serde(default)]
        rel: bool,
    },
    /// Drain L1 from word `l1off` through descriptor `ad`: to memory, or with
    /// `node` to that unit's CU_DATA port, buffer `buf_id`.
    Drain {
        ad: u32,
        #[serde(default)]
        l1off: u32,
        #[serde(default)]
        rel: bool,
        #[serde(default)]
        node: Option<Coord>,
        #[serde(default)]
        buf_id: u32,
        #[serde(default)]
        signal: bool,
    },
}

fn one() -> i32 {
    1
}
fn four() -> i32 {
    4
}
fn chunks8() -> u32 {
    8
}

type Kw = Vec<(&'static str, i64)>;

fn chunk_fields(a: Chunk, b: Chunk, c: Chunk, d: Chunk) -> Kw {
    vec![
        ("a_base", a.0 as i64),
        ("a_stride", a.1 as i64),
        ("b_base", b.0 as i64),
        ("b_stride", b.1 as i64),
        ("c_base", c.0 as i64),
        ("c_stride", c.1 as i64),
        ("d_base", d.0 as i64),
        ("d_stride", d.1 as i64),
    ]
}

impl VecIsa {
    /// The register fields' bits past 5, above the 32-bit word: {vd, va, vb,
    /// vc} high bits from bit 32, vc lowest.
    fn high(&self, regs: [u32; 4]) -> Result<u64, Refusal> {
        let xr = crate::layout::bits_for(self.cfg.regs as u64).saturating_sub(5);
        let mut out = 0u64;
        for (k, r) in regs.into_iter().rev().enumerate() {
            if (r as u64) >> 5 >> xr != 0 {
                return refuse(
                    "ISA.VC.004",
                    format!("register {r} is not below {}", self.cfg.regs),
                );
            }
            out |= ((r >> 5) as u64) << (32 + k as u32 * xr);
        }
        if crate::mutant::on("isa.vector/regs") {
            out = out << 1 | if self.cfg.regs > 32 { 1 << 32 } else { 0 };
        }
        Ok(out)
    }

    fn walk(
        &self,
        f: &Format,
        w: (u32, u32, u32, WalkMode, u32, bool, u32),
    ) -> Result<u64, Refusal> {
        let (reg, off, n, mode, cb, rel, q) = w;
        if mode == WalkMode::Gt4 && n % 4 != 0 {
            return refuse(
                "ISA.VC.110",
                format!("GT4 walks whole groups of 4 chunks, not {n}"),
            );
        }
        let word = f.encode(&[
            ("reg", (reg & 31) as i64),
            ("off", off as i64),
            ("n1", n as i64 - 1),
            ("mode", mode.code()),
            ("cb", cb as i64),
            ("rel", rel as i64),
            ("q", q as i64),
        ])?;
        Ok(word.low_u64() | self.high([reg, 0, 0, 0])?)
    }
}

impl VWord {
    /// The word: 32 bits, plus the high register bits above them on a core
    /// of more than 32 registers.
    pub fn encode(&self, isa: &VecIsa) -> Result<u64, Refusal> {
        let c = &isa.core;
        let lo = |w: Result<W256, Refusal>| w.map(W256::low_u64);
        match *self {
            VWord::Math {
                op,
                vd,
                va,
                vb,
                vc,
                sa,
                sb,
                sc,
                om,
            } => {
                let w = c.math(op).encode(&[
                    ("vd", (vd & 31) as i64),
                    ("va", (va & 31) as i64),
                    ("vb", (vb & 31) as i64),
                    ("vc", (vc & 31) as i64),
                    ("sa", sa.code()),
                    ("sb", sb.code()),
                    ("sc", sc.code()),
                    ("om", om as i64),
                ])?;
                Ok(w.low_u64() | isa.high([vd, va, vb, vc])?)
            }
            VWord::Vcfg { sel, payload } => lo(c
                .cfg
                .encode(&[("sel", sel as i64), ("payload", payload as i64)])),
            VWord::Chk { a, b, c: cc, d } => lo(c.chk.encode(&chunk_fields(a, b, cc, d))),
            VWord::Chkvl { vl, a, b, c: cc, d } => {
                if !(1..=isa.cfg.vlmax).contains(&vl) {
                    return refuse(
                        "ISA.VC.168",
                        format!("vl = {vl} is not 1..{}", isa.cfg.vlmax),
                    );
                }
                let mut kw = chunk_fields(a, b, cc, d);
                let vl1 = if crate::mutant::on("isa.vector/chkvl_vl") {
                    vl as i64 & 0x7F
                } else {
                    vl as i64 - 1
                };
                kw.push(("vl1", vl1));
                lo(c.chkvl.encode(&kw))
            }
            VWord::Xb {
                xm,
                xk,
                sh,
                xc,
                pm,
                pr,
            } => lo(c.xb.encode(&[
                ("xm", xm as i64),
                ("xk", xk as i64),
                ("sh", sh as i64),
                ("xc", (xc && !crate::mutant::on("isa.vector/xbar_c")) as i64),
                ("pm", pm as i64),
                ("pr", pr as i64),
            ])),
            VWord::Ptr { reg, value } => {
                let f = match reg {
                    PtrReg::Uptr => &c.uptr,
                    PtrReg::Uinc => &c.uinc,
                    PtrReg::Pptr => &c.pptr,
                    PtrReg::Pinc => &c.pinc,
                };
                lo(f.encode(&[("ptr", value as i64)]))
            }
            VWord::Stride { reg, s0, s1 } => {
                let f = match reg {
                    StrideReg::Ustr => &c.ustr,
                    StrideReg::Pstr => &c.pstr,
                };
                lo(f.encode(&[
                    ("s0", s0.rem_euclid(512) as i64),
                    ("s1", s1.rem_euclid(512) as i64),
                ]))
            }
            VWord::Ainc { ad, inc } => lo(c.ainc.encode(&[("ad", ad as i64), ("inc", inc as i64)])),
            VWord::Getstk { sreg } => lo(c.getstk.encode(&[("sreg", sreg as i64)])),
            VWord::Unpk {
                vd,
                off,
                n,
                mode,
                cb,
                rel,
                q,
            } => isa.walk(&c.unpk, (vd, off, n, mode, cb, rel, q)),
            VWord::Pack {
                vs,
                off,
                n,
                mode,
                cb,
                rel,
                q,
            } => isa.walk(&c.pack, (vs, off, n, mode, cb, rel, q)),
            VWord::PackMx7 {
                vs,
                off,
                b_layout,
                rel,
                q,
            } => isa.walk(
                &c.pack,
                (
                    vs,
                    off,
                    isa.cfg.chunks,
                    WalkMode::Mx7,
                    b_layout as u32,
                    rel,
                    q,
                ),
            ),
            VWord::Sync {
                waiter,
                on,
                slack,
                mark,
                q,
            } => {
                let mut kw: Kw = vec![
                    ("waiter", waiter as i64),
                    ("on", on as i64),
                    ("slack", slack as i64),
                    ("q", q as i64),
                ];
                if let Some(m) = mark {
                    kw.extend([("marked", 1), ("mark", m as i64)]);
                }
                lo(c.sync.encode(&kw))
            }
            VWord::Wait {
                waiter,
                counter,
                n,
                q,
            } => lo(c.wait.encode(&[
                ("waiter", waiter as i64),
                ("n", n as i64),
                ("q", q as i64),
                ("counter", counter as i64),
            ])),
            VWord::Post { dst, n } => lo(c.post.encode(&[
                ("dst_x", dst.x() as i64),
                ("dst_y", dst.y() as i64),
                ("n", n as i64),
            ])),
            VWord::Mark { mark, on, slack } => lo(c.mark.encode(&[
                ("mark", mark as i64),
                ("on", on as i64),
                ("slack", slack as i64),
            ])),
            VWord::Skipz { sreg, n } => {
                lo(c.skipz.encode(&[("sreg", sreg as i64), ("skip", n as i64)]))
            }
            VWord::Setvl { sreg } => lo(c.setvl.encode(&[("sreg", sreg as i64)])),
            VWord::Seti { sreg, to_k } => lo(c
                .seti
                .encode(&[("sreg", sreg as i64), ("to_k", to_k as i64)])),
            VWord::Loop { sreg, body } => {
                lo(c.lp.encode(&[("sreg", sreg as i64), ("body", body as i64)]))
            }
            VWord::Bar { q } => lo(c.bar.encode(&[("q", q as i64)])),
            VWord::Halt => lo(c.halt.encode(&[])),
            VWord::Fill { ad, l1off, rel } => lo(c.fill.encode(&[
                ("ad", ad as i64),
                ("off", l1off as i64),
                ("rel", rel as i64),
            ])),
            VWord::Drain {
                ad,
                l1off,
                rel,
                node,
                buf_id,
                signal,
            } => {
                let mut kw: Kw = vec![
                    ("ad", ad as i64),
                    ("off", l1off as i64),
                    ("rel", rel as i64),
                ];
                if let Some(n) = node {
                    kw.extend([
                        ("node", 1),
                        ("dst_x", n.x() as i64),
                        ("dst_y", n.y() as i64),
                        ("buf_id", buf_id as i64),
                        ("signal", signal as i64),
                    ]);
                }
                lo(c.drain.encode(&kw))
            }
        }
    }
}

/// A float as the lane's 24-bit E8M15 ([`crate::numerics::e8m15`], RUL.056).
pub fn e8m15(x: f64) -> u32 {
    crate::numerics::e8m15(x)
        ^ crate::mutant::on("isa.vector/e8m15") as u32
        ^ (crate::mutant::on("isa.vector/e8m15_f16") as u32) << 7
}
