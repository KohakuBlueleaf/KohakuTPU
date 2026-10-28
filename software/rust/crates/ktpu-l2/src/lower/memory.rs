//! Views of memory buffers as byte runs, in each buffer layout (L2.VIEW,
//! L2.MG.002, L2.MG.010).
//!
//! A view's axes, evaluated: `(start, size, kept)` each -- `I` is
//! `(I, 1, Some(false))`, `LO : +N` `(LO, N, Some(true))`, `:` the whole axis,
//! a new axis `*` `(0, 1, None)`.

use ktpu_l1::text::Term;

use crate::ir::repr::repr;
use crate::ir::Buffer;
use crate::mutant::on;
use crate::target::Target;

/// A view axis: start, size, and whether it is kept (None: a new axis).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ax {
    pub lo: i64,
    pub size: i64,
    pub kept: Option<bool>,
}

pub type ViewResult<T> = std::result::Result<T, String>;

/// Bytes of an MXFP7 entry and of a drained 4 x 4 f16 sub-tile.
pub fn entry(t: &Target) -> i64 {
    t.entry_words() * t.word_bytes
}

pub fn subtile(t: &Target) -> i64 {
    if on("l2.memory/subtile-16") {
        return 16;
    }
    t.word_bytes
}

fn int(t: &Term, name: &str) -> ViewResult<i64> {
    match t {
        Term::Int(v, _) => Ok(*v as i64),
        _ => Err(format!(
            "{name}: a bound is a constant here, got {}",
            repr(t)
        )),
    }
}

/// The axes of an evaluated view `name[slices]` over `shape`.
pub fn axes(name: &str, slices: &[Term], shape: &[i64]) -> ViewResult<Vec<Ax>> {
    let mut out = Vec::new();
    let mut d = 0usize;
    for s in slices {
        if let Term::NewAxis = s {
            out.push(Ax {
                lo: 0,
                size: 1,
                kept: None,
            });
            continue;
        }
        if d >= shape.len() {
            return Err(format!("{name} has {} axes", shape.len()));
        }
        let n = shape[d];
        let (lo, size, kept) = match s {
            Term::Int(v, _) => (*v as i64, 1, false),
            Term::Slice(lo, hi) => {
                let lo = match lo {
                    None => 0,
                    Some(x) => int(x, name)?,
                };
                let hi = match hi {
                    None => n,
                    Some(h) => match h.as_ref() {
                        Term::Pos(x) => lo + int(x, name)?,
                        other => int(other, name)?,
                    },
                };
                (lo, hi - lo, true)
            }
            _ => return Err(format!("{name}: an axis is I, LO : +N, LO : HI or :")),
        };
        if lo < 0 || size <= 0 || lo + size > n {
            return Err(format!("{name} axis {d}: [{lo}, {}) of {n}", lo + size));
        }
        out.push(Ax {
            lo,
            size,
            kept: Some(kept),
        });
        d += 1;
    }
    while d < shape.len() {
        out.push(Ax {
            lo: 0,
            size: shape[d],
            kept: Some(true),
        });
        d += 1;
    }
    if on("l2.memory/lo-ignored") {
        for a in out.iter_mut() {
            a.lo = 0;
        }
    }
    Ok(out)
}

/// The axes of an evaluated view term (a view, or a bare name: the whole).
pub fn axes_of(v: &Term, shape: &[i64]) -> ViewResult<Vec<Ax>> {
    match v {
        Term::View(n, s) => axes(n, s, shape),
        Term::Name(n) => axes(n, &[], shape),
        other => Err(format!("an axis view, got {}", repr(other))),
    }
}

/// The view's shape: kept axes and new ones.
pub fn shape_of(ax: &[Ax]) -> Vec<i64> {
    ax.iter()
        .filter(|a| a.kept != Some(false))
        .map(|a| a.size)
        .collect()
}

