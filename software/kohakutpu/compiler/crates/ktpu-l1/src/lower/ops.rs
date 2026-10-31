//! The V2 vector core's math mnemonics as L1 names them, and what each reads
//! and writes (L1.IMG.005-.007, .051).

use crate::mutant::on;

/// One source (a).
pub const UNARY: [&str; 7] = ["vmov", "vneg", "vabs", "vexp2", "vlog2", "vinv", "vrsqrt"];
/// Reads a second source (b).
pub const READS_B: [&str; 10] = [
    "vmul", "vfma", "vfnma", "vmax", "vmin", "vsel", "vcmplt", "vcmpgt", "vcmpeq", "vexp2d",
];
/// Reads a third source (c).
pub const READS_C: [&str; 5] = ["vadd", "vsub", "vfma", "vfnma", "vsel"];
/// Writes a predicate, not a register.
pub const COMPARE: [&str; 3] = ["vcmplt", "vcmpgt", "vcmpeq"];

pub fn reads_b(op: &str) -> bool {
    READS_B.contains(&op)
}

pub fn reads_c(op: &str) -> bool {
    READS_C.contains(&op) && !(op == "vadd" && on("l1.img/reads"))
}

pub fn is_compare(op: &str) -> bool {
    COMPARE.contains(&op) && !on("l1.img/compare-pred")
}

/// Every math mnemonic.
pub fn is_math(op: &str) -> bool {
    UNARY.contains(&op) || READS_B.contains(&op) || READS_C.contains(&op)
}
