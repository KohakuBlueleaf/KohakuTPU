//! Planning a walk pair into parts (`relayout.plan`, `_plan` and each
//! form's planner).

use super::tables::Tables;
use super::*;
use crate::gen::cdiv;
use crate::trace::{fail, R};
use std::collections::{BTreeMap, BTreeSet, HashMap};

/// An axis `(count, source stride, destination stride)`.
pub type Axis = (i64, i64, i64);

fn stride(a: &Axis, which: usize) -> i64 {
    if which == 1 {
        a.1
    } else {
        a.2
    }
}

/// The walk pair's axes of count > 1, outermost first by destination
/// stride, adjacent axes both walks step as one merged.
pub fn canon(walk: &[(i64, i64)], dwalk: &[(i64, i64)]) -> Vec<Axis> {
    let mut axes: Vec<Axis> = walk
        .iter()
        .zip(dwalk)
        .map(|(&(n, s), &(_, d))| (n, s, d))
        .filter(|a| a.0 > 1)
        .collect();
    axes.sort_by_key(|a| -a.2.abs());
    let mut out: Vec<Axis> = vec![];
    for (n, s, d) in axes {
        match out.last_mut() {
            Some(l) if l.1 == n * s && l.2 == n * d => *l = (l.0 * n, s, d),
            _ => out.push((n, s, d)),
        }
    }
    out
}

/// `at + sum(i_k * stride_k)` over every index of `axes` in walk order;
/// stride `which` (1 source, 2 destination).
pub fn positions(axes: &[Axis], at: i64, which: usize) -> Vec<i64> {
    let mut idx = vec![at];
    for a in axes {
        let s = stride(a, which);
        idx = idx
            .iter()
            .flat_map(|&x| (0..a.0).map(move |i| x + i * s))
            .collect();
    }
    idx
}

/// Whether a word walk (byte `levels` from byte `at`) is the only move that
/// writes part of a line: one move, or whole lines only.
fn one_writer(levels: &[(i64, i64)], at: i64, hw: &Hw) -> bool {
    let n: i64 = levels.iter().map(|l| l.0).product();
    let line = hw.line * hw.word;
    let (c, s) = levels[levels.len() - 1];
    let whole = at.rem_euclid(line) == 0
        && (c * s).rem_euclid(line) == 0
        && levels[..levels.len() - 1]
            .iter()
            .all(|l| l.1.rem_euclid(line) == 0);
    n <= MOVE_WORDS || whole
}

/// [`Fill`] parts of words `w0`..: a long one from a line, so that each of
/// the moves it is cut into writes whole lines but the first's and last's.
fn fills(dst: &str, w0: i64, words: i64, hw: &Hw) -> Vec<Part> {
    let head = if words > MOVE_WORDS {
        (-w0).rem_euclid(hw.line)
    } else {
        0
    };
    let fill = |w0, words| {
        Part::Fill(Fill {
            dst: dst.into(),
            w0,
            words,
        })
    };
    let mut out = vec![];
    if head != 0 {
        out.push(fill(w0, head));
    }
    out.push(fill(w0 + head, words - head));
    out
}

/// Byte levels of a whole-word walk over `axes` (innermost contiguous whole
/// words, every other stride whole words), merged; None if not.
fn byte_levels(axes: &[Axis], at: i64, which: usize, hw: &Hw) -> Option<Levels> {
    let l = hw.lanes;
    let last = axes.last()?;
    if at.rem_euclid(l) != 0 || stride(last, which) != 1 || last.0.rem_euclid(l) != 0 {
        return None;
    }
    let mut lv = vec![];
    for a in &axes[..axes.len() - 1] {
        if stride(a, which).rem_euclid(l) != 0 {
            return None;
        }
        lv.push((a.0, stride(a, which) * 2));
    }
    lv.push((last.0 / l, hw.word));
    let mut out: Levels = vec![];
    for (n, s) in lv {
        match out.last_mut() {
            Some(o) if o.1 == n * s => *o = (o.0 * n, s),
            _ => out.push((n, s)),
        }
    }
    (out.len() as i64 <= hw.levels).then_some(out)
}

/// A run's segments: `(run, offset, start, count)` of runs at destination
/// elements `b`, `len[i]` elements each, cut at every vector-core row of
/// words.
fn segments(b: &[i64], len: &[i64], hw: &Hw) -> Vec<(usize, i64, i64, i64)> {
    let rv = hw.row;
    let mut out = vec![];
    for (j, (&b, &l)) in b.iter().zip(len).enumerate() {
        let k0 = b.div_euclid(rv);
        let k = (b + l - 1).div_euclid(rv) - k0 + 1;
        for c in 0..k {
            let lo = b.max((k0 + c) * rv);
            let hi = (b + l).min((k0 + c + 1) * rv);
            out.push((j, lo - b, lo, hi - lo));
        }
    }
    out
}

/// Each segment's layer: segments sharing a `span`-element line in
/// different layers (in destination order, the least free layer).
fn layers(b: &[i64], n: &[i64], span: i64) -> Vec<i64> {
    let mut out = vec![0; b.len()];
    let mut held: HashMap<i64, BTreeSet<i64>> = HashMap::new();
    let mut order: Vec<usize> = (0..b.len()).collect();
    order.sort_by_key(|&i| b[i]);
    for i in order {
        let (f, e) = (b[i].div_euclid(span), (b[i] + n[i] - 1).div_euclid(span));
        let mut used: BTreeSet<i64> = held.get(&f).cloned().unwrap_or_default();
        used.extend(held.get(&e).into_iter().flatten());
        let mut k = 0;
        while used.contains(&k) {
            k += 1;
        }
        out[i] = k;
        held.entry(f).or_default().insert(k);
        held.entry(e).or_default().insert(k);
    }
    out
}

/// Words of 0 staged around a table: whole lines.
fn margin_words(need: i64, hw: &Hw) -> i64 {
    cdiv(need, hw.line) * hw.line
}

/// The staged copy of a table with words of 0 around it.
pub(super) fn margin_name(r: &Runs) -> String {
    format!("rm_{}", r.src)
}

