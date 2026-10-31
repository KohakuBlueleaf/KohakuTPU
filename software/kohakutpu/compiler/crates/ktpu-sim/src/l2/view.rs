//! Buffers and views: what element or word of memory an L2 name reads
//! (L2.SPACE, L2.VIEW, L2.MG.002, L2.MG.010).

use ktpu_l1::text::{fmt, Term};

use crate::l1::term::{int, Env};
use crate::units::mover::{Level, Walk};
use crate::R;

pub const WORD: i64 = 32;
const ENTRY: i64 = 128;
/// Rows a lane group, values a scale block (`mg.lanes`, `mg.kblock`).
const LANES: i64 = 4;
const KBLOCK: i64 = 32;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Layout {
    Rows,
    /// Row-major tiles of GM x GN 4 x 4 sub-tiles.
    Tile(i64, i64),
    /// MXFP7 packed tile-major; true the B packing.
    Mx7 {
        b: bool,
        g: i64,
        nk: i64,
    },
}

#[derive(Clone, Debug)]
pub struct Buf {
    pub at: i64,
    pub dtype: String,
    pub shape: Vec<i64>,
    pub layout: Layout,
}

fn layout_key(args: &[Term], key: &str) -> Option<Vec<i64>> {
    args.iter().find_map(|a| match a {
        Term::Kw(k, v) if k == key => match v.as_ref() {
            Term::Int(i, _) => Some(vec![*i as i64]),
            Term::Dims(d) => Some(d.iter().map(|x| *x as i64).collect()),
            _ => None,
        },
        _ => None,
    })
}

impl Buf {
    /// The buffer a type term names, at `at`.
    pub fn of(t: &Term, at: i64) -> R<Buf> {
        let (ty, place) = match t {
            Term::Placed(x, p) => (x.as_ref(), Some(p.as_ref())),
            other => (other, None),
        };
        let Term::View(dtype, dims) = ty else {
            return Err(format!("a buffer's type, got {}", fmt(t)));
        };
        let shape: Vec<i64> = dims
            .iter()
            .map(|d| d.as_int().unwrap_or(1) as i64)
            .collect();
        let args: &[Term] = match place {
            Some(Term::Call(_, args)) => args,
            _ => &[],
        };
        let one = |k: &str| layout_key(args, k).map(|v| v[0]);
        let layout = match dtype.as_str() {
            "mx7a" | "mx7b" => {
                let b = dtype == "mx7b";
                let g = one(if b { "gn" } else { "gm" }).ok_or("an MXFP7 buffer's group")?;
                Layout::Mx7 {
                    b,
                    g,
                    nk: one("nk").ok_or("an MXFP7 buffer's nk")?,
                }
            }
            _ => match layout_key(args, "tile") {
                Some(d) => Layout::Tile(d[0], d[1]),
                None => Layout::Rows,
            },
        };
        Ok(Buf {
            at,
            dtype: dtype.clone(),
            shape,
            layout,
        })
    }

    pub fn item(&self) -> i64 {
        match self.dtype.as_str() {
            "f32" | "u32" | "i32" => 4,
            "u64" => 8,
            "u8" => 1,
            _ => 2,
        }
    }

    pub fn bytes(&self) -> i64 {
        let n: i64 = self.shape.iter().product();
        match self.layout {
            Layout::Mx7 { .. } => {
                let (m, k) = (self.shape[0], self.shape[1]);
                m / LANES * (k / KBLOCK) * ENTRY
            }
            _ => n * self.item(),
        }
    }

    /// Bytes between rows of a row-major rank-2 buffer; a rank-1 buffer's
    /// rows are its items.
    pub fn pitch(&self) -> i64 {
        self.shape.last().map_or(0, |c| {
            if self.shape.len() > 1 {
                c * self.item()
            } else {
                self.item()
            }
        })
    }

    /// Element (r, c)'s byte address.
    pub fn element(&self, r: i64, c: i64) -> R<i64> {
        match self.layout {
            Layout::Rows if self.shape.len() == 1 => Ok(self.at + (r + c) * self.item()),
            Layout::Rows => Ok(self.at + (r * self.shape[1] + c) * self.item()),
            Layout::Tile(gm, gn) => {
                let n = self.shape[1];
                let (sr, sc) = (r / LANES, c / LANES);
                let tn = n / (LANES * gn);
                let (ti, tj) = (sr / gm, sc / gn);
                let sub = (ti * tn + tj) * gm * gn + (sr % gm) * gn + sc % gn;
                Ok(self.at + sub * WORD + (r % LANES * LANES + c % LANES) * self.item())
            }
            Layout::Mx7 { .. } => Err("an MXFP7 buffer has no elements".into()),
        }
    }

