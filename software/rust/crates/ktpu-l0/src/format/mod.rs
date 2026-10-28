//! The package: units, buffers, steps, relocations and payloads
//! (docs/spec/stack/l0/package.md); `wire` is its bytes, `bind` its binding.

pub mod bind;
pub mod cut;
pub mod template;
pub mod wire;

use crate::error::Result;
use crate::refuse;
use crate::word::Word;

pub const ALIGN: usize = 32;
pub const HEADER_WORDS: usize = 12;
pub const UNIT_BYTES: usize = 16;
pub const BUFFER_BYTES: usize = 32;
pub const STEP_BYTES: usize = 16;
pub const RELOC_BYTES: usize = 16;
pub const PAYLOAD_BYTES: usize = 32;

pub const FLAG_CHECKSUM: u64 = 1;
/// The package RECVs: flits in the node's RX queue are its own.
pub const FLAG_RX: u64 = 2;
/// Header word holding the checksum; it reads as zero while the sum is taken.
pub const CHECK_WORD: usize = 10;

pub const FNV_BASIS: u64 = 0xCBF2_9CE4_8422_2325;
pub const FNV_PRIME: u64 = 0x0000_0100_0000_01B3;

/// A MOVER / STORE / CTRL pair whose register (address) is this is skipped.
pub const MOVER_SKIP: u64 = u64::MAX;

/// A unit table credit meaning no credit bound (L0.PKG.035).
pub const CREDIT_UNBOUNDED: u64 = 0;

/// The in-flight bound "no credit bound" stands for, past any count a
/// package reaches (RUL.042): the writer, `engine::lower` and the verifier
/// read this one value.
pub const UNBOUNDED: u64 = 1 << 32;

/// A binding meaning "unbound: take the buffer's default"; address 0 binds
/// like any other (RUL.040).
pub const UNBOUND: u64 = u64::MAX;

/// Step opcodes (L0.PKG.025, steps.md).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
#[repr(u8)]
pub enum Op {
    End = 0,
    Dispatch = 1,
    Await = 2,
    Barrier = 3,
    Mover = 4,
    Ring = 5,
    WaitBell = 6,
    Signal = 7,
    Settle = 8,
    Repeat = 9,
    Mwait = 10,
    Engine = 11,
    Fetch = 12,
    WaitSlot = 13,
    Store = 14,
    Load = 15,
    Alu = 16,
    Jump = 17,
    Patch = 18,
    Send = 19,
    Recv = 20,
    Ctrl = 21,
    AssertZero = 22,
}

impl Op {
    pub const ALL: [Op; 23] = [
        Op::End,
        Op::Dispatch,
        Op::Await,
        Op::Barrier,
        Op::Mover,
        Op::Ring,
        Op::WaitBell,
        Op::Signal,
        Op::Settle,
        Op::Repeat,
        Op::Mwait,
        Op::Engine,
        Op::Fetch,
        Op::WaitSlot,
        Op::Store,
        Op::Load,
        Op::Alu,
        Op::Jump,
        Op::Patch,
        Op::Send,
        Op::Recv,
        Op::Ctrl,
        Op::AssertZero,
    ];

    pub fn from_code(code: u64) -> Option<Op> {
        Op::ALL.get(usize::try_from(code).ok()?).copied()
    }

    /// The Python enum name (`"WAIT_BELL"`).
    pub fn name(self) -> &'static str {
        [
            "END",
            "DISPATCH",
            "AWAIT",
            "BARRIER",
            "MOVER",
            "RING",
            "WAIT_BELL",
            "SIGNAL",
            "SETTLE",
            "REPEAT",
            "MWAIT",
            "ENGINE",
            "FETCH",
            "WAIT_SLOT",
            "STORE",
            "LOAD",
            "ALU",
            "JUMP",
            "PATCH",
            "SEND",
            "RECV",
            "CTRL",
            "ASSERT_ZERO",
        ][self as usize]
    }

    pub fn from_name(name: &str) -> Option<Op> {
        Op::ALL.iter().copied().find(|o| o.name() == name)
    }
}

/// Step flags. A MOVER step POSTED starts its moves and goes on.
pub const F_POSTED: u8 = 1;
/// ALU / JUMP: operand b is the argument, not register rb.
pub const F_IMM: u8 = 2;
/// STORE / CTRL: a pair's value names a register.
pub const F_REG: u8 = 4;
/// LOAD: the address adds register count[3:0].
pub const F_INDEXED: u8 = 8;
/// LOAD: the address is a control-region offset.
pub const F_CTRL: u8 = 16;
/// WAIT_SLOT: take the count from the slot once it is reached.
pub const F_CONSUME: u8 = 32;
/// SEND: a CU_DATA descriptor payload, then `count` words from memory.
/// Host FETCH: arg selects an address payload, resolved and cleared by bind.
pub const F_MEM: u8 = 64;
/// SEND: wait for the unit's DATA_RECEIVED.
pub const F_ACK: u8 = 128;

