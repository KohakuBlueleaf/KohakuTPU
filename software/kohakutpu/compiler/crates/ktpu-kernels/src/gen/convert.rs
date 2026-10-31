//! Form conversions on the card (`kernels/convert.py`): a copy, a walk or
//! a fill by the mover, an element type to another.

use super::{cdiv, vector, Hw};
use crate::expr;
use crate::kernel::{build, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::f16;
use crate::trace::mover::Walk;
use crate::trace::R;
use ktpu_graph as g;
use ktpu_machine::Machine;

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
