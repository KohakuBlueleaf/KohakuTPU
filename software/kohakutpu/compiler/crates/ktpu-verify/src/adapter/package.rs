//! A bound package as the checker reads it (from `ktpu_l0::Package`,
//! `l0.rs`): its units, steps and payloads, with the opcodes and flags of
//! `l0/steps.md`.

use super::u256::U256;

/// Step opcodes (L0.STEP.*).
pub mod op {
    pub const END: u8 = 0;
    pub const DISPATCH: u8 = 1;
    pub const AWAIT: u8 = 2;
    pub const BARRIER: u8 = 3;
    pub const RING: u8 = 5;
    pub const WAIT_BELL: u8 = 6;
    pub const REPEAT: u8 = 9;
    pub const ENGINE: u8 = 11;
    pub const FETCH: u8 = 12;
    pub const WAIT_SLOT: u8 = 13;
    pub const LOAD: u8 = 15;
    pub const ALU: u8 = 16;
    pub const JUMP: u8 = 17;
    pub const PATCH: u8 = 18;
    pub const SEND: u8 = 19;
    pub const RECV: u8 = 20;

    /// The opcode's name (`"WAIT_BELL"`).
    pub fn name(op: u8) -> String {
        ktpu_l0::Op::from_code(op as u64)
            .map(|o| o.name().to_string())
            .unwrap_or_else(|| format!("OP{op}"))
    }
}

/// Step flags.
pub mod flag {
    pub const IMM: u8 = 2;
    pub const CONSUME: u8 = 32;
    pub const MEM: u8 = 64;
    pub const ACK: u8 = 128;
}

/// A PATCH argument's `steps` bit.
pub const PATCH_STEPS: u64 = 1 << 24;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Unit {
    pub ty: u16,
    pub x: u8,
    pub y: u8,
    pub mesh: u8,
    pub credit: u32,
    pub fetch: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Step {
    pub op: u8,
    pub flags: u8,
    pub unit: u16,
    pub count: u32,
    pub arg: u64,
}

impl Step {
    pub fn new(op: u8, unit: u16, count: u32, arg: u64, flags: u8) -> Self {
        Step {
            op,
            flags,
            unit,
            count,
            arg,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Hash, Default)]
pub struct Package {
    pub units: Vec<Unit>,
    pub steps: Vec<Step>,
    pub payloads: Vec<U256>,
}
