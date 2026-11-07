//! Form conversions on the card: a copy, a walk or
//! a fill by the mover, an element type to another.

use super::{cdiv, vector, Hw};
use crate::expr;
use crate::kernel::{build, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, sl, ALL};
use crate::trace::mover::Walk;
use crate::trace::R;
use ktpu_graph as g;
use ktpu_machine::Machine;

pub mod destination;
pub mod local;
pub mod prefix;
pub mod rows;

/// Words of `nbytes` and the f16 values a word holds: a buffer as words.
fn words(nbytes: i64, hw: &Hw) -> (i64, i64) {
    (cdiv(nbytes, hw.word_bytes), hw.word_bytes / 2)
}

/// Ports and the specs given them.
type Ports = (Vec<Port>, Vec<(String, g::Spec)>);

fn word_ports(names: &[(&str, g::Role, i64)], hw: &Hw) -> R<Ports> {
    let (mut ports, mut given) = (vec![], vec![]);
    for (n, role, w) in names {
        let shape = [*w, hw.word_bytes / 2];
        ports.push(match role {
            g::Role::In => Port::input(n, &shape, g::Dtype::F16),
            g::Role::Out => Port::output(n, &shape, g::Dtype::F16),
        });
        given.push((n.to_string(), f16(&shape)?));
    }
    Ok((ports, given))
}

/// `nbytes` (whole words, the allocation's tail covering the rest) from
/// `src` to `dst` by the mover (DSL.MOV.001).
pub fn copy_kernel(nbytes: i64, m: &Machine) -> R<Kernel> {
    let hw = Hw::of(m);
    let (w, _) = words(nbytes, &hw);
    let (ports, given) = word_ports(&[("src", g::Role::In, w), ("dst", g::Role::Out, w)], &hw)?;
    build(&name("copy", vec![Py::Int(w)]), m, ports, given, |t, b| {
        t.copy(&b[1].whole(), &b[0].whole(), None, None)
    })
}

/// Complete destination words, coalesced at physical write-line boundaries.
pub fn split_options(
    nbytes: i64,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<Vec<crate::split::SplitOption>> {
    let hw = Hw::of(m);
    let (w, lanes) = words(nbytes, &hw);
    let operand = vector::Operand {
        kind: vector::Kind::Full,
        dtype: g::Dtype::F16,
        offset: 0,
        pitch: lanes,
        rows: w,
    };
    let tensor = |name: &str| vector::split::Tensor {
        name: name.into(),
        operand,
        width: lanes,
        planes: vec![0],
    };
    let shape = vector::split::Shape {
        rows: w,
        quantum: 1,
        reads: vec![tensor("src")],
        writes: vec![tensor("dst")],
        tokens: false,
    };
    let mut options = vector::split::split_options(&shape, request, m)?;
    for option in &mut options {
        option.id = "destination-tiles".into();
        option.axis = crate::split::SplitAxis::DestinationTiles;
        option.legality.clauses =
            vec!["complete destination words; one owner per physical write line".into()];
        for share in &mut option.shards {
            share.local_schedule = "destination-copy".into();
        }
    }
    Ok(options)
}

/// Copy each destination share directly from its global source origin.
pub fn copy_split(
    nbytes: i64,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    let hw = Hw::of(m);
    let w = words(nbytes, &hw).0;
    let options = split_options(nbytes, request, m)?;
    let signature = copy_kernel(nbytes, m)?;
    vector::split::kernel_options(options, signature, request, m, |first, count| {
        let (ports, given) = word_ports(&[("src", g::Role::In, w), ("dst", g::Role::Out, w)], &hw)?;
        let key = name(
            "copy_share",
            vec![Py::Int(w), Py::Int(first), Py::Int(count)],
        );
        build(&key, m, ports, given, |t, b| {
            let slice = [sl(first, first + count), ALL];
            t.copy(&b[1].at(&slice)?, &b[0].at(&slice)?, None, None)
        })
    })
}

/// `dst` walked by `dlevels` from byte `dat` := `src` walked by `levels`
/// from byte `at` (DSL.MOV.002).
pub fn walk_kernel(
    src_bytes: i64,
    levels: &Walk,
    at: i64,
    dst_bytes: i64,
    dlevels: &Walk,
    dat: i64,
    m: &Machine,
) -> R<Kernel> {
    let hw = Hw::of(m);
    let (sw, dw) = (words(src_bytes, &hw).0, words(dst_bytes, &hw).0);
    let (ports, given) = word_ports(&[("src", g::Role::In, sw), ("dst", g::Role::Out, dw)], &hw)?;
    let key = name(
        "walk",
        vec![
            Py::Int(sw),
            Py::walk(levels),
            Py::Int(at),
            Py::Int(dw),
            Py::walk(dlevels),
            Py::Int(dat),
        ],
    );
    build(&key, m, ports, given, |t, b| {
        t.copy(
            &b[1].whole(),
            &b[0].whole(),
            Some((levels, Some(at))),
            Some((dlevels, Some(dat))),
        )
    })
}

/// `dst` walked by `dlevels` from byte `dat` := `value` (its low `ew` bits
/// replicated) by the mover (DSL.MOV.005).
pub fn fill_kernel(
    dst_bytes: i64,
    dlevels: &Walk,
    dat: i64,
    value: i64,
    ew: i64,
    m: &Machine,
) -> R<Kernel> {
    let hw = Hw::of(m);
    let dw = words(dst_bytes, &hw).0;
    let (ports, given) = word_ports(&[("dst", g::Role::Out, dw)], &hw)?;
    let key = name(
        "fill",
        vec![
            Py::Int(dw),
            Py::walk(dlevels),
            Py::Int(dat),
            Py::Int(value),
            Py::Int(ew),
        ],
    );
    build(&key, m, ports, given, |t, b| {
        t.fill_value(&b[0].whole(), value, Some(ew), Some((dlevels, Some(dat))))
    })
}

/// `n` values of `src` (f16 / f32) rewritten as `dst`.
pub fn cast_kernel(n: i64, src: g::Dtype, dst: g::Dtype, m: &Machine) -> R<Kernel> {
    vector::flat_kernel(&expr::load(0), &[src], n, dst, &[], m)
}