/// Where runs read and write.
struct Ends<'a> {
    src: &'a str,
    src_words: i64,
    pitch: i64,
    dst: &'a str,
    zero: bool,
}

/// [`Runs`] of segments into destination elements `b`.. (`n` each): the
/// segment's lane l of destination word w reads lane `(l + rot) mod lanes`
/// of source word `w + q` (the next word past the lane's end of the word) --
/// with `pitch` > 1, row `q` of the table, unrotated. `zero`: the lanes no
/// segment writes are 0 (first layer), else kept.
fn runs(q: &[i64], rot: &[i64], b: &[i64], n: &[i64], e: &Ends, t: &mut Tables, hw: &Hw) -> Runs {
    let (lanes, lw, lv) = (hw.lanes, hw.line, hw.line_values());
    let count = b.len();
    let l0: Vec<i64> = b.iter().map(|x| x.div_euclid(lv) * lw).collect();
    let wd: Vec<i64> = (0..count)
        .map(|i| ((b[i] + n[i] - 1).div_euclid(lanes).div_euclid(lw) + 1) * lw - l0[i])
        .collect();
    let lo: Vec<i64> = (0..count).map(|i| b[i] - l0[i] * lanes).collect();
    let full: Vec<bool> = (0..count)
        .map(|i| lo[i] == 0 && n[i] == wd[i] * lanes)
        .collect();
    let lay = layers(b, n, lv);
    let (s1, s2, margin): (Vec<i64>, Vec<i64>, i64) = if e.pitch == 1 {
        let s1: Vec<i64> = (0..count).map(|i| l0[i] + q[i]).collect();
        let s2: Vec<i64> = (0..count)
            .map(|i| if rot[i] != 0 { s1[i] + 1 } else { s1[i] })
            .collect();
        let need = if count > 0 {
            let lo_s = s1.iter().min().unwrap();
            let hi_s = (0..count).map(|i| s2[i] + wd[i]).max().unwrap();
            0.max(-lo_s).max(hi_s - e.src_words)
        } else {
            0
        };
        (s1, s2, margin_words(need, hw))
    } else {
        (q.to_vec(), q.to_vec(), 0)
    };
    let unit = lcm(ROWS * hw.cores, ENTRIES);
    let mut groups = vec![];
    let mut row = 0;
    let widths: BTreeSet<i64> = wd.iter().copied().collect();
    for w in widths {
        let in_w: Vec<usize> = (0..count).filter(|&i| wd[i] == w).collect();
        let (mut s_rows, mut s2_rows, mut out, g0) = (vec![], vec![], vec![], row);
        let ks: BTreeSet<i64> = in_w.iter().map(|&i| lay[i]).collect();
        for k in ks {
            let mut by: BTreeMap<(i64, i64, i64), Vec<usize>> = BTreeMap::new();
            for &i in in_w.iter().filter(|&&i| lay[i] == k) {
                let rm = if rot[i] != 0 { t.rot_mask(rot[i]) } else { -1 };
                let em = if full[i] {
                    -1
                } else {
                    t.lane_mask(lo[i], n[i])
                };
                by.entry((rot[i], rm, em)).or_default().push(i);
            }
            let (mut classes, mut dlist, r0) = (vec![], vec![], row);
            for ((r, rm, em), mut ix) in by {
                let last = *ix.last().unwrap();
                while ix.len() as i64 % unit != 0 {
                    ix.push(last);
                }
                classes.push(Cls {
                    rot: r,
                    rmask: rm,
                    mask: em,
                    row0: row,
                    rows: ix.len() as i64,
                });
                s_rows.extend(ix.iter().map(|&i| s1[i] + margin));
                s2_rows.extend(ix.iter().map(|&i| s2[i] + margin));
                dlist.extend(ix.iter().map(|&i| l0[i].div_euclid(lw)));
                row += ix.len() as i64;
            }
            let base = if e.zero && k == 0 { "zero" } else { "dst" };
            out.push(Layer {
                k,
                base: base.into(),
                row0: r0,
                rows: row - r0,
                at_d: t.put(&dlist),
                classes,
            });
        }
        let rotated = in_w.iter().any(|&i| rot[i] != 0);
        let at_s = t.put(&s_rows);
        let at_s2 = rotated.then(|| t.put(&s2_rows));
        groups.push(Group {
            words: w,
            row0: g0,
            rows: row - g0,
            at_s,
            at_s2,
            layers: out,
        });
    }
    Runs {
        src: e.src.into(),
        src_words: e.src_words,
        pitch: e.pitch,
        margin,
        dst: e.dst.into(),
        groups,
    }
}

/// [`Runs`] of `len`-element runs from source elements `a` (a word table)
/// to destination elements `b`.
fn run_parts(a: &[i64], b: &[i64], len: i64, e: &Ends, t: &mut Tables, hw: &Hw) -> Runs {
    let lanes = hw.lanes;
    let seg = segments(b, &vec![len; b.len()], hw);
    let (mut q, mut rot, mut sb, mut sn) = (vec![], vec![], vec![], vec![]);
    for &(run, off, start, count) in &seg {
        let delta = a[run] + off - start;
        let r = delta.rem_euclid(lanes);
        q.push((delta - r) / lanes);
        rot.push(r);
        sb.push(start);
        sn.push(count);
    }
    runs(&q, &rot, &sb, &sn, e, t, hw)
}

pub(super) fn staging(r: &Runs, hw: &Hw) -> Vec<(String, i64, i64)> {
    let rows: i64 = r.groups.iter().map(|g| g.rows).sum();
    let mut out = vec![];
    if rows != 0 {
        out.push(("rs".into(), rows, hw.row));
        out.push(("ro".into(), rows, hw.row));
    }
    if r.groups.iter().any(|g| g.at_s2.is_some()) {
        out.push(("rs2".into(), rows, hw.row));
    }
    if r.groups
        .iter()
        .any(|g| g.layers.iter().any(|l| l.base == "dst"))
    {
        out.push(("rd".into(), rows, hw.row));
    }
    if r.margin != 0 {
        let words = cdiv(r.src_words + 2 * r.margin, hw.line) * hw.line;
        out.push((margin_name(r), words, hw.lanes));
    }
    out
}

