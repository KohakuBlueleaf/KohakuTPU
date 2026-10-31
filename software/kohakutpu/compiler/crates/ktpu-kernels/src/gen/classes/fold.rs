//! Wide row folds: one column loop per repeated node-level row band
//! (`classes/fold.py`).

use super::{in_ports, rows_major, scalar, Classed, Rows};
use crate::expr::{self, emit::emit_over, Expr};
use crate::gen::vector::{stat_words, Kind, Operand};
use crate::gen::{cdiv, Hw};
use crate::kernel::{build, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, sl, Buf};
use crate::trace::control::Classed as Par;
use crate::trace::vector::Fold;
use crate::trace::{fail, Arg, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::json;
use std::collections::HashMap;

/// The class kernel of `a`, a root reduction over a wide row.
pub fn kernel(a: &Rows, m: &Machine) -> R<Classed> {
    let Rows {
        e,
        operands,
        r,
        w,
        out,
        bands,
        class,
        ..
    } = *a;
    match &**e {
        Expr::Reduce(_, arg) if expr::reduces(arg).is_empty() => {}
        _ => {
            return fail(
                "API.ROW.009",
                "a wide carry takes a root sum, max or prod without nested reductions",
            )
        }
    }
    if out.dtype != g::Dtype::F16 || operands.iter().any(|o| o.dtype != g::Dtype::F16) {
        return fail("API.ROW.009", "a wide fold stages f16 operands and output");
    }
    if out.kind != Kind::Row {
        return fail("API.ROW.009", "a wide carry takes a statistic output");
    }
    let hw = Hw::of(m);
    let height = hw.stat_rows();
    let trips = cdiv(r, height);
    let used = class.cores.used(hw.vc, trips);
    let repeats = cdiv(trips, used);
    let padded = repeats * used * height;
    let width = class.kblock;
    if w % width != 0 {
        return fail(
            "API.ROW.008",
            format!("{w} columns are not whole blocks of {width}"),
        );
    }
    let n = stat_words(r, &hw) * height;
    let (mut ports, mut given) = in_ports(operands, 0, r, w, &hw)?;
    let out_shape = [out.rows.max(out.offset + n)];
    ports.push(Port::output("out", &out_shape, g::Dtype::F16));
    given.push(("out".to_string(), f16(&out_shape)?));
    let band = used * height;
    let mut advances: Vec<i64> = operands
        .iter()
        .map(|o| match o.kind {
            Kind::Full | Kind::Tile => band * w * 2,
            Kind::Row => band * 2,
            Kind::Col | Kind::Scalar => 0,
        })
        .collect();
    advances.push(band * 2);
    let key = name(
        "rows_fold_class",
        vec![
            Py::Str(expr::py(e).repr()),
            Py::Tuple(operands.iter().map(Operand::py).collect()),
            Py::Int(r),
            Py::Int(w),
            out.py(),
            Py::Int(hw.vc),
            bands.map_or(Py::None, Py::Int),
            class.py(),
        ],
    );
    let kernel = build(&key, m, ports, given, |t: &mut Trace, b: &[Buf]| {
        let mut bufs = vec![];
        for (i, o) in operands.iter().enumerate() {
            let src = &b[i];
            bufs.push(if rows_major(o) {
                let buf = t.buffer(&[padded, w], g::Dtype::F16, vec![])?;
                t.fill_value(&buf.whole(), 0, None, None)?;
                let row_words = (w * 2 / hw.word_bytes, hw.word_bytes);
                t.copy_classed(
                    &buf.whole(),
                    &src.whole(),
                    (&[(r, o.pitch * 2), row_words], Some(o.offset * 2)),
                    (&[(r, w * 2), row_words], None),
                    None,
                )?;
                buf
            } else if o.kind == Kind::Row {
                let buf = t.buffer(&[padded], g::Dtype::F16, vec![])?;
                t.fill_value(&buf.whole(), 0, None, None)?;
                let view = src.at(&[sl(o.offset, o.offset + n)])?;
                t.copy(&buf.at(&[sl(0, n)])?, &view, None, None)?;
                buf
            } else {
                src.clone()
            });
        }
        let dst = t.buffer(&[padded], g::Dtype::F16, vec![])?;
        let (bufs, dst2) = (&bufs, &dst);
        let par = Par {
            band: None,
            repeats: Some(repeats),
            advances,
        };
        t.par_classed(used, par, |t, c| {
            let row = c.mul(height);
            let row_hi = row.add(height);
            let active = Idx::Int(used.min(trips) + used - 1).sub(&c).div(used);
            let (row, row_hi) = (&row, &row_hi);
            t.range(active.mul(w / width), |t, k| {
                let col = k.mul(width);
                let mut vals = HashMap::new();
                for (i, o) in operands.iter().enumerate() {
                    let v = match o.kind {
                        Kind::Full | Kind::Tile => {
                            let keys = [sl(row, row_hi), sl(&col, col.add(width))];
                            Arg::Val(t.load(&bufs[i].at(&keys)?, true)?)
                        }
                        Kind::Col => {
                            let lo = Idx::Int(o.offset).add(&col);
                            let view = bufs[i].at(&[sl(&lo, lo.add(width))])?;
                            Arg::Val(t.load(&view, true)?)
                        }
                        Kind::Row => {
                            let view = bufs[i].at(&[sl(row, row_hi)])?;
                            Arg::Val(t.load(&view, false)?)
                        }
                        Kind::Scalar => scalar(t, &bufs[i], o, &hw)?,
                    };
                    vals.insert(i, v);
                }
                let fold = Fold {
                    valid_cols: None,
                    carry: true,
                };
                let v = emit_over(t, e, &vals, fold)?;
                t.store(&dst2.at(&[sl(row, row_hi)])?, v)
            })
        })?;
        t.copy(
            &b[operands.len()].at(&[sl(out.offset, out.offset + n)])?,
            &dst.at(&[sl(0, n)])?,
            None,
            None,
        )
    })?;
    Ok(Classed {
        kernel,
        info: json!({
            "width": width,
            "trip_rows": height,
            "cores": used,
            "padded_rows": padded - r,
            "padded_columns": 0,
            "fold": true,
            "wide": true,
            "core_policy": class.cores.name(),
            "lowering": "node-repeat-column-carry",
            "blocks": w / width,
        }),
    })
}
