//! Element-granular relayouts (`kernels/lanes.py`, TG.RUN.008): f16
//! elements walked into any other order, exact.
//!
//! A destination word's lanes group into taps: the lanes reading one
//! source word at one rotation r (lane l reads lane `(l + r) mod lanes`).
//! Words with the same taps form a class: the mover gathers each tap's
//! source words (fp32 index lists), a vector core makes `where(m_t,
//! xrot(S_t, r_t), .., base)` (`base` 0 or the destination's word, a pass
//! of at most `(vc.descriptors - 2) / 2` taps), the mover scatters the
//! words back. Words no element reaches are 0 (one fill), or kept.

use super::vector::split;
use super::{cdiv, Hw};
use crate::kernel::{build, Kernel, Port};
use crate::mutants;
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, sl, spec, Buf, Key, View, ALL};
use crate::trace::control::At;
use crate::trace::{fail, Arg, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use std::collections::BTreeMap;

/// Rows of `vc.vlmax` values a vector trip takes.
pub const ROWS: i64 = 2;
const F16_ONE: u16 = 0x3c00;

/// Destination words that read the same taps.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Class {
    /// `(rotation, lane mask bits)`, ordered by their first lane.
    pub taps: Vec<(i64, i64)>,
    pub words: i64,
    /// Lanes outside every tap keep the destination's value.
    pub keep: bool,
}

/// A relayout: its classes, their fp32 index lists (per class, per tap
/// then the destination, per column block, `entries(class)` each), the
/// taps' lane masks (`mask_rows` x `vc.vlmax` f16 bits).
#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    pub classes: Vec<Class>,
    pub index: Vec<f32>,
    pub masks: Vec<u16>,
    pub mask_rows: i64,
    pub src_words: i64,
    pub dst_words: i64,
    pub fill: bool,
}

/// Words a scratch row holds.
fn half(hw: &Hw) -> i64 {
    hw.vlmax / hw.lanes
}

/// Rows of a class's scratch: whole trips of [`ROWS`].
pub fn rows(c: &Class, hw: &Hw) -> i64 {
    cdiv(c.words, half(hw) * ROWS) * ROWS
}

/// Indices one of a class's lists holds: its rows, in whole words (the
/// mover reads an index list from a word boundary).
pub fn entries(c: &Class, hw: &Hw) -> i64 {
    let per = hw.word_bytes / 4;
    cdiv(rows(c, hw), per) * per
}

/// The elements `at + sum(i_k * stride_k)` of `walk` (`(count, element
/// stride)`, outermost first), in walk order.
pub fn elements(walk: &[(i64, i64)], at: i64) -> Vec<i64> {
    let mut idx = vec![at];
    for &(n, s) in walk {
        idx = idx
            .iter()
            .flat_map(|&x| (0..n).map(move |i| x + i * s))
            .collect();
    }
    idx
}

