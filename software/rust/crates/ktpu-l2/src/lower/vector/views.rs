//! Load and store views: a view's byte offset, and the walks of `tile=` and
//! MXFP7 buffers.

#[cfg(any(test, feature = "witness"))]
use std::fmt;

use super::*;

#[cfg(test)]
mod tests;

/// Descriptor dimensions in order: (byte stride, count).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Walk(pub Vec<(i64, i64)>);

impl Walk {
    pub fn flat(stride: i64, count: i64) -> Self {
        Self(vec![(stride, count)])
    }
}

/// The descriptor's walk text inside its outer parentheses.
#[cfg(any(test, feature = "witness"))]
impl fmt::Display for Walk {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let [(s, n)] = self.0.as_slice() {
            return write!(f, "{s}, {n}");
        }
        for (i, (s, n)) in self.0.iter().enumerate() {
            if i != 0 {
                f.write_str(", ")?;
            }
            write!(f, "({s}, {n})")?;
        }
        Ok(())
    }
}

/// The byte offset a load or store view starts at in its buffer.
pub fn view_offset(
    body: &Body,
    st: &ktpu_l1::text::Stmt,
    view: &Term,
    env: &Env,
    t: &Target,
) -> VResult<i64> {
    let v = match value(body, st, view, env)? {
        Term::Name(n) => Term::View(n, vec![]),
        other => other,
    };
    let name = value_name(&v).unwrap_or("").to_string();
    let Some(buf) = body.buffers.get(&name) else {
        return Err(VErr::Value(format!("KeyError: '{name}'")));
    };
    let ax = M::axes_of(&v, &buf.shape).map_err(VErr::Value)?;
    if buf.has("tile") {
        return Ok(tiled(buf, &ax, t).map_err(VErr::Value)?.0);
    }
    if buf.is_mx7() {
        return Ok(mx7(buf, &ax, t).map_err(VErr::Value)?.0);
    }
    Ok(M::rowmajor_walk(buf, &ax).map_err(VErr::Value)?.0)
}

/// `(byte offset, walk dimensions, words)` of a view of a `tile=` buffer: R/4
/// bands of C/16 consecutive sub-tiles, the bands equally spaced.
pub fn tiled(buf: &crate::ir::Buffer, ax: &[M::Ax], t: &Target) -> M::ViewResult<(i64, Walk, i64)> {
    let (base, two) = M::slab(buf, ax, t)?;
    let ((r0, rows), (c0, cols)) = (two[0], two[1]);
    if r0 % 4 != 0 || c0 % 16 != 0 || rows % 4 != 0 || cols % 32 != 0 {
        return Err("a GT4 view is bands of 4 rows by 32 columns".into());
    }
    let (sr, sc) = (r0 / 4, c0 / 4);
    let sub = M::subtile(t);
    let first = M::subtile_address(buf, sr, sc, t);
    let per = cols / 4;
    if M::subtile_address(buf, sr, sc + per - 1, t) != first + (per - 1) * sub {
        return Err("a band's sub-tiles are one run: the columns stay in one tile".into());
    }
    let bands = rows / 4;
    let stride = if bands > 1 {
        M::subtile_address(buf, sr + 1, sc, t) - first
    } else {
        0
    };
    for b in 0..bands {
        if M::subtile_address(buf, sr + b, sc, t) != first + b * stride {
            return Err("the view's bands are not equally spaced (rows cross a tile)".into());
        }
    }
    let walk = if bands == 1 {
        Walk::flat(32, per)
    } else {
        Walk(vec![(32, per), (stride, bands)])
    };
    Ok((base + first, walk, per * bands))
}

/// `(byte offset, walk dimensions, words)` of a view of an MXFP7 buffer packed
/// tile-major: bands of 4 rows, each band's blocks in K order.
pub fn mx7(buf: &crate::ir::Buffer, ax: &[M::Ax], t: &Target) -> M::ViewResult<(i64, Walk, i64)> {
    let g = buf.get(if buf.dtype == "mx7a" { "gm" } else { "gn" });
    let nk = buf.get("nk");
    let (base, two) = M::slab(buf, ax, t)?;
    let ((r0, rows), (k0, cols)) = (two[0], two[1]);
    let chunk = nk * 32;
    if r0 % 4 != 0 || rows % 4 != 0 || k0 % chunk != 0 || cols % chunk != 0 {
        return Err(format!(
            "an MXFP7 store is bands of 4 rows by whole chunks of {chunk}"
        ));
    }
    let (g0, bands) = (r0 / 4, rows / 4);
    if g0 / g != (g0 + bands - 1) / g {
        return Err(format!("the rows cross a packed tile of {} rows", 4 * g));
    }
    let chunks = buf.shape.last().unwrap() / chunk;
    let (tt, c0) = (g0 / g, k0 / chunk);
    let e = M::entry(t);
    let first = ((tt * chunks + c0) * g + (g0 - tt * g)) * nk * e;
    let mut dims = vec![(32, 4 * nk)];
    if cols > chunk {
        dims.push((g * nk * e, cols / chunk));
    }
    if bands > 1 {
        dims.push((nk * e, bands));
    }
    let walk = Walk(dims);
    Ok((base + first, walk, 4 * nk * (cols / chunk) * bands))
}

pub fn contiguous(dims: &[(i64, i64)], item: i64) -> bool {
    let mut want = item;
    for &(n, stride) in dims.iter().rev() {
        if stride != want {
            return false;
        }
        want *= n;
    }
    true
}