/// `(byte offset of the slab, [(start, size)] of its last two axes)`: the
/// leading axes must pick one slab (L2.VIEW.003).
pub fn slab(buf: &Buffer, ax: &[Ax], t: &Target) -> ViewResult<(i64, Vec<(i64, i64)>)> {
    let lead = &buf.shape[..buf.shape.len().saturating_sub(2)];
    let real: Vec<&Ax> = ax.iter().filter(|a| a.kept.is_some()).collect();
    let (mut off, mut stride) = (0, buf.nbytes(t));
    for (a, n) in real.iter().take(lead.len()).zip(lead) {
        if a.size != 1 {
            return Err("a view picks one slab of a batched buffer".into());
        }
        stride /= n;
        off += a.lo * stride;
    }
    Ok((
        off,
        real[lead.len()..].iter().map(|a| (a.lo, a.size)).collect(),
    ))
}

/// `[(byte offset, entries, first L1 entry)]` a FILL of the view reads, for an
/// `mx7a` / `mx7b` buffer packed tile-major: the view's K one packed chunk,
/// its rows whole lane groups; rows crossing a packed tile are two runs.
pub fn fill_runs(buf: &Buffer, ax: &[Ax], t: &Target) -> ViewResult<Vec<(i64, i64, i64)>> {
    if !buf.is_mx7() || buf.flag("t") {
        return Err(format!(
            "a fill reads an mx7a / mx7b buffer, not {}",
            buf.dtype
        ));
    }
    let g = buf.get(if buf.dtype == "mx7a" { "gm" } else { "gn" });
    let nk = buf.get("nk");
    let (base, two) = slab(buf, ax, t)?;
    let ((r0, rows), (k0, k)) = (two[0], two[1]);
    let kt = *buf.shape.last().unwrap();
    let kblock = if on("l2.memory/kblock-16") {
        16
    } else {
        t.kblock
    };
    let chunk = nk * kblock;
    if k != chunk || k0 % chunk != 0 {
        return Err(format!(
            "a fill's K is one packed chunk ({chunk} at a multiple of {chunk}); got [{k0}, {})",
            k0 + k
        ));
    }
    let lanes = t.mg_lanes;
    if r0 % lanes != 0 || rows % lanes != 0 {
        return Err(format!("a fill's rows are whole groups of {lanes}"));
    }
    let chunks = kt / chunk;
    let c = k0 / chunk;
    let (mut lo, hi) = (r0 / lanes, (r0 + rows) / lanes);
    let mut out = Vec::new();
    let mut eoff = 0;
    while lo < hi {
        let tt = lo / g;
        let top = hi.min((tt + 1) * g);
        let first = ((tt * chunks + c) * g + (lo - tt * g)) * nk;
        let n = (top - lo) * nk;
        out.push((base + first * entry(t), n, eoff));
        eoff += n;
        lo = top;
    }
    Ok(out)
}

/// Byte offset of 4 x 4 sub-tile (r, c) of an f16 `tile=` buffer.
pub fn subtile_address(buf: &Buffer, r: i64, c: i64, t: &Target) -> i64 {
    let (gm, gn) = buf.tile().unwrap();
    let n = *buf.shape.last().unwrap();
    let tn = n / (4 * gn);
    let (ti, tj) = (r / gm, c / gn);
    ((ti * tn + tj) * gm * gn + (r % gm) * gn + c % gn) * subtile(t)
}

/// Byte offset of a drain of the view: an `f16 @dram(tile=..)` region whose
/// sub-tiles, row-major over the view, are consecutive words.
pub fn drain_base(buf: &Buffer, ax: &[Ax], t: &Target) -> ViewResult<i64> {
    if buf.dtype != "f16" || !buf.has("tile") {
        return Err("a drain to memory writes an f16 buffer with a tile= layout".into());
    }
    let (base, two) = slab(buf, ax, t)?;
    let ((r0, rows), (c0, cols)) = (two[0], two[1]);
    if r0 % 4 != 0 || c0 % 4 != 0 || rows % 4 != 0 || cols % 4 != 0 {
        return Err("a drained view is whole 4x4 sub-tiles".into());
    }
    let (sr, sc, nr, nc) = (r0 / 4, c0 / 4, rows / 4, cols / 4);
    let first = subtile_address(buf, sr, sc, t);
    for r in 0..nr {
        for c in [0, nc - 1] {
            let want = first + (r * nc + c) * subtile(t);
            if subtile_address(buf, sr + r, sc + c, t) != want {
                return Err(format!(
                    "rows [{r0}, {}) x cols [{c0}, {}) are not one run in tile={}: a drain \
                     writes its sub-tiles row-major, one after another",
                    r0 + rows,
                    c0 + cols,
                    buf.layout["tile"].py()
                ));
            }
        }
    }
    Ok(base + first)
}

