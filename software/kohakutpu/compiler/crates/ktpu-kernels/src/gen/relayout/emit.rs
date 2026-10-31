//! A plan's statements (`relayout_kernel`, `emit_steps` and each part's
//! emitter).

use super::forms::margin_name;
use super::repr::{hw_repr, shape_repr};
use super::*;
use crate::gen::cdiv;
use crate::gen::transpose;
use crate::gen::vector::split;
use crate::kernel::{build, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{at, f16, sl, spec, Buf, Key, View, ALL};
use crate::trace::control::At;
use crate::trace::{Arg, Idx, Trace, R, V};
use g::ValueOp as Op;
use ktpu_graph as g;
use std::collections::{BTreeMap, HashMap};

/// The machine facts the emitters read: the plan's and the transpose's.
struct Ctx {
    hw: Hw,
    gen: crate::gen::Hw,
}

type Bufs = HashMap<String, Buf>;

/// `rows` of `width` f16 rounded up to whole `hw.grain` bytes: the kernel's
/// scratch buffers are laid out one after another, so each starts on the
/// grain.
pub fn scratch_rows(rows: i64, width: i64, hw: &Hw) -> i64 {
    let unit = hw.grain / gcd(hw.grain, 2 * width);
    cdiv(rows, unit) * unit
}

fn scratch(t: &mut Trace, rows: i64, width: i64, hw: &Hw) -> R<Buf> {
    t.buffer(
        &[scratch_rows(rows, width, hw), width],
        g::Dtype::F16,
        vec![],
    )
}

fn table_spec(len: i64) -> R<g::Spec> {
    spec(&[len], g::Dtype::F32, vec![], g::BufSpace::Dram)
}

fn mask_shape(pl: &Plan) -> [i64; 2] {
    [pl.masks.len() as i64, pl.masks[0].len() as i64]
}

/// Ports `src` (f16 as words), `dst` (f16 as words from a line, the
/// destination's), `index` (the plan's index lists, fp32), `mask` (its mask
/// rows).
pub fn relayout_kernel(pl: &Plan, m: &Machine) -> R<Kernel> {
    let cx = Ctx {
        hw: Hw::of(m),
        gen: crate::gen::Hw::of(m),
    };
    let hw = &cx.hw;
    let (ss, ds) = ([pl.src_words, hw.lanes], [pl.dst_words, hw.lanes]);
    let (is, ms) = ([pl.index.len() as i64], mask_shape(pl));
    let ports = vec![
        Port::input("src", &ss, g::Dtype::F16),
        Port::output("dst", &ds, g::Dtype::F16),
        Port::input("index", &is, g::Dtype::F32),
        Port::input("mask", &ms, g::Dtype::F16),
    ];
    let given = vec![
        ("src".to_string(), f16(&ss)?),
        ("dst".to_string(), f16(&ds)?),
        ("index".to_string(), table_spec(is[0])?),
        ("mask".to_string(), f16(&ms)?),
    ];
    // The relayout kernels' family name (`lanes_`), whichever form.
    let key = name(
        "lanes",
        vec![Py::str("relayout"), shape_repr(pl), hw_repr(hw)],
    );
    build(&key, m, ports, given, |t, b| {
        let mut bufs: Bufs = HashMap::new();
        bufs.insert("src".into(), b[0].clone());
        bufs.insert("dst".into(), b[1].clone());
        for (name, rows, width) in &pl.scratch {
            bufs.insert(name.clone(), scratch(t, *rows, *width, hw)?);
        }
        for part in &pl.parts {
            emit(t, part, &bufs, &b[2], &b[3], &cx)?;
        }
        Ok(())
    })
}

/// Statements of `plans` inside the kernel being traced, each into a
/// scratch of lines: the first reading `src` (a word table), each next the
/// one before (its lines copied into a word table); `tables` maps
/// `{prefix}index{i}` / `{prefix}mask{i}` to their ports. Every buffer on
/// the grain (L2.REL.013). Returns the last plan's lines buffer
/// (`destination` when given).
pub fn emit_steps(
    t: &mut Trace,
    plans: &[Plan],
    src: &Buf,
    tables: &HashMap<String, Buf>,
    prefix: &str,
    m: &Machine,
    destination: Option<&Buf>,
) -> R<Buf> {
    let cx = Ctx {
        hw: Hw::of(m),
        gen: crate::gen::Hw::of(m),
    };
    let hw = &cx.hw;
    let mut cur = src.clone();
    for (i, p) in plans.iter().enumerate() {
        if i > 0 {
            let w = scratch(t, p.src_words, hw.lanes, hw)?;
            let walk = [(p.src_words, hw.word)];
            t.copy(
                &w.whole(),
                &cur.whole(),
                Some((&walk, None)),
                Some((&walk, None)),
            )?;
            cur = w;
        }
        let dst = match destination {
            Some(d) if i == plans.len() - 1 => d.clone(),
            _ => scratch(t, cdiv(p.dst_words, hw.line), hw.line_values(), hw)?,
        };
        let mut bufs: Bufs = HashMap::new();
        bufs.insert("src".into(), cur.clone());
        bufs.insert("dst".into(), dst.clone());
        for (name, n, width) in &p.scratch {
            bufs.insert(name.clone(), scratch(t, *n, *width, hw)?);
        }
        let index = &tables[&format!("{prefix}index{i}")];
        let mask = &tables[&format!("{prefix}mask{i}")];
        for part in &p.parts {
            emit(t, part, &bufs, index, mask, &cx)?;
        }
        cur = dst;
    }
    Ok(cur)
}

/// Ports and the spec each is given.
pub type Ports = (Vec<Port>, Vec<(String, g::Spec)>);

/// `(ports, given)` of `plans`' tables for [`emit_steps`].
pub fn step_ports(plans: &[Plan], prefix: &str) -> R<Ports> {
    let (mut ports, mut given) = (vec![], vec![]);
    for (i, p) in plans.iter().enumerate() {
        let (ix, mk) = (format!("{prefix}index{i}"), format!("{prefix}mask{i}"));
        let (is, ms) = ([p.index.len() as i64], mask_shape(p));
        ports.push(Port::input(&ix, &is, g::Dtype::F32));
        ports.push(Port::input(&mk, &ms, g::Dtype::F16));
        given.push((ix, table_spec(is[0])?));
        given.push((mk, f16(&ms)?));
    }
    Ok((ports, given))
}

fn emit(t: &mut Trace, part: &Part, bufs: &Bufs, index: &Buf, mask: &Buf, cx: &Ctx) -> R<()> {
    let hw = &cx.hw;
    match part {
        Part::Fill(f) => t.fill_value(
            &bufs[&f.dst].whole(),
            0,
            None,
            Some((&[(f.words, hw.word)], Some(f.w0 * hw.word))),
        ),
        Part::Words(w) => t.copy(
            &bufs[&w.dst].whole(),
            &bufs[&w.src].whole(),
            Some((&w.levels, Some(w.at))),
            Some((&w.dlevels, Some(w.dat))),
        ),
        Part::Splat(sp) => emit_splat(t, sp, bufs, mask, hw),
        Part::Shuffle(sh) => emit_shuffle(t, sh, bufs, mask, hw),
        Part::Pull(p) => emit_pull(t, p, bufs, index, mask, hw),
        Part::Exchange(x) => transpose::emit(
            t,
            &bufs[&x.src],
            mask,
            x.masks,
            &bufs[&x.dst],
            x.nb,
            x.a,
            x.b,
            None,
            &cx.gen,
        ),
        Part::Runs(r) => emit_runs(t, r, bufs, index, mask, hw),
    }
}

fn rows(lo: &Idx, n: i64) -> Key {
    sl(lo, lo.add(n))
}

fn load(t: &mut Trace, view: &View) -> R<V> {
    let l = t.load(view, false)?;
    t.unpack(l, None)
}

fn sel(t: &mut Trace, m: V, a: Arg, b: Arg) -> R<V> {
    t.math(Op::Sel, &[Arg::Val(m), a, b])
}

/// An index list's view: whole words of entries (`Tables::put` pads).
fn list(index: &Buf, at: i64, n: i64) -> R<View> {
    index.at(&[sl(at, at + cdiv(n, 8) * 8)])
}

/// A trip: one word of values (a statistic of `lanes` rows) broadcast over
/// that many rows of copies.
fn emit_splat(t: &mut Trace, sp: &Splat, bufs: &Bufs, mask: &Buf, hw: &Hw) -> R<()> {
    let (src, out, lanes) = (&bufs[&sp.src], &bufs[&sp.dst], hw.lanes);
    let s = split(sp.count, hw.cores);
    t.par(s.cores, g::UnitKind::Vc, At::Var, |t, c| {
        // A statistic broadcasts over GT4 bands; they store row-major.
        let l = t.load(&mask.at(&[sl(sp.ones, sp.ones + lanes), ALL])?, true)?;
        let ones = t.unpack(l, None)?;
        let first = s.start(&c);
        t.range(s.trips(&c), |t, k| {
            let w = first.add(k);
            let stat = load(t, &src.at(&[at(w.add(sp.w0)), ALL])?)?;
            let v = sel(t, ones, Arg::Val(stat), Arg::Val(ones))?;
            t.store(&out.at(&[rows(&w.mul(lanes), lanes), ALL])?, Arg::Val(v))
        })
    })
}

fn emit_pull(t: &mut Trace, p: &Pull, bufs: &Bufs, index: &Buf, mask: &Buf, hw: &Hw) -> R<()> {
    let (lanes, word) = (hw.lanes, hw.word);
    let (src, dst, out) = (&bufs[&p.src], &bufs[&p.dst], &bufs["po"]);
    let staged = p.rows * hw.row_words();
    let whole = [(p.words, word)];
    for (k, how) in p.slots.iter().enumerate() {
        let ps = &bufs[&format!("ps{k}")];
        match how {
            Slot::Walk(levels, from) => t.copy(
                &ps.whole(),
                &src.whole(),
                Some((levels, Some(*from))),
                Some((&whole, None)),
            )?,
            Slot::List(from) => {
                let pw = &bufs[&format!("pw{k}")];
                let v = pw.at(&[sl(0, p.words), ALL])?;
                t.gather(&v, &src.whole(), &list(index, *from, p.words)?, true, None)?;
                t.copy(
                    &ps.whole(),
                    &pw.whole(),
                    Some((&whole, None)),
                    Some((&whole, None)),
                )?;
            }
        }
        if staged > p.words {
            let walk = [(staged - p.words, word)];
            t.fill_value(&ps.whole(), 0, None, Some((&walk, Some(p.words * word))))?;
        }
    }
    let outs = [(p.out, word)];
    if p.base == "dst" {
        // The padding from the line the words end in first: then the copy is
        // the one move writing part of a line (L1.VER.017).
        let pd = &bufs["pd"];
        let head = p.out - p.out % hw.line;
        if staged > head {
            let walk = [(staged - head, word)];
            t.fill_value(&pd.whole(), 0, None, Some((&walk, Some(head * word))))?;
        }
        t.copy(
            &pd.whole(),
            &dst.whole(),
            Some((&outs, Some(p.w0 * word))),
            Some((&outs, None)),
        )?;
    }
    let s = split(p.rows / ROWS, hw.cores);
    t.par(s.cores, g::UnitKind::Vc, At::Var, |t, c| {
        let first = s.start(&c);
        t.range(s.trips(&c), |t, k| {
            let r = first.add(k).mul(ROWS);
            let code = match p.tree {
                None => Some(load(t, &mask.at(&[rows(&r.add(p.codes), ROWS), ALL])?)?),
                Some(_) => None,
            };
            let mut acc = if p.base == "dst" {
                Arg::Val(load(t, &bufs["pd"].at(&[rows(&r, ROWS), ALL])?)?)
            } else {
                Arg::Num(0.0)
            };
            let mut vals: BTreeMap<i64, V> = BTreeMap::new();
            let mut candidates: Vec<Arg> = vec![];
            for &pair in &p.pairs {
                let (k, rot) = (pair / lanes, pair % lanes);
                let v = match vals.get(&k) {
                    Some(&v) => v,
                    None => {
                        let ps = &bufs[&format!("ps{k}")];
                        let v = load(t, &ps.at(&[rows(&r, ROWS), ALL])?)?;
                        vals.insert(k, v);
                        v
                    }
                };
                let x = if rot != 0 {
                    t.xrot(Arg::Val(v), rot)?
                } else {
                    v
                };
                match code {
                    None => candidates.push(Arg::Val(x)),
                    Some(code) => {
                        let hit = t.math(Op::Cmpeq, &[Arg::Val(code), Arg::Num(pair as f64)])?;
                        acc = Arg::Val(sel(t, hit, Arg::Val(x), acc)?);
                    }
                }
            }
            match (&p.tree, code) {
                (Some((selectors, valid, zero)), _) => {
                    candidates.resize(1 << selectors.len(), Arg::Num(0.0));
                    for &offset in selectors {
                        let bit = load(t, &mask.at(&[rows(&r.add(offset), ROWS), ALL])?)?;
                        let mut next = vec![];
                        for i in (0..candidates.len()).step_by(2) {
                            next.push(Arg::Val(sel(t, bit, candidates[i + 1], candidates[i])?));
                        }
                        candidates = next;
                    }
                    acc = if *valid >= 0 {
                        let mv = load(t, &mask.at(&[rows(&r.add(*valid), ROWS), ALL])?)?;
                        Arg::Val(sel(t, mv, candidates[0], acc)?)
                    } else {
                        candidates[0]
                    };
                    if *zero >= 0 {
                        let mz = load(t, &mask.at(&[rows(&r.add(*zero), ROWS), ALL])?)?;
                        acc = Arg::Val(sel(t, mz, Arg::Num(0.0), acc)?);
                    }
                }
                (None, Some(code)) if p.zeros => {
                    let hit = t.math(Op::Cmpeq, &[Arg::Val(code), Arg::Num(ZERO as f64)])?;
                    acc = Arg::Val(sel(t, hit, Arg::Num(0.0), acc)?);
                }
                _ => {}
            }
            t.store(&out.at(&[rows(&r, ROWS), ALL])?, acc)
        })
    })?;
    t.copy(
        &dst.whole(),
        &out.whole(),
        Some((&outs, None)),
        Some((&outs, Some(p.w0 * word))),
    )
}

fn emit_shuffle(t: &mut Trace, sh: &Shuffle, bufs: &Bufs, mask: &Buf, hw: &Hw) -> R<()> {
    let (lanes, bb, word) = (hw.lanes, sh.b, hw.word);
    let (g_, rv) = (lanes / bb, sh.nb * sh.ap / hw.row);
    let (s, y) = (&bufs["sh"], &bufs[&sh.dst]);
    for tt in 0..bb {
        let walk = [(sh.nb, sh.sb * 2), (sh.a / lanes, bb * word)];
        let dwalk = [(sh.nb, sh.ap * 2), (sh.a / lanes, word)];
        t.copy(
            &s.whole(),
            &bufs[&sh.src].whole(),
            Some((&walk, Some((sh.at + tt * lanes) * 2))),
            Some((&dwalk, Some(tt * rv * hw.row * 2))),
        )?;
    }
    let sp = split(rv / ROWS, hw.cores);
    for (b, (_, stages, taps)) in sh.progs.iter().enumerate() {
        let b = b as i64;
        t.par(sp.cores, g::UnitKind::Vc, At::Var, |t, c| {
            let mut regs = vec![];
            for &(_, m) in stages {
                regs.push(load(t, &mask.at(&[sl(m, m + 1), ALL])?)?);
            }
            let mut tap = vec![];
            for &m in taps {
                tap.push(load(t, &mask.at(&[sl(m, m + 1), ALL])?)?);
            }
            let first = sp.start(&c);
            t.range(sp.trips(&c), |t, k| {
                let r = first.add(k).mul(ROWS);
                let mut acc = None;
                for tt in 0..bb {
                    let mut v = load(t, &s.at(&[rows(&r.add(tt * rv), ROWS), ALL])?)?;
                    for (&(kk, _), &m) in stages.iter().zip(&regs) {
                        let x = t.xrot(Arg::Val(v), kk)?;
                        v = sel(t, m, Arg::Val(x), Arg::Val(v))?;
                    }
                    acc = Some(match acc {
                        None => v,
                        Some(a) => {
                            let x = t.xrot(Arg::Val(v), lanes - tt * g_)?;
                            sel(t, tap[(tt - 1) as usize], Arg::Val(x), Arg::Val(a))?
                        }
                    });
                }
                let o = y.at(&[rows(&r.add(b * rv), ROWS), ALL])?;
                t.store(&o, Arg::Val(acc.unwrap()))
            })
        })?;
    }
    Ok(())
}

/// `(group, layer)` in the order they run: by layer, then width.
fn in_order(r: &Runs) -> Vec<(&Group, &Layer)> {
    let mut out: Vec<(&Group, &Layer)> = r
        .groups
        .iter()
        .flat_map(|g| g.layers.iter().map(move |l| (g, l)))
        .collect();
    out.sort_by_key(|(_, l)| l.k);
    out
}

fn emit_runs(t: &mut Trace, r: &Runs, bufs: &Bufs, index: &Buf, mask: &Buf, hw: &Hw) -> R<()> {
    let (lanes, dst) = (hw.lanes, &bufs[&r.dst]);
    let mut table = &bufs[&r.src];
    if r.margin != 0 {
        // The margin words are read only into lanes no segment selects.
        let staged = &bufs[&margin_name(r)];
        let walk = [(r.src_words, hw.word)];
        t.copy(
            &staged.whole(),
            &table.whole(),
            Some((&walk, None)),
            Some((&walk, Some(r.margin * hw.word))),
        )?;
        table = staged;
    }
    for g in &r.groups {
        let (cols, rs) = (sl(0, g.words * lanes), sl(g.row0, g.row0 + g.rows));
        let v = bufs["rs"].at(&[rs.clone(), cols.clone()])?;
        t.gather(
            &v,
            &table.whole(),
            &list(index, g.at_s, g.rows)?,
            true,
            None,
        )?;
        if let Some(at_s2) = g.at_s2 {
            let v = bufs["rs2"].at(&[rs, cols])?;
            t.gather(&v, &table.whole(), &list(index, at_s2, g.rows)?, true, None)?;
        }
    }
    let stride =
        (dst.shape()[dst.shape().len() - 1] != hw.line_values()).then_some(hw.line * hw.word);
    for (g, lay) in in_order(r) {
        let cols = sl(0, g.words * lanes);
        let dl = list(index, lay.at_d, lay.rows)?;
        if lay.base == "dst" {
            let v = bufs["rd"].at(&[sl(lay.row0, lay.row0 + lay.rows), cols.clone()])?;
            t.gather(&v, &dst.whole(), &dl, true, stride)?;
        }
        // Unrotated rows of every lane are their gathered words.
        let first = &lay.classes[0];
        let copy = if first.rot == 0 && first.mask < 0 {
            first.rows
        } else {
            0
        };
        if copy != 0 {
            let v = bufs["rs"].at(&[sl(lay.row0, lay.row0 + copy), cols.clone()])?;
            t.scatter(
                &dst.whole(),
                &v,
                &list(index, lay.at_d, copy)?,
                true,
                stride,
            )?;
        }
        if copy < lay.rows {
            for cl in &lay.classes[usize::from(copy != 0)..] {
                program(t, lay, cl, bufs, mask, hw)?;
            }
            let v = bufs["ro"].at(&[sl(lay.row0 + copy, lay.row0 + lay.rows), cols])?;
            // The copy rows' list is whole words of entries (a class is).
            let ix = list(index, lay.at_d + copy, lay.rows - copy)?;
            t.scatter(&dst.whole(), &v, &ix, true, stride)?;
        }
    }
    Ok(())
}

/// One vector-core program (one stream loop): the class's rows rotated
/// (lane l from lane l + r of the row's word, past the word from the next
/// row's) and selected under its lane mask over the layer's base.
fn program(t: &mut Trace, lay: &Layer, cl: &Cls, bufs: &Bufs, mask: &Buf, hw: &Hw) -> R<()> {
    let (s, o) = (&bufs["rs"], &bufs["ro"]);
    let per = cl.rows / ROWS / hw.cores;
    t.par(hw.cores, g::UnitKind::Vc, At::Var, |t, c| {
        let rm = if cl.rot != 0 {
            Some(load(t, &mask.at(&[sl(cl.rmask, cl.rmask + 1), ALL])?)?)
        } else {
            None
        };
        let em = if cl.mask >= 0 {
            Some(load(t, &mask.at(&[sl(cl.mask, cl.mask + 1), ALL])?)?)
        } else {
            None
        };
        t.range(per, |t, k| {
            let r = c.mul(per).add(k).mul(ROWS).add(cl.row0);
            let mut v = load(t, &s.at(&[rows(&r, ROWS), ALL])?)?;
            if let Some(rm) = rm {
                let v2 = load(t, &bufs["rs2"].at(&[rows(&r, ROWS), ALL])?)?;
                let a = t.xrot(Arg::Val(v), cl.rot)?;
                let b = t.xrot(Arg::Val(v2), cl.rot)?;
                v = sel(t, rm, Arg::Val(a), Arg::Val(b))?;
            }
            if let Some(em) = em {
                let base = if lay.base == "dst" {
                    Arg::Val(load(t, &bufs["rd"].at(&[rows(&r, ROWS), ALL])?)?)
                } else {
                    Arg::Num(0.0)
                };
                v = sel(t, em, Arg::Val(v), base)?;
            }
            t.store(&o.at(&[rows(&r, ROWS), ALL])?, Arg::Val(v))
        })
    })
}
