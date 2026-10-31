//! One lanes class: padded duplicate members and fixed tap instructions
//! (`classes/lanes.py`).

use super::{core_trips, trip_row, Classed, Cores};
use crate::gen::lanes::{entries, rows, Class, Plan, Rows, ROWS};
use crate::gen::{cdiv, Hw};
use crate::kernel::{build, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, sl, spec, ALL};
use crate::trace::control::Classed as Par;
use crate::trace::{Arg, Idx, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::json;

/// A padded class's tables.
#[derive(Clone, Debug, PartialEq)]
pub struct Tables {
    pub class: Class,
    pub index: Vec<f32>,
    pub masks: Vec<u16>,
}

/// Rows of a class padded to whole words of a row's words.
fn padded_rows(c: &Class, hw: &Hw) -> i64 {
    let per = hw.vlmax / hw.lanes;
    cdiv(rows(c, hw), per) * per
}

/// Each class of `pl`'s tables: a padded member repeats the last real
/// member, its destination too.
pub fn tables(pl: &Plan, m: &Machine) -> Vec<Tables> {
    let hw = Hw::of(m);
    let hf = hw.vlmax / hw.lanes;
    let (mut offset, mut first) = (0usize, 0usize);
    let mut out = vec![];
    for c in &pl.classes {
        let taps = c.taps.len() as i64;
        let (ne, nr) = (entries(c, &hw), padded_rows(c, &hw));
        let old = &pl.index[offset..offset + ((taps + 1) * hf * ne) as usize];
        let mut index = vec![0f32; ((taps + 1) * hf * nr) as usize];
        for k in 0..nr * hf {
            let k_old = k.min(c.words - 1);
            let (r, j) = (k_old / hf, k_old % hf);
            let (dest_r, dest_j) = (k / hf, k % hf);
            for t in 0..=taps {
                index[((t * hf + dest_j) * nr + dest_r) as usize] =
                    old[((t * hf + j) * ne + r) as usize];
            }
        }
        let end = first + (taps * hw.vlmax) as usize;
        out.push(Tables {
            class: c.clone(),
            index,
            masks: pl.masks[first..end].to_vec(),
        });
        offset += old.len();
        first = end;
    }
    out
}

/// The kernel of class `c`: ports `src`, `dst`, `index` ([`tables`]'), `mask`.
pub fn kernel(c: &Class, src_words: i64, dst_words: i64, cores: Cores, m: &Machine) -> R<Classed> {
    let hw = Hw::of(m);
    let hf = hw.vlmax / hw.lanes;
    let nr = padded_rows(c, &hw);
    let trips = nr / ROWS;
    let used = cores.used(hw.vc, trips);
    let padded = cdiv(trips, used) * used * ROWS;
    let taps = c.taps.len() as i64;
    let (ss, ds) = ([src_words, hw.lanes], [dst_words, hw.lanes]);
    let is = [(taps + 1) * hf * nr];
    let ms = [taps, hw.vlmax];
    let ports = vec![
        Port::input("src", &ss, g::Dtype::F16),
        Port::output("dst", &ds, g::Dtype::F16),
        Port::input("index", &is, g::Dtype::F32),
        Port::input("mask", &ms, g::Dtype::F16),
    ];
    let given = vec![
        ("src".to_string(), f16(&ss)?),
        ("dst".to_string(), f16(&ds)?),
        (
            "index".to_string(),
            spec(&is, g::Dtype::F32, vec![], g::BufSpace::Dram)?,
        ),
        ("mask".to_string(), f16(&ms)?),
    ];
    let key = name(
        "lanes_class",
        vec![
            Py::Tuple(c.taps.iter().map(|(r, b)| Py::ints(&[*r, *b])).collect()),
            Py::Int(c.words),
            Py::Bool(c.keep),
            Py::Int(src_words),
            Py::Int(dst_words),
            Py::Int(hw.vc),
            Py::str(cores.name()),
        ],
    );
    let group = (hw.descriptors - 2) / 2;
    let kernel = build(&key, m, ports, given, |t, b| {
        let (src, dst, index, mask) = (&b[0], &b[1], &b[2], &b[3]);
        let scratch = t.buffer(&[(taps + 1) * padded, hw.vlmax], g::Dtype::F16, vec![])?;
        let at = |first: i64| Rows {
            buf: &scratch,
            first,
        };
        let inputs: Vec<Rows> = (0..taps).map(|k| at(k * padded)).collect();
        let out = at(taps * padded);
        let cols = |j: i64| sl(j * hw.lanes, (j + 1) * hw.lanes);
        let list = |tap: i64, j: i64| {
            let start = (tap * hf + j) * nr;
            index.at(&[sl(start, start + nr)])
        };
        let (zero, whole) = (Idx::Int(0), Idx::Int(nr));
        for j in 0..hf {
            for (k, s) in inputs.iter().enumerate() {
                let v = s.at(&zero, &whole, cols(j))?;
                t.gather(&v, &src.whole(), &list(k as i64, j)?, true, None)?;
            }
            if c.keep {
                let v = out.at(&zero, &whole, cols(j))?;
                t.gather(&v, &dst.whole(), &list(taps, j)?, true, None)?;
            }
        }
        for first in (0..taps).step_by(group as usize) {
            let members: Vec<i64> = (first..(first + group).min(taps)).collect();
            let (members, inputs, out) = (&members, &inputs, &out);
            t.par_classed(used, Par::default(), |t, core| {
                let mut ms = vec![];
                for &k in members {
                    let l = t.load(&mask.at(&[sl(k, k + 1), ALL])?, false)?;
                    ms.push(t.unpack(l, None)?);
                }
                let (ms, c2) = (&ms, core.clone());
                t.range(core_trips(trips, used, &core), |t, k| {
                    let row = trip_row(&c2, &k, used, ROWS);
                    let hi = row.add(ROWS);
                    let mut base = if c.keep || first > 0 {
                        let l = t.load(&out.at(&row, &hi, ALL)?, false)?;
                        Arg::Val(t.unpack(l, None)?)
                    } else {
                        Arg::Num(0.0)
                    };
                    for (n, &tap) in members.iter().enumerate() {
                        let rot = c.taps[tap as usize].0;
                        let one = if rot == 1 { 2 } else { 1 };
                        let two = (rot - one).rem_euclid(hw.lanes);
                        let l = t.load(&inputs[tap as usize].at(&row, &hi, ALL)?, false)?;
                        let v = t.unpack(l, None)?;
                        let v = t.xrot(Arg::Val(v), one)?;
                        let v = t.xrot(Arg::Val(v), two)?;
                        let sel = [Arg::Val(ms[n]), Arg::Val(v), base];
                        base = Arg::Val(t.math(g::ValueOp::Sel, &sel)?);
                    }
                    t.store(&out.at(&row, &hi, ALL)?, base)
                })
            })?;
        }
        for j in 0..hf {
            let v = out.at(&zero, &whole, cols(j))?;
            t.scatter(&dst.whole(), &v, &list(taps, j)?, true, None)?;
        }
        Ok(())
    })?;
    Ok(Classed {
        kernel,
        info: json!({
            "taps": taps,
            "keep": c.keep,
            "rows": nr,
            "cores": used,
            "padded_words": nr * hf - c.words,
        }),
    })
}
