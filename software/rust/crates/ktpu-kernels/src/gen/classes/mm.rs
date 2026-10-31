//! Packed products on node tile and chunk loops, with a fixed epilogue
//! trip (`classes/mm.py`).

use super::{core_trips, in_ports, scalar, trip_row, Classed, ShapeClass};
use crate::expr::{self, E};
use crate::gen::matmul::Mm;
use crate::gen::vector::{port_shape, store_over, Kind, Operand};
use crate::gen::Hw;
use crate::kernel::{build, layout, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, Buf, ALL};
use crate::trace::control::Classed as Par;
use crate::trace::{fail, Arg, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::json;
use std::collections::HashMap;

/// The class kernel of `mm` under epilogue `e`, `Load(0)` the product.
pub fn kernel(
    mm: &Mm,
    e: &E,
    extras: &[Operand],
    out: &Operand,
    class: &ShapeClass,
    m: &Machine,
) -> R<Classed> {
    let hw = Hw::of(m);
    if out.kind != Kind::Full || !expr::reduces(e).is_empty() {
        return fail(
            "API.OP.007",
            "a class product's epilogue takes a full elementwise output",
        );
    }
    if mm.tn > class.max_block || mm.tn % hw.lanes != 0 {
        return fail(
            "API.OP.006",
            format!("a class product's tile of {} columns", mm.tn),
        );
    }
    if extras
        .iter()
        .any(|o| !matches!(o.kind, Kind::Full | Kind::Col | Kind::Scalar))
    {
        return fail(
            "API.OP.007",
            "a class product's extras are full, column or scalar",
        );
    }
    let (ntm, ntn) = (mm.mp / mm.tm, mm.n / mm.tn);
    let total = mm.bt * ntm * ntn * mm.tm;
    let band = hw.band;
    let trips = total / band;
    let used = class.cores.used(hw.vc, trips);
    let tiles = mm.bt * ntm * ntn;
    let clusters = class.cores.used(hw.mg, tiles);
    let (la, lb) = (
        layout(&[("gm", mm.tm / band), ("nk", mm.nk)]),
        layout(&[("gn", mm.tn / band), ("nk", mm.nk)]),
    );
    let ash = [mm.bt * mm.mp, mm.kp];
    let bsh = [if mm.shared { mm.n } else { mm.bt * mm.n }, mm.kp];
    let (a, b) = (g::Dtype::Mx7a, g::Dtype::Mx7b);
    let mut ports = vec![
        Port::input("a", &ash, a).with_layout(la.clone()),
        Port::input("b", &bsh, b).with_layout(lb.clone()),
    ];
    let mut given = vec![
        ("a".to_string(), spec(&ash, a, la, g::BufSpace::Dram)?),
        ("b".to_string(), spec(&bsh, b, lb, g::BufSpace::Dram)?),
    ];
    let rows = mm.bt * mm.mp;
    let (more, specs) = in_ports(extras, 1, rows, mm.n, &hw)?;
    ports.extend(more);
    given.extend(specs);
    let shape = port_shape(out, rows, mm.n, &hw)?;
    ports.push(Port::output("out", &shape, out.dtype));
    given.push((
        "out".to_string(),
        spec(&shape, out.dtype, vec![], g::BufSpace::Dram)?,
    ));
    let key = name(
        "mm_class",
        vec![
            mm.py(),
            Py::Str(expr::py(e).repr()),
            Py::Tuple(extras.iter().map(Operand::py).collect()),
            out.py(),
            Py::Int(hw.mg),
            Py::Int(hw.vc),
            class.py(),
        ],
    );
    let tiled = vec![(
        "tile".to_string(),
        g::LayoutValue::Ints(vec![mm.tm / band, mm.tn / band]),
    )];
    let wb = hw.word_bytes;
    let kernel = build(&key, m, ports, given, |t: &mut Trace, p: &[Buf]| {
        let h = t.buffer(&[total, mm.tn], g::Dtype::F16, tiled)?;
        t.gemm_tiles(
            &h.whole(),
            &p[0].whole(),
            &p[1].whole(),
            mm.bt,
            mm.mp,
            mm.n,
            clusters,
            mm.shared,
        )?;
        let mut staged = vec![];
        for (i, o) in extras.iter().enumerate() {
            let src = &p[2 + i];
            if o.kind == Kind::Scalar {
                staged.push(src.clone());
                continue;
            }
            let item = Hw::item(o.dtype);
            let buf = t.buffer(&[total, mm.tn], o.dtype, vec![])?;
            let stride = if o.kind == Kind::Full {
                o.pitch * item
            } else {
                0
            };
            let tile_row = mm.tn * item;
            let tile = mm.tm * tile_row;
            t.copy_classed(
                &buf.whole(),
                &src.whole(),
                (
                    &[
                        (mm.bt * ntm, mm.tm * stride),
                        (ntn, tile_row),
                        (mm.tm, stride),
                        (tile_row / wb, wb),
                    ],
                    Some(o.offset * item),
                ),
                (
                    &[
                        (mm.bt * ntm, ntn * tile),
                        (ntn, tile),
                        (mm.tm, tile_row),
                        (tile_row / wb, wb),
                    ],
                    None,
                ),
                Some(1),
            )?;
            staged.push(buf);
        }
        let dst = t.buffer(&[total, mm.tn], out.dtype, vec![])?;
        let (hh, staged, dst2) = (&h, &staged, &dst);
        t.par_classed(used, Par::default(), |t, c| {
            let c2 = c.clone();
            t.range(core_trips(trips, used, &c), |t, k| {
                let row = trip_row(&c2, &k, used, band);
                let hi = row.add(band);
                let mut vals = HashMap::new();
                let product = t.load(&hh.at(&[sl(&row, &hi), ALL])?, true)?;
                vals.insert(0, Arg::Val(product));
                for (i, o) in extras.iter().enumerate() {
                    let v = if o.kind == Kind::Scalar {
                        scalar(t, &staged[i], o, &hw)?
                    } else {
                        let view = staged[i].at(&[sl(&row, &hi), ALL])?;
                        Arg::Val(t.load(&view, true)?)
                    };
                    vals.insert(i + 1, v);
                }
                let v = expr::emit(t, e, &vals)?;
                store_over(t, dst2, &row, &Idx::Int(0), band, mm.tn, v)
            })
        })?;
        let item = Hw::item(out.dtype);
        let tile_row = mm.tn * item;
        let tile = mm.tm * tile_row;
        t.copy_classed(
            &p[2 + extras.len()].whole(),
            &dst.whole(),
            (
                &[
                    (mm.bt * ntm, ntn * tile),
                    (ntn, tile),
                    (mm.tm, tile_row),
                    (tile_row / wb, wb),
                ],
                None,
            ),
            (
                &[
                    (mm.bt * ntm, mm.tm * out.pitch * item),
                    (ntn, tile_row),
                    (mm.tm, out.pitch * item),
                    (tile_row / wb, wb),
                ],
                Some(out.offset * item),
            ),
            Some(1),
        )
    })?;
    Ok(Classed {
        kernel,
        info: json!({
            "mm": true,
            "width": mm.tn,
            "trip_rows": band,
            "cores": used,
            "clusters": clusters,
            "tm": mm.tm,
            "tn": mm.tn,
            "nk": mm.nk,
            "kblock": class.kblock,
            "padded_rows": mm.bt * (mm.mp - mm.m),
            "padded_columns": 0,
            "core_policy": class.cores.name(),
        }),
    })
}