/// The relayout of `m` destination elements from element `at` of `src_n`
/// source elements walked by `walk` into the elements `dwalk` reaches from
/// `dat` (the two walks' counts equal).
#[allow(clippy::too_many_arguments)]
pub fn plan(
    src_n: i64,
    walk: &[(i64, i64)],
    at: i64,
    dwalk: &[(i64, i64)],
    dat: i64,
    m: i64,
    keep: bool,
    mach: &Machine,
) -> R<Plan> {
    let hw = Hw::of(mach);
    let lanes = hw.lanes;
    let keep = keep && !mutants::on("l2.lanes/keep-ignored");
    let (src, dst) = (elements(walk, at), elements(dwalk, dat));
    let out_of = |v: &[i64], n: i64| v.iter().any(|&x| x < 0 || x >= n);
    if src.len() != dst.len() || out_of(&src, src_n) {
        return fail(
            "L2.REL.005",
            format!(
                "walks of {} / {} elements over {src_n}",
                src.len(),
                dst.len()
            ),
        );
    }
    if out_of(&dst, m) {
        return fail(
            "L2.REL.005",
            format!("a destination element outside 0..{m}"),
        );
    }
    let dw = cdiv(m, lanes);
    let mut grid = vec![-1i64; (dw * lanes) as usize];
    for (d, s) in dst.iter().zip(&src) {
        grid[*d as usize] = *s;
    }
    let lane_of = |w: i64, l: i64| grid[(w * lanes + l) as usize];
    type Sig = Vec<(i64, i64)>;
    let mut order: Vec<(Sig, bool)> = vec![];
    let mut groups: BTreeMap<usize, Vec<(i64, Vec<i64>)>> = BTreeMap::new();
    let mut all_covered = true;
    for w in 0..dw {
        let covered: Vec<i64> = (0..lanes).filter(|l| lane_of(w, *l) >= 0).collect();
        if covered.len() as i64 != lanes {
            all_covered = false;
        }
        if covered.is_empty() {
            continue;
        }
        let mut taps: Vec<((i64, i64), i64)> = vec![];
        for &l in &covered {
            let s = lane_of(w, l);
            let key = (
                s.div_euclid(lanes),
                (s.rem_euclid(lanes) - l).rem_euclid(lanes),
            );
            match taps.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 |= 1 << l,
                None => taps.push((key, 1 << l)),
            }
        }
        taps.sort_by_key(|(_, mask)| mask & -mask);
        let sig: Sig = taps.iter().map(|((_, r), mask)| (*r, *mask)).collect();
        let whole = covered.len() as i64 == lanes;
        let gk = (sig, keep && !whole);
        let at = match order.iter().position(|k| *k == gk) {
            Some(i) => i,
            None => {
                order.push(gk);
                order.len() - 1
            }
        };
        let qs = taps.iter().map(|((q, _), _)| *q).collect();
        groups.entry(at).or_default().push((w, qs));
    }
    let (mut classes, mut index, mut masks) = (vec![], vec![], vec![]);
    let hf = half(&hw);
    for (i, (sig, kept)) in order.iter().enumerate() {
        let members = &groups[&i];
        let c = Class {
            taps: sig.clone(),
            words: members.len() as i64,
            keep: *kept,
        };
        let ne = entries(&c, &hw);
        let nt = sig.len() as i64;
        let mut ix = vec![0f32; ((nt + 1) * hf * ne) as usize];
        let at = |t: i64, j: i64, r: i64| ((t * hf + j) * ne + r) as usize;
        for (k, (w, qs)) in members.iter().enumerate() {
            let (r, j) = ((k as i64) / hf, (k as i64) % hf);
            for (t, q) in qs.iter().enumerate() {
                ix[at(t as i64, j, r)] = *q as f32;
            }
            ix[at(nt, j, r)] = *w as f32;
        }
        index.extend(ix);
        for (_, mask) in sig {
            for v in 0..hw.vlmax {
                let l = v % lanes;
                masks.push(if (mask >> l) & 1 == 1 { F16_ONE } else { 0 });
            }
        }
        classes.push(c);
    }
    let mask_rows = (masks.len() as i64 / hw.vlmax).max(1);
    if index.is_empty() {
        index = vec![0.0; 8];
    }
    if masks.is_empty() {
        masks = vec![0; hw.vlmax as usize];
    }
    if mutants::on("l2.lanes/rot-negated") {
        for c in &mut classes {
            for t in &mut c.taps {
                t.0 = (-t.0).rem_euclid(lanes);
            }
        }
    }
    if mutants::on("l2.lanes/masks-inverted") {
        masks
            .iter_mut()
            .for_each(|b| *b = if *b == 0 { F16_ONE } else { 0 });
    }
    Ok(Plan {
        classes,
        index,
        masks,
        mask_rows,
        src_words: cdiv(src_n, lanes),
        dst_words: dw,
        fill: !keep && !all_covered,
    })
}

/// Rows of the scratch from `first` on, indexed from 0.
struct Rows<'a> {
    buf: &'a Buf,
    first: i64,
}

impl Rows<'_> {
    fn at(&self, lo: &Idx, hi: &Idx, cols: Key) -> R<View> {
        let lo = Idx::Int(self.first).add(lo);
        let hi = Idx::Int(self.first).add(hi);
        self.buf.at(&[Key::Span(lo, hi), cols])
    }
}

/// Ports `src` (f16 as words), `dst` (f16 as words), `index` (the plan's
/// fp32 lists), `mask` (its lane masks).
pub fn relayout_kernel(pl: &Plan, m: &Machine) -> R<Kernel> {
    let hw = Hw::of(m);
    let lanes = hw.lanes;
    let (ss, ds) = ([pl.src_words, lanes], [pl.dst_words, lanes]);
    let is = [pl.index.len() as i64];
    let ms = [pl.mask_rows, hw.vlmax];
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
    let shape = Py::Tuple(
        pl.classes
            .iter()
            .map(|c| {
                Py::Tuple(vec![
                    Py::Tuple(c.taps.iter().map(|(r, b)| Py::ints(&[*r, *b])).collect()),
                    Py::Int(c.words),
                    Py::Bool(c.keep),
                ])
            })
            .collect(),
    );
    let key = name(
        "lanes",
        vec![
            shape,
            Py::Int(pl.src_words),
            Py::Int(pl.dst_words),
            Py::Bool(pl.fill),
        ],
    );
    let Some(most) = pl
        .classes
        .iter()
        .map(|c| (c.taps.len() as i64 + 1) * rows(c, &hw))
        .max()
    else {
        return fail("L2.REL.005", "a relayout of no element");
    };
    build(&key, m, ports, given, |t, b| {
        let (src, dst, index, mask) = (&b[0], &b[1], &b[2], &b[3]);
        if pl.fill {
            t.fill_value(&dst.whole(), 0, None, None)?;
        }
        let scratch = t.buffer(&[most, hw.vlmax], g::Dtype::F16, vec![])?;
        let (mut at, mut m0) = (0, 0);
        for c in &pl.classes {
            class(t, c, src, dst, index, at, mask, m0, &scratch, &hw)?;
            at += (c.taps.len() as i64 + 1) * half(&hw) * entries(c, &hw);
            m0 += c.taps.len() as i64;
        }
        Ok(())
    })
}

