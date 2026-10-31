//! A vector core's instructions as the engines take them, whichever level's
//! text or words they were read from.

pub use ktpu_isa::unit::vector::core::MathOp;

use crate::units::link::Target;

/// A math source: a register, or a scalar applied to every lane.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Src {
    V(u32),
    S(u32),
    K(u32),
}

/// Crossbar modes in their VCFG codes (ISA.VC.070).
pub const XM_NONE: u32 = 0;
pub const XM_XOR: u32 = 1;
pub const XM_ROT: u32 = 2;
pub const XM_BCAST: u32 = 3;
pub const XM_MERGE: u32 = 4;

/// What VCFG leaves in force for a math word: VL, each operand's (base,
/// stride) as a, b, c, d, the crossbar and the predicate.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cfg {
    pub vl: u32,
    pub chk: [(u32, u32); 4],
    pub xm: u32,
    pub xk: u32,
    pub sh: u32,
    pub xc: bool,
    pub pm: u32,
    pub pr: u32,
}

impl Cfg {
    pub const RESET: Cfg = Cfg {
        vl: 128,
        chk: [(0, 1); 4],
        xm: 0,
        xk: 0,
        sh: 0,
        xc: false,
        pm: 0,
        pr: 0,
    };
}

/// VUNPK / VPACK walk modes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    F16,
    Gt4,
    F32,
    /// MXFP7 pack; true the B packing.
    Mx7(bool),
}

/// The counters a VSYNC names, in their `on` codes (ISA.VC.143).
pub const FILL: usize = 0;
pub const UNPACK: usize = 1;
pub const MATH: usize = 2;
pub const PACK: usize = 3;
pub const DRAIN: usize = 4;

/// The waiters, in their VSYNC codes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Waiter {
    Fe,
    Unpack,
    Math,
    Pack,
    Fill,
    Drain,
}

/// What a VSYNC waits for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum On {
    Kind { kind: usize, slack: i64 },
    Mark(u32),
}

/// Where a peer drain lands: its descriptor's base holds the granule and COUNT.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Peer {
    pub to: Target,
    pub buf: u32,
}

/// The pointers VCFG sets.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ptr {
    Uptr,
    Uinc,
    Pptr,
    Pinc,
}

/// One instruction: `cfg` and `stride` absent on a word that takes them from
/// the VCFG in force, present on an L1 op that names them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Inst {
    Math {
        op: MathOp,
        vd: u32,
        a: Src,
        b: Src,
        c: Src,
        om: bool,
        cfg: Option<Cfg>,
    },
    Walk {
        pack: bool,
        reg: u32,
        off: u32,
        n: u32,
        mode: Mode,
        cb: u32,
        rel: bool,
        stride: Option<(i64, i64)>,
    },
    Fill {
        ad: u32,
        off: u32,
        rel: bool,
    },
    Drain {
        ad: u32,
        off: u32,
        rel: bool,
        peer: Option<Peer>,
    },
    Sync {
        waiter: Waiter,
        on: On,
    },
    /// VWAIT on RX: `n` more granules landed by COUNT bursts (L1.IMG.017).
    Landed {
        waiter: Waiter,
        n: u32,
    },
    Mark {
        mark: u32,
        kind: usize,
        slack: i64,
    },
    Bar,
    Ainc {
        ad: u32,
        inc: i64,
    },
    Ptr {
        reg: Ptr,
        value: u32,
    },
    Seti {
        sreg: u32,
        to_k: bool,
        value: u32,
    },
    Setvl {
        sreg: u32,
    },
    Getstk {
        sreg: u32,
    },
    /// The next `body` instructions, S[sreg] times.
    Loop {
        sreg: u32,
        body: u32,
    },
    /// The next `n` instructions skipped when S[sreg] is zero.
    Skipz {
        sreg: u32,
        n: u32,
    },
    /// A configuration word: the words' way of naming what an L1 op carries.
    Config(Config),
    Halt,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Config {
    Chk([(u32, u32); 4]),
    Chkvl(u32, [(u32, u32); 4]),
    Xb {
        xm: u32,
        xk: u32,
        sh: u32,
        xc: bool,
        pm: u32,
        pr: u32,
    },
    Ustr(i64, i64),
    Pstr(i64, i64),
}