/// RECV from any unit.
pub const ANY_UNIT: u64 = 0xFFFF;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Alu {
    Mov = 0,
    Add = 1,
    Sub = 2,
    Mul = 3,
    And = 4,
    Or = 5,
    Xor = 6,
    Shl = 7,
    Shr = 8,
    Min = 9,
    Max = 10,
    Divu = 11,
    Remu = 12,
}

impl Alu {
    pub const ALL: [Alu; 13] = [
        Alu::Mov,
        Alu::Add,
        Alu::Sub,
        Alu::Mul,
        Alu::And,
        Alu::Or,
        Alu::Xor,
        Alu::Shl,
        Alu::Shr,
        Alu::Min,
        Alu::Max,
        Alu::Divu,
        Alu::Remu,
    ];

    pub fn from_code(code: u64) -> Option<Alu> {
        Alu::ALL.get(usize::try_from(code).ok()?).copied()
    }
}

impl Cond {
    pub const ALL: [Cond; 7] = [
        Cond::Always,
        Cond::Eq,
        Cond::Ne,
        Cond::Ltu,
        Cond::Geu,
        Cond::Lt,
        Cond::Ge,
    ];

    pub fn from_code(code: u64) -> Option<Cond> {
        Cond::ALL.get(usize::try_from(code).ok()?).copied()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Cond {
    Always = 0,
    Eq = 1,
    Ne = 2,
    Ltu = 3,
    Geu = 4,
    Lt = 5,
    Ge = 6,
}

/// What a buffer is to the package's caller (L0.PKG.024).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Kind {
    Input = 0,
    Output = 1,
    Inout = 2,
    Temp = 3,
    Const = 4,
}

impl Kind {
    pub const ALL: [Kind; 5] = [
        Kind::Input,
        Kind::Output,
        Kind::Inout,
        Kind::Temp,
        Kind::Const,
    ];

    pub fn from_code(code: u64) -> Option<Kind> {
        Kind::ALL.get(usize::try_from(code).ok()?).copied()
    }

    pub fn name(self) -> &'static str {
        ["INPUT", "OUTPUT", "INOUT", "TEMP", "CONST"][self as usize]
    }

    pub fn from_name(name: &str) -> Option<Kind> {
        Kind::ALL.iter().copied().find(|k| k.name() == name)
    }
}

/// FNV-1a 64 over `data`, from `h`.
pub fn fnv1a_from(data: &[u8], mut h: u64) -> u64 {
    for &b in data {
        h = (h ^ b as u64).wrapping_mul(FNV_PRIME);
    }
    h
}

pub fn fnv1a(data: &[u8]) -> u64 {
    fnv1a_from(data, FNV_BASIS)
}

/// A unit as one word: `type << 32 | mesh << 16 | y << 8 | x` (L0.PKG.016).
pub fn unit_word(type_code: u64, x: u64, y: u64, mesh: u64) -> u64 {
    if crate::mutant::on("l0.pkg/unit_word") {
        return (type_code & 0xFFFF) << 32 | (mesh & 0xFF) << 16 | (x & 0xFF) << 8 | y;
    }
    (type_code & 0xFFFF) << 32 | (mesh & 0xFF) << 16 | (y & 0xFF) << 8 | x & 0xFF
}

/// Two ASCII characters -> the 16-bit CU_TYPE, the first high (L0.PKG.017).
pub fn type_code(name: &str) -> Result<u64> {
    let raw = name.as_bytes();
    if raw.len() != 2 || !name.is_ascii() {
        return refuse!(
            "L0.PKG.017",
            "a unit type is exactly two ASCII characters, got {name:?}"
        );
    }
    if crate::mutant::on("l0.pkg/type_code") {
        return Ok((raw[1] as u64) << 8 | raw[0] as u64);
    }
    Ok((raw[0] as u64) << 8 | raw[1] as u64)
}

/// 0x4D47 -> "MG"; a byte outside ASCII reads as U+FFFD.
pub fn type_name(code: u64) -> String {
    [(code >> 8) & 0xFF, code & 0xFF]
        .iter()
        .map(|&b| {
            if b < 0x80 {
                b as u8 as char
            } else {
                '\u{FFFD}'
            }
        })
        .collect()
}

/// The machine signature: FNV-1a 64 over the sorted unit words, 8 bytes each
/// little-endian (L0.PKG.031).
pub fn signature(unit_words: &[u64]) -> u64 {
    let mut w = unit_words.to_vec();
    w.sort_unstable();
    let bytes: Vec<u8> = w.iter().flat_map(|v| v.to_le_bytes()).collect();
    fnv1a(&bytes)
}