/// One class: gather its taps' words, assemble, scatter.
#[allow(clippy::too_many_arguments)]
fn class(
    t: &mut Trace,
    c: &Class,
    src: &Buf,
    dst: &Buf,
    index: &Buf,
    at: i64,
    mask: &Buf,
    m0: i64,
    scratch: &Buf,
    hw: &Hw,
) -> R<()> {
    let (nr, ne, hf, lanes) = (rows(c, hw), entries(c, hw), half(hw), hw.lanes);
    let nt = c.taps.len() as i64;
    let taps: Vec<Rows> = (0..nt)
        .map(|k| Rows {
            buf: scratch,
            first: k * nr,
        })
        .collect();
    let out = Rows {
        buf: scratch,
        first: nt * nr,
    };
    let lists = |k: i64, j: i64| -> R<View> {
        let lo = at + (k * hf + j) * ne;
        index.at(&[sl(lo, lo + ne)])
    };
    let blocks: Vec<(i64, i64)> = (0..hf)
        .filter(|j| *j < c.words)
        .map(|j| (j, cdiv(c.words - j, hf)))
        .collect();
    let cols = |j: i64| sl(lanes * j, lanes * (j + 1));
    let zero = Idx::Int(0);
    for &(j, n) in &blocks {
        for (k, s) in taps.iter().enumerate() {
            let v = s.at(&zero, &Idx::Int(n), cols(j))?;
            t.gather(&v, &src.whole(), &lists(k as i64, j)?, true, None)?;
        }
        if c.keep {
            let v = out.at(&zero, &Idx::Int(n), cols(j))?;
            t.gather(&v, &dst.whole(), &lists(nt, j)?, true, None)?;
        }
    }
    let group = (hw.descriptors - 2) / 2;
    let mut g0 = 0;
    while g0 < nt {
        assemble(t, c, &taps, &out, mask, m0, g0, group, hw)?;
        g0 += group;
    }
    for &(j, n) in &blocks {
        let v = out.at(&zero, &Idx::Int(n), cols(j))?;
        t.scatter(&dst.whole(), &v, &lists(nt, j)?, true, None)?;
    }
    Ok(())
}

/// Taps `g0..g0 + group` of every word of the class, over `out`: from what
/// it holds (a kept destination, or an earlier group's taps), else from 0.
#[allow(clippy::too_many_arguments)]
fn assemble(
    t: &mut Trace,
    c: &Class,
    taps: &[Rows],
    out: &Rows,
    mask: &Buf,
    m0: i64,
    g0: i64,
    group: i64,
    hw: &Hw,
) -> R<()> {
    let members: Vec<i64> = (g0..(g0 + group).min(c.taps.len() as i64)).collect();
    let trips = rows(c, hw) / ROWS;
    let sp = split(trips, hw.vc);
    let full = (1i64 << hw.lanes) - 1;
    t.par(sp.cores, g::UnitKind::Vc, At::Var, |t, core| {
        let mut ms = BTreeMap::new();
        for &k in &members {
            let l = t.load(&mask.at(&[sl(m0 + k, m0 + k + 1), ALL])?, false)?;
            ms.insert(k, t.unpack(l, None)?);
        }
        let first = sp.start(&core);
        t.range(sp.trips(&core), |t, k| {
            let r = first.add(k).mul(ROWS);
            let hi = r.add(ROWS);
            let mut base = if c.keep || g0 > 0 {
                let l = t.load(&out.at(&r, &hi, ALL)?, false)?;
                Arg::Val(t.unpack(l, None)?)
            } else {
                Arg::Num(0.0)
            };
            for &tap in &members {
                let (rot, bits) = c.taps[tap as usize];
                let l = t.load(&taps[tap as usize].at(&r, &hi, ALL)?, false)?;
                let mut v = Arg::Val(t.unpack(l, None)?);
                if rot != 0 {
                    v = Arg::Val(t.xrot(v, rot)?);
                }
                base = if bits == full {
                    v
                } else {
                    Arg::Val(t.math(g::ValueOp::Sel, &[Arg::Val(ms[&tap]), v, base])?)
                };
            }
            t.store(&out.at(&r, &hi, ALL)?, base)
        })
    })
}
