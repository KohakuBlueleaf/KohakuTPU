//! An exact on-card transpose of f16 `[nb, A, B]` to `[nb, B, A]` (A and B
//! multiples of a word's lanes) and an innermost-axis flip
//! (`kernels/transpose.py`, TG.RUN.006, .007). Elements move inside a word
//! only in a vector core's lanes; the mover moves everything else.
//!
//! With b = `mg.lanes` (a GT4 band, b x b lanes a word):
//! 1. mover GT4: b rows of a word's columns to b b x b sub-tiles;
//! 2. vector core: each sub-tile transposed in its chunk -- lane b*i + j
//!    takes lane b*j + i: log2(b) bit swaps, each a crossbar xor (5, then
//!    10 for b = 4) under a lane mask;
//! 3. mover GT4 again: the sub-tiles of each output row group back to rows;
//! 4. mover copy: those groups to row-major `[nb, B, A]`.

use super::vector::split;
use super::{cdiv, Hw};
use crate::kernel::{build, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, sl, Buf, ALL};
use crate::trace::control::At;
use crate::trace::mover::Walk;
use crate::trace::{fail, Arg, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;

/// Rows of `vc.vlmax` values a vector-core trip transforms.
pub const ROWS: i64 = 8;

const F16_ONE: u16 = 0x3c00;

/// The bit swaps of a sub-tile transpose: log2(`mg.lanes`) of them, a word
/// being `mg.lanes`^2 lanes, a power of two (TG.RUN.006).
fn swaps(hw: &Hw) -> R<i64> {
    let band = hw.band;
    if band < 2 || band & (band - 1) != 0 || band * band != hw.lanes {
        return fail(
            "TG.RUN.006",
            format!(
                "a word of {} lanes is not {band} x {band} sub-tiles of a power of two",
                hw.lanes
            ),
        );
    }
    Ok(band.trailing_zeros() as i64)
}

/// `[log2(mg.lanes), vlmax]` f16 bits: row i the lanes whose bits i and
/// i + log2(mg.lanes) differ (the table `masks()` uploads).
pub fn masks(m: &Machine) -> R<Vec<u16>> {
    let hw = Hw::of(m);
    let s = swaps(&hw)?;
    let bit = |b: bool| if b { F16_ONE } else { 0 };
    let lanes: Vec<i64> = (0..hw.vlmax).map(|i| i % hw.lanes).collect();
    Ok((0..s)
        .flat_map(|i| {
            lanes
                .iter()
                .map(move |l| bit((l >> i) & 1 != (l >> (i + s)) & 1))
        })
        .collect())
}

/// Ports `x` (f16 [nb, A, B] as words), `mask` ([`masks`]), `y` (f16
/// [nb, B, A] as words).
pub fn transpose_kernel(nb: i64, a: i64, b: i64, m: &Machine) -> R<Kernel> {
    let hw = Hw::of(m);
    let (lanes, vl) = (hw.lanes, hw.vlmax);
    let s = swaps(&hw)?;
    if a % lanes != 0 || b % lanes != 0 {
        return fail(
            "TG.RUN.006",
            format!("a transpose of {a} x {b}: both multiples of {lanes}"),
        );
    }
    let words = nb * a * b / lanes;
    let (xs, ms) = ([words, lanes], [s, vl]);
    let ports = vec![
        Port::input("x", &xs, g::Dtype::F16),
        Port::input("mask", &ms, g::Dtype::F16),
        Port::output("y", &xs, g::Dtype::F16),
    ];
    let given = vec![
        ("x".to_string(), f16(&xs)?),
        ("mask".to_string(), f16(&ms)?),
        ("y".to_string(), f16(&xs)?),
    ];
    let key = name("transpose", vec![Py::Int(nb), Py::Int(a), Py::Int(b)]);
    build(&key, m, ports, given, |t, bufs| {
        emit(t, &bufs[0], &bufs[1], 0, &bufs[2], nb, a, b, &hw)
    })
}

/// The transpose's statements in the kernel being traced: `x` (f16 [nb, A,
/// B] as words) into `y` ([nb, B, A]), the lane masks at rows `m0`.. of
/// `mask` ([`masks`]); its trips split over the cores ([`split`]).
#[allow(clippy::too_many_arguments)]
pub fn emit(
    t: &mut Trace,
    x: &Buf,
    mask: &Buf,
    m0: i64,
    y: &Buf,
    nb: i64,
    a: i64,
    b: i64,
    hw: &Hw,
) -> R<()> {
    let (lanes, vl, wb, band) = (hw.lanes, hw.vlmax, hw.word_bytes, hw.band);
    let s = swaps(hw)?;
    let words = nb * a * b / lanes;
    let lines = words / (vl / lanes);
    let (p, rg, cw) = (2 * b, a / band, b / lanes);
    let sub = b / band;
    let al = a / lanes;
    let trips = cdiv(lines, ROWS);
    // A GT4 group: a band's consecutive words.
    let sw = band * wb;
    let st = t.buffer(&[trips * ROWS, vl], g::Dtype::F16, vec![])?;
    let tt = t.buffer(&[trips * ROWS, vl], g::Dtype::F16, vec![])?;
    let z = t.buffer(&[words, lanes], g::Dtype::F16, vec![])?;
    let walk: &Walk = &[(nb, a * p), (rg, band * p), (cw, wb), (band, p)];
    let dwalk: &Walk = &[(nb, rg * cw * sw), (rg, cw * sw), (cw, sw)];
    t.gt4(&st.whole(), &x.whole(), walk, dwalk)?;
    let sp = split(trips, hw.vc);
    t.par(sp.cores, g::UnitKind::Vc, At::Var, |t, c| {
        let mut ms = vec![];
        for i in m0..m0 + s {
            let l = t.load(&mask.at(&[sl(i, i + 1), ALL])?, false)?;
            ms.push(t.unpack(l, None)?);
        }
        let first = sp.start(&c);
        t.range(sp.trips(&c), |t, k| {
            let r = first.add(k).mul(ROWS);
            let lv = t.load(&st.at(&[sl(&r, r.add(ROWS)), ALL])?, false)?;
            let mut v = t.unpack(lv, None)?;
            for (i, mi) in ms.iter().enumerate() {
                let x = t.xlane(Arg::Val(v), (1i64 << i) | (1i64 << (i as i64 + s)))?;
                v = t.math(g::ValueOp::Sel, &[Arg::Val(*mi), Arg::Val(x), Arg::Val(v)])?;
            }
            t.store(&tt.at(&[sl(&r, r.add(ROWS)), ALL])?, Arg::Val(v))
        })
    })?;
    let walk: &Walk = &[
        (nb, rg * sub * wb),
        (sub, wb),
        (al, band * sub * wb),
        (band, sub * wb),
    ];
    let dwalk: &Walk = &[(nb, sub * al * sw), (sub, al * sw), (al, sw)];
    t.gt4(&z.whole(), &tt.whole(), walk, dwalk)?;
    let walk: &Walk = &[(nb, sub * al * sw), (sub, al * sw), (band, wb), (al, sw)];
    let dwalk: &Walk = &[(words, wb)];
    t.copy(
        &y.whole(),
        &z.whole(),
        Some((walk, None)),
        Some((dwalk, None)),
    )
}

/// `y` (`n` f16, row-major, word-exact) := `x` walked by `levels` from byte `at`
/// (the last level a row's words stepping back) with each word's lanes
/// reversed (TG.RUN.007).
pub fn reverse_kernel(src_bytes: i64, levels: &Walk, at: i64, n: i64, m: &Machine) -> R<Kernel> {
    let hw = Hw::of(m);
    let (lanes, vl, wb) = (hw.lanes, hw.vlmax, hw.word_bytes);
    let (sw, words) = (cdiv(src_bytes, wb), n / lanes);
    let trips = cdiv(words, vl / lanes * ROWS);
    let exact = words % (vl / lanes * ROWS) != 0 && !crate::mutants::on("memory/reverse-overrun");
    let (xs, ys) = (
        [sw, lanes],
        if exact {
            [words, lanes]
        } else {
            [trips * ROWS, vl]
        },
    );
    let ports = vec![
        Port::input("x", &xs, g::Dtype::F16),
        Port::output("y", &ys, g::Dtype::F16),
    ];
    let given = vec![("x".to_string(), f16(&xs)?), ("y".to_string(), f16(&ys)?)];
    let key = name(
        "reverse",
        vec![Py::Int(sw), Py::walk(levels), Py::Int(at), Py::Int(n)],
    );
    build(&key, m, ports, given, |t: &mut Trace, b| {
        let (x, y) = (&b[0], &b[1]);
        let s = t.buffer(&[trips * ROWS, vl], g::Dtype::F16, vec![])?;
        let output = if exact {
            t.buffer(&[trips * ROWS, vl], g::Dtype::F16, vec![])?
        } else {
            y.clone()
        };
        let dwalk: &Walk = &[(words, wb)];
        t.copy(
            &s.whole(),
            &x.whole(),
            Some((levels, Some(at))),
            Some((dwalk, None)),
        )?;
        let sp = split(trips, hw.vc);
        t.par(sp.cores, g::UnitKind::Vc, At::Var, |t, c| {
            let first = sp.start(&c);
            t.range(sp.trips(&c), |t, k| {
                let r = first.add(k).mul(ROWS);
                let lv = t.load(&s.at(&[sl(&r, r.add(ROWS)), ALL])?, false)?;
                let v = t.unpack(lv, None)?;
                let o = t.xlane(Arg::Val(v), lanes - 1)?;
                t.store(&output.at(&[sl(&r, r.add(ROWS)), ALL])?, Arg::Val(o))
            })
        })?;
        if exact {
            t.copy(
                &y.whole(),
                &output.whole(),
                Some((dwalk, None)),
                Some((dwalk, None)),
            )?;
        }
        Ok(())
    })
}