    /// Entry (group `g0`, block `k0`) of an MXFP7 buffer: tiles of G groups.
    pub fn entry(&self, g0: i64, k0: i64) -> R<i64> {
        let Layout::Mx7 { g, nk, .. } = self.layout else {
            return Err("a fill reads an MXFP7 buffer".into());
        };
        let chunk = nk * KBLOCK;
        let chunks = self.shape[1] / chunk;
        let (c, block) = (k0 * KBLOCK / chunk, k0 % nk);
        let t = g0 / g;
        Ok(self.at + (((t * chunks + c) * g + g0 % g) * nk + block) * ENTRY)
    }
}

/// A view's axes: (start, size), the index-axes `kept` false.
#[derive(Clone, Debug)]
pub struct View {
    pub name: String,
    pub axes: Vec<(i64, i64)>,
    pub kept: Vec<bool>,
}

fn axis(s: &Term, n: i64, env: &Env) -> R<(i64, i64, bool)> {
    Ok(match s {
        Term::Slice(lo, hi) => {
            let lo = lo.as_ref().map_or(Ok(0), |x| int(x, env))?;
            let hi = match hi.as_ref().map(|h| h.as_ref()) {
                None => n,
                Some(Term::Pos(x)) => lo + int(x, env)?,
                Some(other) => int(other, env)?,
            };
            (lo, hi - lo, true)
        }
        other => (int(other, env)?, 1, false),
    })
}

/// A term naming a buffer's view, evaluated.
pub fn view(t: &Term, shape: impl Fn(&str) -> R<Vec<i64>>, env: &Env) -> R<View> {
    let (name, slices): (&str, &[Term]) = match t {
        Term::View(n, s) => (n, s),
        Term::Name(n) => (n, &[]),
        other => return Err(format!("a view, got {}", fmt(other))),
    };
    let dims = shape(name)?;
    let (mut axes, mut kept) = (Vec::new(), Vec::new());
    for (d, n) in dims.iter().enumerate() {
        let (lo, size, keep) = match slices.get(d) {
            Some(s) => axis(s, *n, env)?,
            None => (0, *n, true),
        };
        if lo < 0 || size <= 0 || lo + size > *n {
            return Err(format!("{name} axis {d}: [{lo}, {}) of {n}", lo + size));
        }
        axes.push((lo, size));
        kept.push(keep);
    }
    Ok(View {
        name: name.to_string(),
        axes,
        kept,
    })
}

impl View {
    /// (first row, rows, first column, columns) of a rank-1 or -2 view.
    pub fn rect(&self) -> (i64, i64, i64, i64) {
        match self.axes[..] {
            [(c0, c)] => (0, 1, c0, c),
            [(r0, r), (c0, c)] => (r0, r, c0, c),
            _ => (0, 0, 0, 0),
        }
    }

    /// The walk of a row-major view whose rows are whole words.
    pub fn walk(&self, b: &Buf) -> R<Walk> {
        let (r0, rows, c0, cols) = self.rect();
        let row_major = b.layout == Layout::Rows;
        if !row_major {
            return Err(format!("a walk of {} in {:?}", self.name, b.layout));
        }
        let start = b.element(r0, c0)?;
        let word_cols = WORD / b.item();
        let (rows, cols) = match b.shape.len() {
            1 => (cols / word_cols, word_cols),
            _ => (rows, cols),
        };
        if (cols * b.item()) % WORD != 0 || (start - b.at) % WORD != 0 {
            return Err(format!("{}: a walk steps whole 32-byte words", self.name));
        }
        let level = |count, stride| Level {
            count,
            stride,
            axis: 0,
            astep: 0,
        };
        let pitch = b.pitch().max(WORD);
        Ok(Walk {
            base: start,
            levels: vec![level(rows, pitch), level(cols * b.item() / WORD, WORD)],
            windows: [(0, 0); 2],
        })
    }
}

/// `((count, stride), ...)` at `base`, the level's words as they are written.
pub fn walk_term(t: &Term, base: i64, env: &Env) -> R<Walk> {
    let Term::Tuple(items) = t else {
        return Err(format!("a walk, got {}", fmt(t)));
    };
    let mut levels = Vec::new();
    for item in items {
        let (count, stride) = crate::l1::term::pair(item, env)?;
        levels.push(Level {
            count,
            stride,
            axis: 0,
            astep: 0,
        });
    }
    Ok(Walk {
        base,
        levels,
        windows: [(0, 0); 2],
    })
}