/// The relayout of `m` destination elements (a port from a line, whole
/// words) from element `at` of `src_n` source elements walked by `walk`
/// into the elements `dwalk` reaches from `dat`; every other element of
/// `[z0, m)` 0 (with `keep`, as it is), those before `z0` kept; with `tree`,
/// a [`Pull`]'s codes predecoded ([`tree_pull`]). A refusal for elements
/// outside the source or `[z0, m)`.
#[allow(clippy::too_many_arguments)]
pub fn plan(
    src_n: i64,
    walk: &[(i64, i64)],
    at: i64,
    dwalk: &[(i64, i64)],
    dat: i64,
    m: i64,
    keep: bool,
    hw: &Hw,
    z0: i64,
    tree: bool,
) -> R<Result<Plan, Unplanned>> {
    let got = plan_forms(src_n, walk, at, dwalk, dat, m, keep, hw, z0)?;
    Ok(got.map(|p| {
        let p = stage(p, keep || z0 > 0, hw);
        if tree {
            tree_pull(p, hw)
        } else {
            p
        }
    }))
}

/// Words of the destination port of `m` elements: their words, as the
/// storage holds them (API.MEM.002); whole lines under [`LINES_MUTANT`].
pub fn dst_words(m: i64, hw: &Hw) -> i64 {
    if crate::mutants::on(LINES_MUTANT) {
        return cdiv(m, hw.line_values()) * hw.line;
    }
    cdiv(m, hw.lanes)
}

fn on_lines(name: &mut String) {
    if *name == "dst" {
        *name = "dx".into();
    }
}

fn copy(src: &str, dst: &str, levels: Levels, at: i64, dlevels: Levels, dat: i64) -> Part {
    Part::Words(Words {
        src: src.into(),
        dst: dst.into(),
        levels,
        at,
        dlevels,
        dat,
    })
}

/// `pl` whose destination words end inside a line and runs write the
/// destination (as whole lines): its parts on the lines of scratch `dx`,
/// then one walk copying the words out, the one move writing part of the
/// destination's last line (L1.VER.017). With `kept` (words the parts leave
/// as they are) the words are first copied in, the last line's read again
/// to its end so that the copy writes whole lines.
fn stage(mut pl: Plan, kept: bool, hw: &Hw) -> Plan {
    let (dw, word, line) = (pl.dst_words, hw.word, hw.line);
    let runs = pl
        .parts
        .iter()
        .any(|p| matches!(p, Part::Runs(r) if r.dst == "dst"));
    if dw % line == 0 || !runs {
        return pl;
    }
    let tail = dw % line;
    let (head, again) = (dw - tail, cdiv(line, tail));
    for p in &mut pl.parts {
        let (src, dst) = match p {
            Part::Fill(f) => (None, &mut f.dst),
            Part::Words(w) => (Some(&mut w.src), &mut w.dst),
            Part::Runs(r) => (Some(&mut r.src), &mut r.dst),
            Part::Splat(s) => (Some(&mut s.src), &mut s.dst),
            Part::Pull(p) => (Some(&mut p.src), &mut p.dst),
            Part::Exchange(x) => (Some(&mut x.src), &mut x.dst),
            Part::Shuffle(s) => (Some(&mut s.src), &mut s.dst),
        };
        if let Some(src) = src {
            on_lines(src);
        }
        on_lines(dst);
    }
    if kept {
        let mut copy_in = vec![];
        if head > 0 {
            let lines = vec![(head, word)];
            copy_in.push(copy("dst", "dx", lines.clone(), 0, lines, 0));
        }
        copy_in.push(copy(
            "dst",
            "dx",
            vec![(again, 0), (tail, word)],
            head * word,
            vec![(again * tail, word)],
            head * word,
        ));
        pl.parts.splice(0..0, copy_in);
    }
    let words = vec![(dw, word)];
    pl.parts.push(copy("dx", "dst", words.clone(), 0, words, 0));
    let rows = cdiv(head + again * tail, line);
    pl.scratch.push(("dx".into(), rows, hw.line_values()));
    pl.scratch.sort();
    pl
}

