//! Wide elementwise rows streamed in block-major scratch order
//! (`classes/wide.py`).

use super::{core_trips, fold, in_ports, scalar, trip_row, zero, Classed, Rows};
use crate::expr;
use crate::gen::vector::{
    holds_stat, port_shape, prologue, rows_block, stat_words, store_over, Bufs, Kind, Operand,
    Origin, Plan,
};
use crate::gen::{cdiv, Hw};
use crate::kernel::{build, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, Buf, ALL};
use crate::trace::control::Classed as Par;
use crate::trace::{fail, Arg, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::json;
use std::collections::HashMap;

fn gcd(a: i64, b: i64) -> i64 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

/// The class kernel of `a`, wider than a class block.
pub fn kernel(a: &Rows, m: &Machine) -> R<Classed> {
    let Rows {
        e,
        operands,
        r,
        w,
        out,
        bands,
        class,
        schedule,
    } = *a;
    if !expr::reduces(e).is_empty() || out.kind == Kind::Row {
        return fold::kernel(a, m);
    }
    let hw = Hw::of(m);
    let wb = hw.word_bytes;
    let preferred = schedule.wide_width.unwrap_or(class.kblock);
    if preferred % class.kblock != 0 || !(class.kblock..=class.max_block).contains(&preferred) {
        return fail(
            "API.ROW.008",
            format!("a wide block of {preferred} is not whole blocks within the class"),
        );
    }
    if w % class.kblock != 0 {
        return fail(
            "API.ROW.008",
            format!("{w} columns are not whole blocks of {}", class.kblock),
        );
    }
    let width = gcd(w, preferred);
    let quantum = if holds_stat(operands, out.kind) {
        hw.stat_rows()
    } else {
        hw.band
    };
    let height = quantum.max(bands.filter(|b| *b != 0).unwrap_or(1) * hw.band);
    if height % quantum != 0 {
        return fail(
            "API.ROW.002",
            format!("a wide trip of {height} rows is not whole statistic words"),
        );
    }
    let block_rows = cdiv(r, height) * height;
    let nblocks = w / width;
    let trips = nblocks * block_rows / height;
    let mut used = class.cores.used(hw.vc, trips);
    let total = cdiv(trips, used) * used * height;
    let direct = schedule.wide_direct && r % height == 0;
    if direct {
        used = used.min(r / height);
    }
    let (mut ports, mut given) = in_ports(operands, 0, r, w, &hw)?;
    let out_shape = port_shape(out, r, w, &hw)?;
    ports.push(Port::output("out", &out_shape, out.dtype));
    given.push((
        "out".to_string(),
        spec(&out_shape, out.dtype, vec![], g::BufSpace::Dram)?,
    ));
    let key = name(
        "rows_wide_class",
        vec![
            Py::Str(expr::py(e).repr()),
            Py::Tuple(operands.iter().map(Operand::py).collect()),
            Py::Int(r),
            Py::Int(w),
            out.py(),
            Py::Int(hw.vc),
            bands.map_or(Py::None, Py::Int),
            class.py(),
            Py::Int(width),
            Py::Bool(direct),
        ],
    );
    let in_place = |t: &mut Trace, b: &[Buf]| -> R<()> {
        let plan = Plan {
            wc: width,
            nblk: nblocks,
            rows: height,
            nb: r / height,
            rp: r,
        };
        let plan = &plan;
        let bufs = Bufs {
            ins: b[..operands.len()].to_vec(),
            out: b[operands.len()].clone(),
            maps: Default::default(),
        };
        let bufs = &bufs;
        let hw = &hw;
        t.range(nblocks, |t, block| {
            let block = block.mul(width);
            let block = &block;
            t.par_classed(used, Par::default(), |t, c| {
                let pro = prologue(t, bufs, operands, plan, 0, block)?;
                let c2 = c.clone();
                let count = core_trips(r / height, used, &c);
                t.range(count, |t, k| {
                    let row = trip_row(&c2, &k, used, height);
                    let at = Origin {
                        ins: vec![(row.clone(), block.clone()); operands.len()],
                        out: (row, block.clone()),
                    };
                    rows_block(t, bufs, e, operands, out, plan, &at, 0, &pro, hw)
                })
            })
        })
    };
    let staged = |t: &mut Trace, b: &[Buf]| -> R<()> {
        let mut bufs = vec![];
        for (i, o) in operands.iter().enumerate() {
            let src = &b[i];
            match o.kind {
                Kind::Full | Kind::Tile | Kind::Col => {
                    let item = Hw::item(o.dtype);
                    let buf = t.buffer(&[total, width], o.dtype, vec![])?;
                    zero(t, &buf, total, width * item / wb, &hw)?;
                    let col = o.kind == Kind::Col;
                    let count = if col { block_rows } else { r };
                    let stride = if col { 0 } else { o.pitch * item };
                    let row_words = (width * item / wb, wb);
                    t.copy_classed(
                        &buf.whole(),
                        &src.whole(),
                        (
                            &[(nblocks, width * item), (count, stride), row_words],
                            Some(o.offset * item),
                        ),
                        (
                            &[
                                (nblocks, block_rows * width * item),
                                (count, width * item),
                                row_words,
                            ],
                            None,
                        ),
                        Some(1),
                    )?;
                    bufs.push(buf);
                }
                Kind::Row => {
                    let buf = t.buffer(&[total], o.dtype, vec![])?;
                    zero(t, &buf, total / hw.lanes, 1, &hw)?;
                    let n = stat_words(r, &hw);
                    t.copy_classed(
                        &buf.whole(),
                        &src.whole(),
                        (&[(nblocks, 0), (n, wb)], Some(o.offset * 2)),
                        (&[(nblocks, block_rows * 2), (n, wb)], None),
                        Some(1),
                    )?;
                    bufs.push(buf);
                }
                Kind::Scalar => bufs.push(src.clone()),
            }
        }
        let dst = t.buffer(&[total, width], out.dtype, vec![])?;
        let (bufs, dst2) = (&bufs, &dst);
        t.par_classed(used, Par::default(), |t, c| {
            let c2 = c.clone();
            t.range(core_trips(trips, used, &c), |t, k| {
                let row = trip_row(&c2, &k, used, height);
                let hi = row.add(height);
                let mut vals = HashMap::new();
                for (i, o) in operands.iter().enumerate() {
                    let v = match o.kind {
                        Kind::Full | Kind::Tile | Kind::Col => {
                            let view = bufs[i].at(&[sl(&row, &hi), ALL])?;
                            Arg::Val(t.load(&view, true)?)
                        }
                        Kind::Row => {
                            let view = bufs[i].at(&[sl(&row, &hi)])?;
                            Arg::Val(t.load(&view, false)?)
                        }
                        Kind::Scalar => scalar(t, &bufs[i], o, &hw)?,
                    };
                    vals.insert(i, v);
                }
                let v = expr::emit(t, e, &vals)?;
                store_over(t, dst2, &row, &Idx::Int(0), height, width, v)
            })
        })?;
        let item = Hw::item(out.dtype);
        let row_words = (width * item / wb, wb);
        t.copy_classed(
            &b[operands.len()].whole(),
            &dst.whole(),
            (
                &[
                    (nblocks, block_rows * width * item),
                    (r, width * item),
                    row_words,
                ],
                None,
            ),
            (
                &[(nblocks, width * item), (r, out.pitch * item), row_words],
                Some(out.offset * item),
            ),
            Some(1),
        )
    };
    let kernel = if direct {
        build(&key, m, ports, given, in_place)?
    } else {
        build(&key, m, ports, given, staged)?
    };
    let lowering = if direct {
        "direct-blocks"
    } else {
        "block-major-serial-band1"
    };
    Ok(Classed {
        kernel,
        info: json!({
            "width": width,
            "trip_rows": height,
            "cores": used,
            "padded_rows": total - r * nblocks,
            "padded_columns": 0,
            "wide": true,
            "core_policy": class.cores.name(),
            "lowering": lowering,
            "blocks": nblocks,
        }),
    })
}
