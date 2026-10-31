//! Reduction trees, statistic packing and the quantiser's chunk order
//! (L2.OP.011-.015, L2.STAT.003).

use super::sign::ZeroSign;

pub(super) mod typed;

/// Whether a tree of `code` folds lanes by xor alone: `zero_sign` rewrites it
/// as compare and copies, and a merge also redirects the a operand
/// (ISA.VC.076), so no copy of a merge operand is exact. These trees and the
/// compact are the only producers of a merge pick.
pub fn lane_folds(zero_sign: ZeroSign, code: &str) -> bool {
    zero_sign.picks(code) && !crate::mutant::on("l2.vector/merge-tree-picked")
}