#[allow(clippy::too_many_arguments)]
fn plan_forms(
    src_n: i64,
    walk: &[(i64, i64)],
    at: i64,
    dwalk: &[(i64, i64)],
    dat: i64,
    m: i64,
    keep: bool,
    hw: &Hw,
    z0: i64,
) -> R<Result<Plan, Unplanned>> {
    let (lanes, lv) = (hw.lanes, hw.line_values());
    let sw = cdiv(src_n, lanes);
    let dw = dst_words(m, hw);
    let axes = canon(walk, dwalk);
    let (spos, dpos) = (positions(&axes, at, 1), positions(&axes, dat, 2));
    if spos.iter().any(|&x| x < 0 || x >= src_n) {
        return fail("L2.REL.005", format!("source elements outside 0..{src_n}"));
    }
    if dpos.iter().any(|&x| x < z0 || x >= m) {
        return fail(
            "L2.REL.005",
            format!("destination elements outside {z0}..{m}"),
        );
    }
    let no = |why: &str| Ok(Err(Unplanned(why.into())));
    if axes.is_empty() {
        return no("one element");
    }
    if dpos.iter().collect::<BTreeSet<_>>().len() != dpos.len() {
        return no("a destination element written twice");
    }
    let mut t = Tables::new(*hw);
    let mut parts: Vec<Part> = vec![];
    if !keep && (dpos.len() as i64) < m - z0 {
        parts.extend(fills(
            "dst",
            z0.div_euclid(lanes),
            cdiv(m, lanes) - z0.div_euclid(lanes),
            hw,
        ));
    }
    // Lanes no segment writes may be 0 where the port is the zero range.
    let zero = !keep && z0 == 0 && m == dw * lanes;
    // A walk may write part of a line that only a fill of whole lines writes.
    let whole = parts.is_empty() || (z0.rem_euclid(lv) == 0 && m.rem_euclid(lv) == 0);
    let room = (!keep).then_some((z0, m));
    let s = (sw, dw, zero, whole, room);
    let sl = byte_levels(&axes, at, 1, hw);
    let dl = byte_levels(&axes, dat, 2, hw);
    if let (Some(sl), Some(dl)) = (&sl, &dl) {
        if whole && one_writer(dl, dat * 2, hw) {
            parts.push(Part::Words(Words {
                src: "src".into(),
                dst: "dst".into(),
                levels: sl.clone(),
                at: at * 2,
                dlevels: dl.clone(),
                dat: dat * 2,
            }));
            return Ok(Ok(done(parts, vec![], t, sw, dw)));
        }
    }
    let (n, sa, d) = axes[axes.len() - 1];
    if d != 1 {
        // Every element its own destination run: a transpose whose
        // destination-contiguous side is one element.
        let Some(j) = axes.iter().position(|x| x.1 == 1) else {
            return no("no axis is contiguous on either side");
        };
        let mut ax = axes.clone();
        ax.push((1, 0, 1));
        return transpose(&ax, j, at, dat, s, parts, t, hw);
    }
    let rows = &axes[..axes.len() - 1];
    if sa == 1 {
        let e = Ends {
            src: "src",
            src_words: sw,
            pitch: 1,
            dst: "dst",
            zero,
        };
        let r = run_parts(
            &positions(rows, at, 1),
            &positions(rows, dat, 2),
            n,
            &e,
            &mut t,
            hw,
        );
        let scratch = staging(&r, hw);
        parts.push(Part::Runs(r));
        return Ok(Ok(done(parts, scratch, t, sw, dw)));
    }
    if sa == 0 {
        if n <= lanes {
            if let Ok(p) = pull(&axes, at, dat, sw, z0, m, keep, t, hw) {
                return Ok(Ok(p));
            }
        }
        return Ok(Ok(splat(rows, n, at, dat, s, parts, Tables::new(*hw), hw)));
    }
    let mut rows = rows.to_vec();
    let j = match rows.iter().position(|x| x.1 == 1) {
        Some(j) => {
            if let Ok(Some(p)) = shuffle(&rows, j, n, sa, at, dat, s, &parts, &mut t, hw) {
                return Ok(Ok(p));
            }
            t = Tables::new(*hw);
            j
        }
        None => {
            // A strided read: a transpose whose source-contiguous side is
            // one element.
            rows.push((1, 1, 0));
            rows.len() - 1
        }
    };
    rows.push(axes[axes.len() - 1]);
    transpose(&rows, j, at, dat, s, parts, t, hw)
}

fn done(parts: Vec<Part>, scratch: Vec<(String, i64, i64)>, t: Tables, sw: i64, dw: i64) -> Plan {
    let index = if t.index.is_empty() {
        vec![0.0; 8]
    } else {
        t.index
    };
    let masks = if t.masks.is_empty() {
        vec![vec![0.0; t.hw.row as usize]]
    } else {
        t.masks
    };
    let mut merged: BTreeMap<String, (i64, i64)> = BTreeMap::new();
    for (name, rows, width) in scratch {
        let held = merged.get(&name).map_or(0, |m| m.0);
        merged.insert(name, (rows.max(held), width));
    }
    Plan {
        parts,
        scratch: merged.into_iter().map(|(k, (r, w))| (k, r, w)).collect(),
        index,
        masks,
        src_words: sw,
        dst_words: dw,
    }
}

/// The plan's sizes and destination facts, passed to each form.
type Shape = (i64, i64, bool, bool, Option<(i64, i64)>);

/// Runs of `n` copies of one value: each value a row of copies (row v of
/// `sp` holds the value at element `lanes * w0 + v`), then runs from that
/// row.
#[allow(clippy::too_many_arguments)]
fn splat(
    rows: &[Axis],
    n: i64,
    at: i64,
    dat: i64,
    (sw, dw, zero, _, _): Shape,
    mut parts: Vec<Part>,
    mut t: Tables,
    hw: &Hw,
) -> Plan {
    let lanes = hw.lanes;
    let vals = positions(rows, at, 1);
    let b = positions(rows, dat, 2);
    let w0 = vals.iter().min().unwrap().div_euclid(lanes);
    let count = vals.iter().max().unwrap().div_euclid(lanes) - w0 + 1;
    let ones = t.rows(vec![vec![1.0; hw.row as usize]; lanes as usize]);
    let sp = Splat {
        src: "src".into(),
        w0,
        count,
        dst: "sp".into(),
        ones,
    };
    let seg = segments(&b, &vec![n; b.len()], hw);
    let q: Vec<i64> = seg.iter().map(|s| vals[s.0] - w0 * lanes).collect();
    let sb: Vec<i64> = seg.iter().map(|s| s.2).collect();
    let sn: Vec<i64> = seg.iter().map(|s| s.3).collect();
    let e = Ends {
        src: "sp",
        src_words: count * lanes * hw.row_words(),
        pitch: hw.row_words(),
        dst: "dst",
        zero,
    };
    let r = runs(&q, &vec![0; q.len()], &sb, &sn, &e, &mut t, hw);
    let mut scratch = vec![("sp".into(), count * lanes, hw.row)];
    scratch.extend(staging(&r, hw));
    parts.push(Part::Splat(sp));
    parts.push(Part::Runs(r));
    done(parts, scratch, t, sw, dw)
}

/// `(at, ((count, stride), ..))`, outermost first and at most `levels` of
/// them, whose walk visits the integers `seq` in order; None when no such
/// walk is.
pub fn word_levels(seq: &[i64], levels: i64) -> Option<(i64, Vec<(i64, i64)>)> {
    let mut seq = seq.to_vec();
    let mut out: Vec<(i64, i64)> = vec![];
    while seq.len() > 1 {
        let n = seq.len();
        let d: Vec<i64> = seq.windows(2).map(|w| w[1] - w[0]).collect();
        let run = d.iter().position(|&x| x != d[0]).map_or(n, |b| b + 1);
        let mut got = None;
        for c in (2..=run.min(n)).rev() {
            if !n.is_multiple_of(c) {
                continue;
            }
            let same = seq
                .chunks(c)
                .all(|r| (0..c).all(|j| r[j] - r[0] == seq[j] - seq[0]));
            if same {
                got = Some(c);
                break;
            }
        }
        let c = got?;
        out.insert(0, (c as i64, d[0]));
        seq = seq.chunks(c).map(|r| r[0]).collect();
    }
    if out.len() as i64 > levels {
        return None;
    }
    if out.is_empty() {
        out.push((1, 0));
    }
    Some((seq[0], out))
}

