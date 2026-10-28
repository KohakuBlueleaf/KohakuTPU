//! Pack/unpack address and walk constructors.

use super::*;
use crate::lower::vector::gen::typed::{self as ir, Select, Symbol};

#[cfg(all(test, feature = "witness"))]
mod tests;

fn walk_option(walk: Walk) -> Term {
    ir::kw("walk", ir::walk(&walk.0).unwrap())
}

pub(super) fn unpack_line_typed(src: &Val, i: i64, dest: &Symbol) -> Stmt {
    let (word, mode, options) = if src.rows_w == 0 {
        let mode = if src.unpk.is_empty() {
            &src.mode
        } else {
            &src.unpk
        };
        (src.buf + i * src.per, mode.as_str(), vec![])
    } else {
        let nb = src.rows_w / 2;
        let (b, j) = (i / nb, i % nb);
        let base = src.buf + if src.bcast { 0 } else { 4 * b * src.rows_w } + 2 * j;
        let stride = if src.bcast { 0 } else { src.rows_w };
        (base, "f16", vec![walk_option(Walk::flat(stride, 1))])
    };
    ir::memory(
        ir::Memory::Unpack,
        dest.operand(Select::Whole),
        word,
        Some(mode),
        false,
        options,
    )
    .unwrap()
}

pub(super) fn pack_line_typed(
    d: &Desc,
    buf: i64,
    i: i64,
    mode: &str,
    dtype: &str,
    src: &Symbol,
) -> Stmt {
    let (word, mode, options) = if d.rows_w != 0 && !on("l2.vector/pack-walk-plain") {
        let nb = d.rows_w / 2;
        let (b, j) = (i / nb, i % nb);
        (
            buf + 4 * b * d.rows_w + 2 * j,
            "f16",
            vec![walk_option(Walk::flat(d.rows_w, 1))],
        )
    } else if dtype == "f32" {
        (buf + i * 16, "f32", vec![])
    } else {
        (buf + i * 8, mode, vec![])
    };
    ir::memory(
        ir::Memory::Pack,
        src.operand(Select::Whole),
        word,
        Some(mode),
        false,
        options,
    )
    .unwrap()
}
