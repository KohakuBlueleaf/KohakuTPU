//! Buffers and views of them (DSL.SPEC, DSL.VIEW).

use super::{fail, Idx, Trace, R};
use ktpu_graph as g;

/// A port or scratch buffer: its name and spec.
#[derive(Clone, Debug)]
pub struct Buf {
    pub name: String,
    pub spec: g::Spec,
}

/// One index entry of a view.
#[derive(Clone, Debug)]
pub enum Ax {
    /// `:`
    All,
    /// `LO : LO + SIZE`
    Run(Idx, i64),
    /// One element, the axis dropped.
    At(Idx),
}

/// A region of a buffer; `whole` when it is the buffer itself (a walk
/// moves whole buffers only, DSL.MOV.010).
#[derive(Clone, Debug)]
pub struct View {
    pub buf: Buf,
    pub axes: Vec<Ax>,
    pub whole: bool,
}

impl Ax {
    /// `lo : hi` (DSL.VIEW.003, .008): the size a constant; with an int
    /// start inside the axis of `n`.
    fn span(lo: Idx, hi: Idx, n: i64) -> R<Ax> {
        let Some(size) = hi.sub(&lo).int() else {
            return fail("DSL.VIEW.008", "a slice of a symbolic size");
        };
        if let Some(l) = lo.int() {
            if l < 0 || l + size > n || size <= 0 {
                return fail(
                    "DSL.VIEW.008",
                    format!("[{l} : {}] is outside an axis of {n}", l + size),
                );
            }
        }
        Ok(Ax::Run(lo, size))
    }

    fn graph(&self) -> g::Axis {
        match self {
            Ax::All => g::Axis::All,
            Ax::Run(lo, n) => g::Axis::Run(lo.graph(), *n),
            Ax::At(i) => g::Axis::Idx(i.graph()),
        }
    }
}

/// A slice key: `lo..hi` or `..` (all).
#[derive(Clone, Debug)]
pub enum Key {
    All,
    Span(Idx, Idx),
    At(Idx),
}

/// `lo : hi`.
pub fn sl(lo: impl Into<Idx>, hi: impl Into<Idx>) -> Key {
    Key::Span(lo.into(), hi.into())
}

/// `:`.
pub const ALL: Key = Key::All;

/// One element.
pub fn at(i: impl Into<Idx>) -> Key {
    Key::At(i.into())
}

impl Buf {
    pub fn new(name: &str, spec: g::Spec) -> Buf {
        Buf {
            name: name.to_string(),
            spec,
        }
    }

    pub fn shape(&self) -> &[i64] {
        &self.spec.shape
    }

    pub fn tiled(&self) -> bool {
        self.spec.layout.iter().any(|(k, _)| k == "tile")
    }

    /// `buf[k0, k1, ..]` (DSL.VIEW.001).
    pub fn at(&self, keys: &[Key]) -> R<View> {
        if keys.len() > self.shape().len() {
            return fail(
                "DSL.VIEW.007",
                format!("{} indices for {}{:?}", keys.len(), self.name, self.shape()),
            );
        }
        let mut axes = vec![];
        for (k, &n) in keys.iter().zip(self.shape()) {
            axes.push(match k {
                Key::All => Ax::All,
                Key::Span(lo, hi) => Ax::span(lo.clone(), hi.clone(), n)?,
                Key::At(i) => Ax::At(i.clone()),
            });
        }
        Ok(View {
            buf: self.clone(),
            axes,
            whole: false,
        })
    }

    /// The whole buffer as a view.
    pub fn whole(&self) -> View {
        View {
            buf: self.clone(),
            axes: vec![Ax::All; self.shape().len()],
            whole: true,
        }
    }
}

impl View {
    /// `run` sizes and `all` extents, then the buffer's other axes
    /// (DSL.VIEW.005).
    pub fn shape(&self) -> Vec<i64> {
        let mut out = vec![];
        let dims = self.buf.shape();
        for (d, a) in self.axes.iter().enumerate() {
            match a {
                Ax::Run(_, n) => out.push(*n),
                Ax::All => out.push(dims[d]),
                Ax::At(_) => {}
            }
        }
        out.extend_from_slice(&dims[self.axes.len()..]);
        out
    }

    pub fn graph(&self) -> g::View {
        g::View {
            buffer: self.buf.name.clone(),
            axes: self.axes.iter().map(Ax::graph).collect(),
        }
    }
}

/// A spec as `TensorSpec.make` makes it: layout sorted by key, then
/// checked (DSL.SPEC.004, .011, .013).
pub fn spec(
    shape: &[i64],
    dtype: g::Dtype,
    layout: Vec<(String, g::LayoutValue)>,
    space: g::BufSpace,
) -> R<g::Spec> {
    let mut layout = layout;
    layout.sort_by(|a, b| a.0.cmp(&b.0));
    let keys: Vec<&str> = layout
        .iter()
        .map(|(k, _)| k.as_str())
        .filter(|k| *k != "t" && *k != "node")
        .collect();
    let want: Option<&[&str]> = match dtype {
        g::Dtype::Mx7a => Some(&["gm", "nk"]),
        g::Dtype::Mx7b => Some(&["gn", "nk"]),
        _ => None,
    };
    if let Some(want) = want {
        if keys != want {
            return fail(
                "DSL.SPEC.011",
                format!("{} wants layout {want:?}, got {keys:?}", dtype.name()),
            );
        }
    }
    if shape.iter().any(|&d| d <= 0) {
        return fail(
            "DSL.SPEC.004",
            format!("a buffer's extents are positive, got {shape:?}"),
        );
    }
    Ok(g::Spec {
        dtype,
        shape: shape.to_vec(),
        layout,
        space,
    })
}

/// An f16 row-major dram spec of `shape`.
pub fn f16(shape: &[i64]) -> R<g::Spec> {
    spec(shape, g::Dtype::F16, vec![], g::BufSpace::Dram)
}

impl Trace {
    /// Scratch memory (DSL.SPEC.008): `hN`, declared before the body.
    pub fn buffer(
        &mut self,
        shape: &[i64],
        dtype: g::Dtype,
        layout: Vec<(String, g::LayoutValue)>,
    ) -> R<Buf> {
        self.buffer_in(shape, dtype, layout, g::BufSpace::Dram)
    }

    /// Scratch memory in `space`.
    pub fn buffer_in(
        &mut self,
        shape: &[i64],
        dtype: g::Dtype,
        layout: Vec<(String, g::LayoutValue)>,
        space: g::BufSpace,
    ) -> R<Buf> {
        let s = spec(shape, dtype, layout, space)?;
        let n = self.fresh("h");
        self.scratch.push((n.clone(), s.clone()));
        Ok(Buf::new(&n, s))
    }
}