/// `(byte offset, [(count, stride)] outermost first, element bytes)`.
pub type Walk = (i64, Vec<(i64, i64)>, i64);

/// The [`Walk`] of a row-major view; `node` selects storage, not element order.
pub fn rowmajor_walk(buf: &Buffer, ax: &[Ax]) -> ViewResult<Walk> {
    if buf.layout.keys().any(|key| key != "node") {
        let keys: Vec<String> = buf
            .layout
            .iter()
            .map(|(k, v)| format!("'{k}': {}", v.py()))
            .collect();
        return Err(format!(
            "{} with layout {{{}}} is not row-major",
            buf.dtype,
            keys.join(", ")
        ));
    }
    let item = buf.item();
    let mut strides = vec![0; buf.shape.len()];
    let mut s = item;
    for (i, n) in buf.shape.iter().enumerate().rev() {
        strides[i] = s;
        s *= n;
    }
    let (mut off, mut dims) = (0, Vec::new());
    let real: Vec<&Ax> = ax.iter().filter(|a| a.kept.is_some()).collect();
    for (a, st) in real.iter().zip(&strides) {
        off += a.lo * st;
        if a.size > 1 {
            dims.push((a.size, *st));
        }
    }
    Ok((off, dims, item))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ktpu_l1::text::parse;

    fn tgt() -> Target {
        Target::of(&ktpu_machine::Machine::named("multimesh_v9t6").unwrap())
    }

    fn buf(text: &str) -> Buffer {
        let st = parse(&format!("x = y : {text}\n"), "<t>")
            .unwrap()
            .remove(0);
        Buffer::of(st.annot.as_ref().unwrap()).unwrap()
    }

    fn view(text: &str) -> Term {
        parse(&format!("f {text}\n"), "<t>")
            .unwrap()
            .remove(0)
            .args
            .remove(0)
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_VIEW_001_runs_and_wholes() {
        let ax = axes_of(&view("x[4 : +8, :]"), &[64, 128]).unwrap();
        assert_eq!(shape_of(&ax), vec![8, 128]);
        assert_eq!(
            ax[0],
            Ax {
                lo: 4,
                size: 8,
                kept: Some(true)
            }
        );
        let ax = axes_of(&view("x[3]"), &[64, 128]).unwrap();
        assert_eq!(shape_of(&ax), vec![128]);
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_VIEW_002_refusals() {
        assert_eq!(
            axes_of(&view("x[60 : +8, :]"), &[64, 128]).unwrap_err(),
            "x axis 0: [60, 68) of 64"
        );
        assert_eq!(
            axes_of(&view("x[1, 2, 3]"), &[64, 128]).unwrap_err(),
            "x has 2 axes"
        );
    }

    #[test]
    fn rowmajor_node_is_placement() {
        let b = buf("f16[32, 32] @stage(node=1)");
        let ax = axes_of(&view("x[4 : +4, :]"), &b.shape).unwrap();
        assert_eq!(
            rowmajor_walk(&b, &ax).unwrap(),
            (256, vec![(4, 64), (32, 2)], 2)
        );
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_MG_002_fill_runs() {
        let t = tgt();
        let b = buf("mx7a[256, 256] @dram(gm=16, nk=8)");
        let ax = axes_of(&view("x[64 : +64, :]"), &b.shape).unwrap();
        // Rows 64.. are packed tile 1: 16 groups x 8 entries in.
        assert_eq!(
            fill_runs(&b, &ax, &t).unwrap(),
            vec![(16 * 8 * 128, 128, 0)]
        );
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_MG_011_drain_refusals() {
        let t = tgt();
        let b = buf("f16[64, 64] @dram(tile=4x4)");
        let ax = axes_of(&view("y[2 : +4, :]"), &b.shape).unwrap();
        assert_eq!(
            drain_base(&b, &ax, &t).unwrap_err(),
            "a drained view is whole 4x4 sub-tiles"
        );
        let ax = axes_of(&view("y[0 : +4, 8 : +16]"), &b.shape).unwrap();
        assert!(drain_base(&b, &ax, &t)
            .unwrap_err()
            .contains("are not one run in tile=(4, 4)"));
    }
}
