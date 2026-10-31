//! Rows staged into class-width scratch, with one fixed trip and a masked
//! tail (`classes/rows.py`, `classes/flat.py`).

use super::{core_trips, in_ports, rows_major, scalar, trip_row, wide, Classed, Rows};
use crate::expr::{self, emit::emit_over};
use crate::gen::vector::{
    holds_stat, port_shape, prologue, rows_block, stat_words, store_over, Bufs, Kind, Operand,
    Origin, Plan, L1_MARGIN, TRIP_BANDS,
};
use crate::gen::{cdiv, Hw};
use crate::kernel::{build, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, Buf, ALL};
use crate::trace::control::Classed as Par;
use crate::trace::vector::Fold;
use crate::trace::{fail, Arg, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::json;
use std::collections::HashMap;

/// Single-buffered class bands of `width` columns the L1 holds.
pub fn bands_fit(operands: &[Operand], out: &Operand, width: i64, hw: &Hw) -> i64 {
    let band = |d: g::Dtype| hw.band * width * Hw::item(d) / hw.word_bytes;
    let mut words: i64 = operands
        .iter()
        .filter(|o| rows_major(o))
        .map(|o| band(o.dtype))
        .sum();
    words += operands
        .iter()
        .filter(|o| matches!(o.kind, Kind::Row | Kind::Scalar))
        .count() as i64;
    words += if out.kind == Kind::Row {
        1
    } else {
        band(out.dtype)
    };
    let prologue: i64 = operands
        .iter()
        .filter(|o| o.kind == Kind::Col)
        .map(|_| width * 2 / hw.word_bytes)
        .sum();
    (hw.l1_words - L1_MARGIN - prologue).div_euclid(words)
}

/// `(bands a trip takes, bands the L1 holds)` of class rows of `width`
/// columns.
fn trip(a: &Rows, width: i64, flat: bool, hw: &Hw) -> (i64, i64) {
    let quantum = if holds_stat(a.operands, a.out.kind) && !flat {
        hw.stat_rows()
    } else {
        hw.band
    };
    let fit = bands_fit(a.operands, a.out, width, hw);
    let wanted = match a.bands {
        Some(b) if b != 0 => b,
        _ => TRIP_BANDS.min(fit),
    };
    ((quantum / hw.band).max(wanted), fit)
}

/// Whether `a`'s class trip fits the L1.
pub fn fits(a: &Rows, m: &Machine) -> R<bool> {
    let hw = Hw::of(m);
    let (selected, fit) = trip(a, a.class.width(a.w)?, false, &hw);
    Ok(selected <= fit)
}

/// The class kernel of `a` (`flat`: row-major rows, no GT4 band).
pub fn kernel(a: &Rows, flat: bool, m: &Machine) -> R<Classed> {
    if a.w > a.class.max_block {
        return wide::kernel(a, m);
    }
    let hw = Hw::of(m);
    let Rows {
        e,
        operands,
        r,
        w,
        out,
        class,
        schedule,
        ..
    } = *a;
    let band = schedule.band;
    let width = class.width(w)?;
    let (selected, fit) = trip(a, width, flat, &hw);
    if selected > fit {
        return fail(
            "API.ROW.002",
            format!("{selected} bands of {width} columns do not fit class L1"),
        );
    }
    let rows = selected * hw.band;
    let pl = Plan {
        wc: width,
        nblk: 1,
        rows,
        nb: 1,
        rp: rows,
    };
    let trips = cdiv(r, rows);
    let used = class.cores.used(hw.vc, trips);
    let padded = cdiv(trips, used) * used * rows;
    let sr = hw.stat_rows();
    let count = stat_words(r, &hw) * sr;
    let (mut ports, mut given) = in_ports(operands, 0, r, w, &hw)?;
    let out_shape = if out.kind == Kind::Row {
        vec![out.rows.max(count)]
    } else {
        port_shape(out, r, w, &hw)?
    };
    ports.push(Port::output("out", &out_shape, out.dtype));
    given.push((
        "out".to_string(),
        spec(&out_shape, out.dtype, vec![], g::BufSpace::Dram)?,
    ));
    let direct = schedule.rows_direct
        && !flat
        && w == width
        && r % rows == 0
        && (out.kind != Kind::Row || out.offset == 0);
    let key = name(
        "rows_class",
        vec![
            Py::Str(expr::py(e).repr()),
            Py::Tuple(operands.iter().map(Operand::py).collect()),
            Py::Int(r),
            Py::Int(w),
            out.py(),
            pl.py(),
            class.py(),
            Py::Int(hw.vc),
            Py::Bool(flat),
            Py::Int(band),
        ],
    );
    let key = if direct {
        name("rows_class_direct", vec![Py::Str(key)])
    } else {
        key
    };
    let par = || Par {
        band: Some(band),
        ..Par::default()
    };
    let staged = |t: &mut Trace, b: &[Buf]| -> R<()> {
        let mut bufs = vec![];
        for (i, o) in operands.iter().enumerate() {
            let src = &b[i];
            let item = Hw::item(o.dtype);
            let wb = hw.word_bytes;
            bufs.push(match o.kind {
                Kind::Full | Kind::Tile => {
                    let buf = t.buffer(&[padded, width], o.dtype, vec![])?;
                    t.fill_value(&buf.whole(), 0, None, None)?;
                    let row_words = (w * item / wb, wb);
                    t.copy_classed(
                        &buf.whole(),
                        &src.whole(),
                        (&[(r, o.pitch * item), row_words], Some(o.offset * item)),
                        (&[(r, width * item), row_words], None),
                        None,
                    )?;
                    buf
                }
                Kind::Col => {
                    let buf = t.buffer(&[width], o.dtype, vec![])?;
                    t.fill_value(&buf.whole(), 0, None, None)?;
                    let view = src.at(&[sl(o.offset, o.offset + w)])?;
                    t.copy(&buf.at(&[sl(0, w)])?, &view, None, None)?;
                    buf
                }
                Kind::Row => {
                    let buf = t.buffer(&[padded], o.dtype, vec![])?;
                    t.fill_value(&buf.whole(), 0, None, None)?;
                    let view = src.at(&[sl(o.offset, o.offset + count)])?;
                    t.copy(&buf.at(&[sl(0, count)])?, &view, None, None)?;
                    buf
                }
                Kind::Scalar => src.clone(),
            });
        }
        let shape = if out.kind == Kind::Row {
            vec![padded]
        } else {
            vec![padded, width]
        };
        let dst = t.buffer(&shape, out.dtype, vec![])?;
        let (bufs, dst2) = (&bufs, &dst);
        t.par_classed(used, par(), |t, c| {
            let mut pro = HashMap::new();
            for (i, o) in operands.iter().enumerate() {
                if o.kind == Kind::Col {
                    let l = t.load(&bufs[i].at(&[sl(0, width)])?, true)?;
                    pro.insert(i, Arg::Val(t.unpack(l, None)?));
                }
            }
            let c2 = c.clone();
            t.range(core_trips(trips, used, &c), |t, k| {
                let row = trip_row(&c2, &k, used, rows);
                let hi = row.add(rows);
                let mut vals = pro.clone();
                for (i, o) in operands.iter().enumerate() {
                    match o.kind {
                        Kind::Full | Kind::Tile => {
                            let view = bufs[i].at(&[sl(&row, &hi), ALL])?;
                            vals.insert(i, Arg::Val(t.load(&view, !flat)?));
                        }
                        Kind::Row => {
                            let view = bufs[i].at(&[sl(&row, &hi)])?;
                            vals.insert(i, Arg::Val(t.load(&view, false)?));
                        }
                        Kind::Scalar => {
                            let v = scalar(t, &bufs[i], o, &hw)?;
                            vals.insert(i, v);
                        }
                        Kind::Col => {}
                    }
                }
                let fold = Fold {
                    valid_cols: Some(w),
                    carry: false,
                };
                let v = emit_over(t, e, &vals, fold)?;
                if out.kind == Kind::Row {
                    t.store(&dst2.at(&[sl(&row, &hi)])?, v)
                } else {
                    store_over(t, dst2, &row, &Idx::Int(0), rows, width, v)
                }
            })
        })?;
        let port = &b[operands.len()];
        if out.kind == Kind::Row {
            t.copy(
                &port.at(&[sl(0, count)])?,
                &dst.at(&[sl(0, count)])?,
                None,
                None,
            )
        } else {
            let item = Hw::item(out.dtype);
            let row_words = (w * item / hw.word_bytes, hw.word_bytes);
            t.copy_classed(
                &port.whole(),
                &dst.whole(),
                (&[(r, width * item), row_words], None),
                (&[(r, out.pitch * item), row_words], Some(out.offset * item)),
                None,
            )
        }
    };
    let in_place = |t: &mut Trace, b: &[Buf]| -> R<()> {
        let bufs = Bufs {
            ins: b[..operands.len()].to_vec(),
            out: b[operands.len()].clone(),
            maps: Default::default(),
        };
        let bufs = &bufs;
        t.par_classed(used, par(), |t, c| {
            let pro = prologue(t, bufs, operands, &pl, 0, &Idx::Int(0))?;
            let c2 = c.clone();
            t.range(core_trips(trips, used, &c), |t, k| {
                let row = trip_row(&c2, &k, used, rows);
                let at = Origin::rows(&row, operands.len());
                rows_block(t, bufs, e, operands, out, &pl, &at, 0, &pro, &hw)
            })
        })
    };
    let kernel = if direct {
        build(&key, m, ports, given, in_place)?
    } else {
        build(&key, m, ports, given, staged)?
    };
    Ok(Classed {
        kernel,
        info: json!({
            "direct": direct,
            "width": width,
            "trip_rows": rows,
            "cores": used,
            "padded_rows": padded - r,
            "padded_columns": width - w,
            "lowering": format!("serial-band{band}-hold-pin"),
            "core_policy": class.cores.name(),
            "blocks": 1,
        }),
    })
}

/// `e` over `n` elements as rows of `vc.vlmax` values: every operand at the
/// output's index but `(i, lane)` of `scalars`.
#[allow(clippy::too_many_arguments)]
pub fn flat_kernel(
    e: &expr::E,
    dtypes: &[g::Dtype],
    n: i64,
    out_dtype: g::Dtype,
    scalars: &[(usize, i64)],
    class: &super::ShapeClass,
    schedule: &super::Schedule,
    m: &Machine,
) -> R<Classed> {
    let hw = Hw::of(m);
    let lane_of = |i: usize| scalars.iter().rev().find(|(j, _)| *j == i).map(|(_, l)| *l);
    let operands: Vec<Operand> = dtypes
        .iter()
        .enumerate()
        .map(|(i, d)| match lane_of(i) {
            Some(lane) => Operand {
                dtype: *d,
                offset: lane,
                ..Operand::new(Kind::Scalar)
            },
            None => Operand {
                dtype: *d,
                pitch: hw.vlmax,
                ..Operand::new(Kind::Full)
            },
        })
        .collect();
    let out = Operand {
        dtype: out_dtype,
        pitch: hw.vlmax,
        ..Operand::new(Kind::Full)
    };
    let rows = Rows {
        e,
        operands: &operands,
        r: cdiv(n, hw.vlmax),
        w: hw.vlmax,
        out: &out,
        bands: None,
        class,
        schedule,
    };
    let mut got = kernel(&rows, true, m)?;
    got.info["flat"] = json!(true);
    got.info["padded_elements"] = json!((-n).rem_euclid(hw.vlmax));
    Ok(got)
}