/// [`Pull`] of every destination vector-core row the walk pair writes: each
/// destination word's lanes from at most [`PULL_SOURCES`] source words (else
/// `Unplanned`), the zero range's other lanes 0, the rest kept; the zero
/// range outside those rows filled.
#[allow(clippy::too_many_arguments)]
fn pull(
    axes: &[Axis],
    at: i64,
    dat: i64,
    sw: i64,
    z0: i64,
    m: i64,
    keep: bool,
    mut t: Tables,
    hw: &Hw,
) -> Result<Plan, Unplanned> {
    let (lanes, rv, rw) = (hw.lanes, hw.row, hw.row_words());
    let (spos, dpos) = (positions(axes, at, 1), positions(axes, dat, 2));
    let dw = dst_words(m, hw);
    let r0 = dpos.iter().min().unwrap().div_euclid(rv);
    let r1 = dpos.iter().max().unwrap().div_euclid(rv) + 1;
    let (w0, words) = (r0 * rw, (r1 - r0) * rw);
    let out = (r1 * rw).min(dw) - w0;
    let total = (words * lanes) as usize;
    let e: Vec<i64> = (0..total as i64).map(|i| w0 * lanes + i).collect();
    let mut src_of = vec![-1i64; total];
    for (d, s) in dpos.iter().zip(&spos) {
        src_of[(d - w0 * lanes) as usize] = *s;
    }
    let mut code: Vec<i64> = e
        .iter()
        .map(|&x| {
            if !keep && x >= z0 && x < m {
                ZERO
            } else {
                KEEP
            }
        })
        .collect();
    let lanes_u = lanes as usize;
    let sword: Vec<i64> = src_of
        .iter()
        .map(|&s| if s >= 0 { s.div_euclid(lanes) } else { -1 })
        .collect();
    let mut left: Vec<bool> = src_of.iter().map(|&s| s >= 0).collect();
    let mut slot = vec![0i64; total];
    let mut seqs: Vec<Vec<i64>> = vec![];
    let nw = words as usize;
    for k in 0..PULL_SOURCES {
        let has: Vec<bool> = (0..nw)
            .map(|w| left[w * lanes_u..(w + 1) * lanes_u].iter().any(|&x| x))
            .collect();
        if !has.iter().any(|&x| x) {
            break;
        }
        let first: Vec<i64> = (0..nw)
            .map(|w| {
                let row = &left[w * lanes_u..(w + 1) * lanes_u];
                let l = row.iter().position(|&x| x).unwrap_or(0);
                sword[w * lanes_u + l]
            })
            .collect();
        for (w, &f) in first.iter().enumerate() {
            for l in 0..lanes_u {
                let i = w * lanes_u + l;
                if left[i] && sword[i] == f {
                    slot[i] = k as i64;
                    left[i] = false;
                }
            }
        }
        // Words with no k-th source take the previous word's (any source
        // word does: none of their lanes reads it).
        let mut idx = vec![0usize; nw];
        let mut most = 0;
        for w in 0..nw {
            most = most.max(if has[w] { w } else { 0 });
            idx[w] = most;
        }
        let any = has.iter().position(|&x| x).unwrap();
        seqs.push(
            (0..nw)
                .map(|w| {
                    if has[idx[w]] {
                        first[idx[w]]
                    } else {
                        first[any]
                    }
                })
                .collect(),
        );
    }
    if left.iter().any(|&x| x) {
        return Err(Unplanned(format!(
            "a destination word from over {PULL_SOURCES} source words"
        )));
    }
    for i in 0..total {
        if src_of[i] >= 0 {
            let rot = (src_of[i].rem_euclid(lanes) - e[i].rem_euclid(lanes)).rem_euclid(lanes);
            code[i] = slot[i] * lanes + rot;
        }
    }
    let base = if code.contains(&KEEP) { "dst" } else { "zero" };
    let zeros = base == "dst" && code.contains(&ZERO);
    let rows = cdiv(r1 - r0, ROWS) * ROWS;
    let pad = (rows - (r1 - r0)) * rv;
    let fill = if base == "dst" { KEEP } else { ZERO };
    code.extend(std::iter::repeat_n(fill, pad as usize));
    let codes = t.rows(
        code.chunks(rv as usize)
            .map(|r| r.iter().map(|&c| c as f32).collect())
            .collect(),
    );
    let (mut slots, mut scratch) = (vec![], vec![]);
    for (k, seq) in seqs.iter().enumerate() {
        match word_levels(seq, hw.levels) {
            Some((first, lv)) => slots.push(Slot::Walk(
                lv.iter().map(|&(c, s)| (c, s * hw.word)).collect(),
                first * hw.word,
            )),
            None => {
                slots.push(Slot::List(t.put(seq)));
                scratch.push((format!("pw{k}"), words, lanes));
            }
        }
        scratch.push((format!("ps{k}"), rows, rv));
    }
    if base == "dst" {
        scratch.push(("pd".into(), rows, rv));
    }
    scratch.push(("po".into(), rows, rv));
    let mut parts = vec![];
    if !keep {
        let (a, b) = (z0.div_euclid(lanes), cdiv(m, lanes));
        if z0.rem_euclid(lanes) != 0 && !(w0 <= a && a < w0 + out) {
            return Err(Unplanned(
                "a zero range from inside a word outside the rows".into(),
            ));
        }
        if a < w0 {
            parts.extend(fills("dst", a, w0 - a, hw));
        }
        if b > w0 + out {
            parts.extend(fills("dst", w0 + out, b - w0 - out, hw));
        }
    }
    let pairs: BTreeSet<i64> = code.iter().copied().filter(|&c| c >= 0).collect();
    parts.push(Part::Pull(Pull {
        src: "src".into(),
        dst: "dst".into(),
        w0,
        words,
        rows,
        out,
        slots,
        codes,
        pairs: pairs.into_iter().collect(),
        base: base.into(),
        zeros,
        tree: None,
    }));
    Ok(done(parts, scratch, t, sw, dw))
}