/// A PATCH step's argument: bits `[bit, bit+width)` of a payload (or, with
/// `steps`, a step) := register >> shift, or += it with `add` (L0.STEP.053).
pub fn patch_arg(bit: u64, width: u64, shift: u64, steps: bool, add: bool) -> Result<u64> {
    let room = if steps { 128 } else { 256 };
    if !(1..=64).contains(&width) || shift >= 64 || bit + width > room {
        return refuse!(
            "L0.STEP.072",
            "patch field [{bit}+{width}] >> {shift} leaves the word"
        );
    }
    let bit = bit + crate::mutant::on("l0.build/patch_field_off") as u64;
    let shift_lsb = if crate::mutant::on("l0.build/patch_arg") {
        17
    } else {
        16
    };
    Ok(bit | width << 8 | shift << shift_lsb | (steps as u64) << 24 | (add as u64) << 26)
}

/// A unit the package dispatches to (L0.PKG.016). `credit` bounds its
/// in-flight count; `fetch` is `1 << 16 | y << 8 | x` of the memory port that
/// streams its DISPATCH words, 0 for the mailbox.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Unit {
    pub type_code: u64,
    pub x: u64,
    pub y: u64,
    pub mesh: u64,
    pub credit: u64,
    pub fetch: u64,
}

impl Unit {
    pub fn word(&self) -> u64 {
        unit_word(self.type_code, self.x, self.y, self.mesh)
    }

    pub fn coord(&self) -> (u64, u64) {
        (self.x, self.y)
    }

    pub fn has_port(&self) -> bool {
        self.fetch >> 16 != 0
    }
}

/// A memory range the words address; `default` is used when unbound.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Buffer {
    pub name: String,
    pub size: u64,
    pub default: u64,
    pub kind: Kind,
    pub mesh: u64,
}

/// Bits `[bit, bit+width)` of payload `payload` := `(bind + addend) >> shift`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Reloc {
    pub payload: u64,
    pub bit: u64,
    pub width: u64,
    pub buffer: u64,
    pub addend: i128,
    pub shift: u64,
}

impl Reloc {
    /// L0.PKG.032: the field inside 256 bits, width 1..64, shift 0..63,
    /// buffer 0..255.
    pub fn check(&self) -> Result<()> {
        if !(1..=64).contains(&self.width) || self.bit + self.width > 256 {
            return refuse!(
                "L0.PKG.032",
                "relocation field [{}+{}] leaves 256 bits",
                self.bit,
                self.width
            );
        }
        if self.shift >= 64 {
            return refuse!("L0.PKG.032", "relocation shift {} is not 0..63", self.shift);
        }
        if self.buffer >= 256 {
            return refuse!(
                "L0.PKG.032",
                "relocation buffer {} is not 0..255",
                self.buffer
            );
        }
        Ok(())
    }
}

/// One step: an opcode, a unit (or mesh, slot, register, step), a count, a
/// 64-bit argument, flags (L0.PKG.025).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Step {
    pub op: Op,
    pub unit: u64,
    pub count: u64,
    pub arg: u64,
    pub flags: u8,
}

impl Step {
    pub fn new(op: Op, unit: u64, count: u64, arg: u64, flags: u8) -> Step {
        Step {
            op,
            unit,
            count,
            arg,
            flags,
        }
    }

    pub fn bare(op: Op) -> Step {
        Step::new(op, 0, 0, 0, 0)
    }
}

/// A package in memory; `wire` has its bytes.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Package {
    pub units: Vec<Unit>,
    pub buffers: Vec<Buffer>,
    pub steps: Vec<Step>,
    pub relocs: Vec<Reloc>,
    pub payloads: Vec<Word>,
    pub signature: u64,
    /// Completions that may arrive from units this package never sent to.
    pub ack_reserve: u64,
    pub checksum: bool,
    /// Buffer names, kept for the host; the wire form carries their hashes.
    pub buffer_names: Vec<String>,
}

impl Package {
    /// Whether any step jumps: its steps may run in another order, or again.
    pub fn jumps(&self) -> bool {
        self.steps.iter().any(|s| s.op == Op::Jump)
    }

    pub fn summary(&self) -> String {
        let mut ops: std::collections::BTreeMap<&str, usize> = Default::default();
        for s in &self.steps {
            *ops.entry(s.op.name()).or_default() += 1;
        }
        let mix: Vec<String> = ops.iter().map(|(k, v)| format!("{k}={v}")).collect();
        format!(
            "package: {} units, {} payloads, {} relocations over {} buffers, {} steps ({})",
            self.units.len(),
            self.payloads.len(),
            self.relocs.len(),
            self.buffers.len(),
            self.steps.len(),
            mix.join(" ")
        )
    }
}
