//! The debug region's layout and record words, as
//! `software/firmware/kohakuaccel/include/ka/package/debug.h` declares them
//! (a test reads the header and checks every value here).

/// "KADBG1".
pub const MAGIC: u64 = 0x3147_4244_414b;
pub const VERSION: u64 = 1;

/// Header word offsets, bytes.
pub const H_MAGIC: usize = 0x00;
pub const H_VERSION: usize = 0x08;
pub const H_LAST: usize = 0x10;
pub const H_UNITS: usize = 0x18;
pub const H_REC: usize = 0x20;
pub const H_RECCAP: usize = 0x28;
pub const H_WR: usize = 0x30;
pub const H_LOST: usize = 0x38;
pub const H_RD: usize = 0x40;
/// Per package unit: its completion count, then N words.
pub const LASTS: usize = 0x80;
pub const REC_BYTES: usize = 64;

/// Record types (word 0 [7:0]).
pub const STEP: u64 = 1;
pub const UNIT: u64 = 2;
pub const FAIL: u64 = 3;

/// STEP flags: the cycle is when the step was queued to the dispatch engine.
pub const STEP_ENGINE: u64 = 1;
/// FAIL flags.
pub const FAIL_ENGINE: u64 = 1;
pub const FAIL_DRAINED: u64 = 2;

/// Features (`.ka_abi` word 2, machine `fw.debug.features`).
pub const F_STEPS: u64 = 1;
pub const F_FAIL: u64 = 2;
pub const F_LAST: u64 = 4;

/// Run flags (`KA_RUN_F_*`, SQ word 6) the debug image reads.
pub const RUN_TRACE: u64 = 4;
pub const RUN_NOENGINE: u64 = 8;

/// The opcodes whose step word `[31:16]` is a package unit index (0xffff:
/// any unit, for RECV); for the others it is a register, mesh, slot or step.
pub const UNIT_OPS: [&str; 6] = ["DISPATCH", "AWAIT", "REPEAT", "FETCH", "SEND", "RECV"];

/// Package step opcodes (`enum ka_step_op`, ka/package/format.h), by code.
pub const OPS: [&str; 22] = [
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
];