/// Axis `j` contiguous in the source, the innermost destination axis (A
/// elements) strided there: `tx[nb, A', B']` from the source (padding 0),
/// the exchange into `ty[nb, B', A']`, the destination from it; a side of
/// whole words one walk, else runs. A batch axis of source stride 0 repeats a
/// block: only the held blocks are exchanged, the out side writes the repeats.
#[allow(clippy::too_many_arguments)]
fn transpose(
    axes: &[Axis],
    j: usize,
    at: i64,
    dat: i64,
    (sw, dw, zero, whole, room): Shape,
    parts: Vec<Part>,
    mut t: Tables,
    hw: &Hw,
) -> R<Result<Plan, Unplanned>> {
    let lanes = hw.lanes;
    let (a, sa, _) = axes[axes.len() - 1];
    let (b, _, db) = axes[j];
    let batch: Vec<Axis> = axes[..axes.len() - 1]
        .iter()
        .enumerate()
        .filter(|(k, _)| *k != j)
        .map(|(_, x)| *x)
        .collect();
    let held: Vec<Axis> = batch.iter().filter(|x| x.1 != 0).copied().collect();
    let nb: i64 = held.iter().map(|x| x.0).product();
    let ao = extend(&batch, b, db, a, dat, room, hw);
    let ap = ao.max(cdiv(a, lanes) * lanes);
    let bp = cdiv(b, lanes) * lanes;
    let xw = cdiv(cdiv(nb * ap * bp, lanes), hw.line) * hw.line;
    let mut parts = parts;
    let mut whole = whole;
    // tx is scattered into (rows of whole lines), ty gathered from (words).
    let mut scratch = vec![
        ("tx".to_string(), xw / hw.line, hw.line_values()),
        ("ty".to_string(), xw, lanes),
    ];
    if ao > a {
        let (z0, m) = room.unwrap();
        if batch.iter().map(|x| x.0).product::<i64>() * b * ao == m - z0 {
            // The out side writes every element of the zero range.
            parts.retain(|p| !matches!(p, Part::Fill(f) if f.dst == "dst"));
            whole = true;
        }
        parts.extend(fills("tx", 0, xw, hw));
    }
    let mut in_axes: Vec<Axis> = held.iter().map(|x| (x.0, x.1, 0)).collect();
    in_axes.extend([(a, sa, 0), (b, 1, 0)]);
    let sl = byte_levels(&in_axes, at, 1, hw);
    let dl = byte_levels(&dense_axes(&held, a, b, ap, bp), 0, 2, hw);
    match (sl, dl) {
        (Some(sl), Some(dl)) if one_writer(&dl, 0, hw) => parts.push(Part::Words(Words {
            src: "src".into(),
            dst: "tx".into(),
            levels: sl,
            at: at * 2,
            dlevels: dl,
            dat: 0,
        })),
        _ => {
            let src_at: Vec<i64> = positions(&held, at, 1)
                .iter()
                .flat_map(|&p| (0..a).map(move |i| p + i * sa))
                .collect();
            let dst_at: Vec<i64> = (0..nb * a).map(|i| (i / a * ap + i % a) * bp).collect();
            let e = Ends {
                src: "src",
                src_words: sw,
                pitch: 1,
                dst: "tx",
                zero: true,
            };
            let r = run_parts(&src_at, &dst_at, b, &e, &mut t, hw);
            scratch.extend(staging(&r, hw));
            parts.push(Part::Runs(r));
        }
    }
    let masks = t.rows(transpose_masks());
    parts.push(Part::Exchange(Exchange {
        src: "tx".into(),
        dst: "ty".into(),
        nb,
        a: ap,
        b: bp,
        masks,
    }));
    let dbs = positions(&batch, dat, 2);
    let mut out_axes: Vec<Axis> = batch.iter().map(|x| (x.0, 0, x.2)).collect();
    out_axes.extend([(b, 0, db), (ao, 0, 1)]);
    let dl = byte_levels(&out_axes, dat, 2, hw);
    let out_dense = dense_axes(&batch, b, ao, bp, ap);
    let sl = byte_levels(&out_dense, 0, 2, hw);
    match (dl, sl) {
        (Some(dl), Some(sl)) if whole && one_writer(&dl, dat * 2, hw) => {
            parts.push(Part::Words(Words {
                src: "ty".into(),
                dst: "dst".into(),
                levels: sl,
                at: 0,
                dlevels: dl,
                dat: dat * 2,
            }))
        }
        _ => {
            let src_at = positions(&out_dense[..out_dense.len() - 1], 0, 2);
            let dst_at: Vec<i64> = dbs
                .iter()
                .flat_map(|&p| (0..b).map(move |i| p + i * db))
                .collect();
            let e = Ends {
                src: "ty",
                src_words: xw,
                pitch: 1,
                dst: "dst",
                zero,
            };
            let r = run_parts(&src_at, &dst_at, a, &e, &mut t, hw);
            scratch.extend(staging(&r, hw));
            parts.push(Part::Runs(r));
        }
    }
    Ok(Ok(done(parts, scratch, t, sw, dw)))
}

/// `transpose::masks`: [2, 128], the lanes whose bits 0 and 2 differ, then
/// bits 1 and 3.
fn transpose_masks() -> Vec<Vec<f32>> {
    (0..2)
        .map(|i| {
            (0..128)
                .map(|v| {
                    let l = v % 16;
                    f32::from(u8::from((l >> i) & 1 != (l >> (i + 2)) & 1))
                })
                .collect()
        })
        .collect()
}

