//! Classed mover kernels (`kernels/convert.py`, `transpose.py`, `matmul.py`
//! under a shape class): a copy, a walk, a transpose and a packing whose
//! counts are node loops.

use super::{Classed, Ports, ShapeClass};
use crate::gen::matmul::{self, Side};
use crate::gen::relayout;
use crate::gen::transpose;
use crate::gen::vector::Operand;
use crate::gen::{cdiv, Hw};
use crate::kernel::{build, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::f16;
use crate::trace::mover::Walk;
use crate::trace::{fail, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::json;

fn word_ports(src_words: i64, dst_words: i64, hw: &Hw) -> R<Ports> {
    let (src, dst) = ([src_words, hw.lanes], [dst_words, hw.lanes]);
    let ports = vec![
        Port::input("src", &src, g::Dtype::F16),
        Port::output("dst", &dst, g::Dtype::F16),
    ];
    let given = vec![
        ("src".to_string(), f16(&src)?),
        ("dst".to_string(), f16(&dst)?),
    ];
    Ok((ports, given))
}

/// `nbytes` from `src` to `dst` as one walk of whole words.
pub fn copy_kernel(nbytes: i64, class: &ShapeClass, m: &Machine) -> R<Classed> {
    let hw = Hw::of(m);
    let words = cdiv(nbytes, hw.word_bytes);
    let (ports, given) = word_ports(words, words, &hw)?;
    let key = name("copy", vec![Py::Int(words), class.py()]);
    let walk: &Walk = &[(words, hw.word_bytes)];
    let kernel = build(&key, m, ports, given, |t, b| {
        t.copy_classed(
            &b[1].whole(),
            &b[0].whole(),
            (walk, None),
            (walk, None),
            Some(2),
        )
    })?;
    Ok(Classed {
        kernel,
        info: json!({
            "copy": true,
            "padded_rows": 0,
            "padded_columns": 0,
            "core_policy": class.cores.name(),
        }),
    })
}

/// `dst` walked by `dlevels` from byte `dat` := `src` walked by `levels`
/// from byte `at`.
#[allow(clippy::too_many_arguments)]
pub fn walk_kernel(
    src_bytes: i64,
    levels: &Walk,
    at: i64,
    dst_bytes: i64,
    dlevels: &Walk,
    dat: i64,
    class: &ShapeClass,
    m: &Machine,
) -> R<Classed> {
    let hw = Hw::of(m);
    let (sw, dw) = (
        cdiv(src_bytes, hw.word_bytes),
        cdiv(dst_bytes, hw.word_bytes),
    );
    let (ports, given) = word_ports(sw, dw, &hw)?;
    let key = name(
        "walk",
        vec![
            Py::Int(sw),
            Py::walk(levels),
            Py::Int(at),
            Py::Int(dw),
            Py::walk(dlevels),
            Py::Int(dat),
            class.py(),
        ],
    );
    let kernel = build(&key, m, ports, given, |t, b| {
        t.copy_classed(
            &b[1].whole(),
            &b[0].whole(),
            (levels, Some(at)),
            (dlevels, Some(dat)),
            Some(2),
        )
    })?;
    Ok(Classed {
        kernel,
        info: json!({
            "walk": true,
            "levels": levels.len(),
            "dlevels": dlevels.len(),
            "padded_rows": 0,
            "padded_columns": 0,
            "core_policy": class.cores.name(),
        }),
    })
}

/// [`transpose::transpose_kernel`] with `class`'s cores.
pub fn transpose_kernel(nb: i64, a: i64, b: i64, class: &ShapeClass, m: &Machine) -> R<Classed> {
    let hw = Hw::of(m);
    let kernel = transpose::classed(nb, a, b, class, m)?;
    let lines = nb * a * b / hw.lanes / (hw.vlmax / hw.lanes);
    let trips = cdiv(lines, transpose::ROWS);
    let used = class.cores.used(hw.vc, trips);
    let padded = cdiv(trips, used) * used * transpose::ROWS;
    Ok(Classed {
        kernel,
        info: json!({
            "transpose": true,
            "cores": used,
            "padded_rows": padded - lines,
            "padded_columns": 0,
            "core_policy": class.cores.name(),
        }),
    })
}

/// The packing of [`matmul::quant_kernel_scheduled`] in a fixed entry loop;
/// `direct` the class's admission where it is asked for.
#[allow(clippy::too_many_arguments)]
pub fn quant_kernel(
    src: &Operand,
    rows: i64,
    cols: i64,
    rp: i64,
    cp: i64,
    side: Side,
    groups: i64,
    nk: i64,
    batches: i64,
    class: &ShapeClass,
    direct: Option<bool>,
    mach: &Machine,
    finite: bool,
    schedule: &[(&str, i64)],
    prepared: &[relayout::Plan],
    staged: bool,
) -> R<Classed> {
    let unpadded = rows == rp && cols == cp && src.pitch == cp && src.offset == 0;
    if direct == Some(true) && !unpadded {
        return fail(
            "API.FORM.006",
            "a direct quant class takes unpadded contiguous rows",
        );
    }
    let kernel = matmul::quant_kernel_looped(
        src, rows, cols, rp, cp, side, groups, nk, batches, mach, finite, schedule, prepared,
        staged, true,
    )?;
    Ok(Classed {
        kernel,
        info: json!({
            "quant": true,
            "direct": direct.unwrap_or(unpadded),
            "g": groups,
            "nk": nk,
            "kblock": class.kblock,
            "padded_rows": batches * (rp - rows),
            "padded_columns": cp - cols,
            "core_policy": class.cores.name(),
        }),
    })
}