/// The row length a transpose's out side writes: `a`, or `a` rounded up to
/// whole lines when each destination row starts on a line and the elements
/// it adds are in the zero range `room` and no row's elements.
fn extend(
    batch: &[Axis],
    b: i64,
    db: i64,
    a: i64,
    dat: i64,
    room: Option<(i64, i64)>,
    hw: &Hw,
) -> i64 {
    let lv = hw.line_values();
    let ae = cdiv(a, lv) * lv;
    let Some((z0, m)) = room else { return a };
    if ae == a {
        return a;
    }
    let lead: Vec<Axis> = batch.iter().map(|x| (x.0, 0, x.2)).collect();
    let rows: Vec<i64> = positions(&lead, dat, 2)
        .iter()
        .flat_map(|&p| (0..b).map(move |i| p + i * db))
        .collect();
    if rows.iter().any(|r| r.rem_euclid(lv) != 0) {
        return a;
    }
    let span: Vec<i64> = rows
        .iter()
        .flat_map(|&r| (0..ae).map(move |i| r + i))
        .collect();
    if span.iter().any(|&x| x < z0 || x >= m) {
        return a;
    }
    if span.iter().collect::<BTreeSet<_>>().len() != span.len() {
        return a;
    }
    ae
}

/// Rotation stages `(k, mask)` (lane l from lane l + k where the mask is
/// set) taking lane `j B + b` to lane j for j < lanes / B, least shift
/// first; `Unplanned` when a stage would overwrite a lane still wanted.
fn compress(bb: i64, b: i64, lanes: i64) -> Result<Vec<(i64, Vec<bool>)>, Unplanned> {
    let want: Vec<(i64, i64)> = (0..lanes / bb).map(|j| (j * bb + b, j)).collect();
    let mut at: Vec<(i64, i64)> = want.iter().map(|&(e, _)| (e, e)).collect();
    let mut out = vec![];
    let mut k = 1;
    while k < lanes {
        let moves: Vec<(usize, i64)> = at
            .iter()
            .enumerate()
            .filter(|(i, (_, p))| (p - want[*i].1) & k != 0)
            .map(|(i, (_, p))| (i, p - k))
            .collect();
        let moved: BTreeSet<usize> = moves.iter().map(|m| m.0).collect();
        let held: BTreeSet<i64> = at
            .iter()
            .enumerate()
            .filter(|(i, _)| !moved.contains(i))
            .map(|(_, (_, p))| *p)
            .collect();
        if moves.iter().any(|&(_, q)| held.contains(&q) || q < 0) {
            return Err(Unplanned("a lane compress that overwrites itself".into()));
        }
        if !moves.is_empty() {
            let mut mask = vec![false; lanes as usize];
            for &(_, q) in &moves {
                mask[q as usize] = true;
            }
            out.push((k, mask));
            for (i, q) in moves {
                at[i].1 = q;
            }
        }
        k *= 2;
    }
    Ok(out)
}

/// A transpose whose contiguous source side (B elements) is one of
/// [`SHUFFLE`] and the other (A = `n`, stride B) whole words: [`Shuffle`]
/// into `ty`, then the destination from it; None when it is not one.
#[allow(clippy::too_many_arguments)]
fn shuffle(
    rows: &[Axis],
    j: usize,
    n: i64,
    s: i64,
    at: i64,
    dat: i64,
    (sw, dw, zero, whole, _): Shape,
    parts: &[Part],
    t: &mut Tables,
    hw: &Hw,
) -> Result<Option<Plan>, Unplanned> {
    let lanes = hw.lanes;
    let bb = rows[j].0;
    let batch: Vec<Axis> = rows
        .iter()
        .enumerate()
        .filter(|(k, _)| *k != j)
        .map(|(_, x)| *x)
        .collect();
    if !SHUFFLE.contains(&bb) || s != bb || n % lanes != 0 || at.rem_euclid(lanes) != 0 {
        return Ok(None);
    }
    if batch.iter().any(|x| x.1.rem_euclid(lanes) != 0) || batch.len() > 1 {
        return Ok(None);
    }
    let (nb, sb) = batch.first().map_or((1, 0), |x| (x.0, x.1));
    // A row of a stream: whole trips of vector-core rows.
    let ap = cdiv(n, ROWS * hw.row) * ROWS * hw.row;
    let g = lanes / bb;
    let mut progs = vec![];
    for b in 0..bb {
        let stages = compress(bb, b, lanes)?;
        let m0 = t.rows(
            stages
                .iter()
                .map(|(_, m)| {
                    (0..hw.row)
                        .map(|v| f32::from(u8::from(m[(v % lanes) as usize])))
                        .collect()
                })
                .collect(),
        );
        let taps = t.rows(
            (1..bb)
                .map(|tt| {
                    (0..hw.row)
                        .map(|v| {
                            let l = v % lanes;
                            f32::from(u8::from(l >= tt * g && l < (tt + 1) * g))
                        })
                        .collect()
                })
                .collect(),
        );
        progs.push((
            m0,
            stages
                .iter()
                .enumerate()
                .map(|(i, (k, _))| (*k, m0 + i as i64))
                .collect(),
            (taps..taps + bb - 1).collect(),
        ));
    }
    let rv = nb * ap / hw.row;
    let mut parts = parts.to_vec();
    parts.push(Part::Shuffle(Shuffle {
        src: "src".into(),
        at,
        nb,
        sb,
        a: n,
        b: bb,
        ap,
        dst: "ty".into(),
        progs,
    }));
    let mut scratch = vec![
        ("sh".to_string(), bb * rv, hw.row),
        ("ty".to_string(), bb * rv, hw.row),
    ];
    // ty row (b, n) at element (b nb + n) Ap; the destination's (batch, b, i).
    let mut d_axes: Vec<Axis> = batch.iter().map(|x| (x.0, 0, x.2)).collect();
    d_axes.extend([(bb, 0, rows[j].2), (n, 0, 1)]);
    let mut s_axes: Vec<Axis> = batch.iter().map(|x| (x.0, 0, ap)).collect();
    s_axes.extend([(bb, 0, nb * ap), (n, 0, 1)]);
    let dl = byte_levels(&d_axes, dat, 2, hw);
    let sl = byte_levels(&s_axes, 0, 2, hw);
    let mut t_now = std::mem::replace(t, Tables::new(*hw));
    if let (Some(dl), Some(sl)) = (dl, sl) {
        if whole && one_writer(&dl, dat * 2, hw) {
            parts.push(Part::Words(Words {
                src: "ty".into(),
                dst: "dst".into(),
                levels: sl,
                at: 0,
                dlevels: dl,
                dat: dat * 2,
            }));
            return Ok(Some(done(parts, scratch, t_now, sw, dw)));
        }
    }
    // Runs read a word table: ty copied into one.
    let tw = bb * rv * hw.row_words();
    parts.push(Part::Words(Words {
        src: "ty".into(),
        dst: "tz".into(),
        levels: vec![(tw, hw.word)],
        at: 0,
        dlevels: vec![(tw, hw.word)],
        dat: 0,
    }));
    let a_at = positions(&s_axes[..s_axes.len() - 1], 0, 2);
    let b_at = positions(&d_axes[..d_axes.len() - 1], dat, 2);
    let e = Ends {
        src: "tz",
        src_words: tw,
        pitch: 1,
        dst: "dst",
        zero,
    };
    let r = run_parts(&a_at, &b_at, n, &e, &mut t_now, hw);
    scratch.push(("tz".into(), tw, lanes));
    scratch.extend(staging(&r, hw));
    parts.push(Part::Runs(r));
    Ok(Some(done(parts, scratch, t_now, sw, dw)))
}

/// Axes (destination strides) of `[batch.., X, Y]` held in a dense `[nb,
/// Xp, Yp]` block.
fn dense_axes(batch: &[Axis], x: i64, y: i64, xp: i64, yp: i64) -> Vec<Axis> {
    let mut out = vec![];
    let mut stride = xp * yp;
    for a in batch.iter().rev() {
        let held = a.1 != 0;
        out.insert(0, (a.0, 0, if held { stride } else { 0 }));
        stride *= if held { a.0 } else { 1 };
    }
    out.extend([(x, 0, yp), (y, 0, 1)]);
    out
}

/// A [`Pull`]'s gather codes predecoded into shared selector bits for a
/// balanced tree, where the extra mask streams fit the core's views; the
/// code rows stay the independent executor's source of truth.
pub(super) fn tree_pull(mut pl: Plan, hw: &Hw) -> Plan {
    let rv = hw.row as usize;
    let mut next = pl.masks.len() as i64;
    let mut extra: Vec<Vec<f32>> = vec![];
    for part in &mut pl.parts {
        let Part::Pull(p) = part else { continue };
        if p.pairs.len() < 2 {
            continue;
        }
        let code: Vec<i64> = pl.masks[p.codes as usize..(p.codes + p.rows) as usize]
            .iter()
            .flatten()
            .map(|&v| v as i64)
            .collect();
        let bits = 64 - ((p.pairs.len() - 1) as u64).leading_zeros() as i64;
        let negative = code.iter().any(|&c| c < 0);
        let views = p.slots.len() as i64
            + bits
            + i64::from(negative)
            + i64::from(p.zeros)
            + i64::from(p.base == "dst")
            + 1;
        if views > hw.descriptors {
            continue;
        }
        let index: Vec<i64> = code
            .iter()
            .map(|c| p.pairs.partition_point(|x| x < c) as i64)
            .collect();
        let as_rows =
            |vals: Vec<f32>| -> Vec<Vec<f32>> { vals.chunks(rv).map(<[f32]>::to_vec).collect() };
        let mut selectors = vec![];
        for bit in 0..bits {
            extra.extend(as_rows(
                index.iter().map(|i| ((i >> bit) & 1) as f32).collect(),
            ));
            selectors.push(next);
            next += p.rows;
        }
        let mut valid = -1;
        if negative {
            extra.extend(as_rows(
                code.iter().map(|&c| f32::from(u8::from(c >= 0))).collect(),
            ));
            valid = next;
            next += p.rows;
        }
        let mut zero = -1;
        if p.zeros {
            extra.extend(as_rows(
                code.iter()
                    .map(|&c| f32::from(u8::from(c == ZERO)))
                    .collect(),
            ));
            zero = next;
            next += p.rows;
        }
        p.tree = Some((selectors, valid, zero));
    }
    pl.masks.extend(extra);
    pl
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn word_levels_finds_walks_and_refuses_the_rest() {
        assert_eq!(
            word_levels(&[0, 0, 1, 1, 2, 2], 6),
            Some((0, vec![(3, 1), (2, 0)]))
        );
        assert_eq!(
            word_levels(&[5, 6, 7, 9, 10, 11], 6),
            Some((5, vec![(2, 4), (3, 1)]))
        );
        assert_eq!(word_levels(&[3], 6), Some((3, vec![(1, 0)])));
        assert_eq!(word_levels(&[0, 1, 3], 6), None);
        let deep: Vec<Axis> = (0..7).map(|k| (2, 3i64.pow(k), 0)).collect();
        let seq = positions(&deep, 0, 1);
        assert!(word_levels(&seq, 6).is_none() && word_levels(&seq, 7).is_some());
    }

    #[test]
    fn compress_moves_each_lane_to_its_place() {
        for bb in [2, 4, 8] {
            for b in 0..bb {
                let mut lane: Vec<i64> = (0..16).collect();
                for (k, m) in compress(bb, b, 16).unwrap() {
                    let rolled: Vec<i64> = (0..16).map(|l| lane[((l + k) % 16) as usize]).collect();
                    lane = (0..16)
                        .map(|l| if m[l] { rolled[l] } else { lane[l] })
                        .collect();
                }
                let want: Vec<i64> = (0..16 / bb).map(|j| j * bb + b).collect();
                assert_eq!(lane[..(16 / bb) as usize], want[..]);
            }
        }
    }

    #[test]
    fn canon_merges_axes_both_walks_step_as_one() {
        assert_eq!(
            canon(&[(4, 8), (8, 1), (1, 5)], &[(4, 8), (8, 1), (1, 7)]),
            vec![(32, 1, 1)]
        );
        assert_eq!(
            canon(&[(28, 1), (1344, 28)], &[(28, 1344), (1344, 1)]),
            vec![(28, 1, 1344), (1344, 28, 1)]
        );
    }
}
