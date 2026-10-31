//! An L1 parameter's memory layout, from its type (L1.BODY.002, .006):
//! `DTYPE[dims] @SPACE[(LAYOUT)]`.
//!
//! ```text
//! f16[1024, 128] @dram                    row-major fp16
//! mx7a[1024, 1024] @dram(gm=64, nk=2)     MXFP7, the clusters' A packing
//! mx7b[1024, 1024] @dram(gn=64, nk=2)     MXFP7, B packing
//! f16[1024, 1024] @dram(tile=64x64)       drained sub-tiles
//! f16[256, 128] @dram(tile=1x32, ieee=1)  the same tiling, IEEE fp16 values
//! mx7b[64, 1024] @dram(gn=16, nk=2, t=1)  the transpose of the host's value
//! ```

use std::collections::BTreeMap;

use ktpu_machine::Machine;

use crate::error::{Error, Result};
use crate::layout::tensor::{f16_saturating, fp16_value as cluster_f16, ieee_f16, Mx7};
use crate::text::{fmt, Term};

/// Element bytes of each plain dtype.
pub const DTYPES: [(&str, u64); 6] = [
    ("f16", 2),
    ("f32", 4),
    ("u8", 1),
    ("u32", 4),
    ("i32", 4),
    ("u64", 8),
];

/// A layout value: an integer or dims (`tile=64x64`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LayoutVal {
    Int(i128),
    Dims(Vec<i128>),
}

impl LayoutVal {
    pub fn int(&self) -> Option<i128> {
        match self {
            LayoutVal::Int(v) => Some(*v),
            LayoutVal::Dims(_) => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Buffer {
    pub dtype: String,
    pub shape: Vec<i128>,
    pub space: String,
    pub layout: BTreeMap<String, LayoutVal>,
}

fn bad(msg: impl Into<String>) -> Error {
    Error::new("L1.BODY.002", msg)
}

/// One little-endian value of a plain dtype as float64.
fn scalar(dtype: &str, b: &[u8]) -> f64 {
    let le = |n: usize| {
        let mut a = [0u8; 8];
        a[..n].copy_from_slice(&b[..n]);
        u64::from_le_bytes(a)
    };
    match dtype {
        "f16" => ieee_f16(le(2) as u16),
        "f32" => f32::from_bits(le(4) as u32) as f64,
        "u8" => le(1) as f64,
        "u32" => le(4) as f64,
        "i32" => le(4) as u32 as i32 as f64,
        _ => le(8) as f64,
    }
}

impl Buffer {
    /// The buffer a parameter's type names.
    pub fn of(t: &Term) -> Result<Buffer> {
        let mut space = "dram".to_string();
        let mut layout = BTreeMap::new();
        let mut ty = t;
        if let Term::Placed(x, at) = t {
            ty = x;
            let at = match at.as_ref() {
                Term::Call(name, args) => {
                    for a in args {
                        let Term::Kw(k, v) = a else {
                            return Err(bad(format!("a layout is NAME=VALUE, got {}", fmt(a))));
                        };
                        let v = match v.as_ref() {
                            Term::Dims(d) => LayoutVal::Dims(d.clone()),
                            Term::Int(i, _) => LayoutVal::Int(*i),
                            other => {
                                return Err(bad(format!(
                                    "a layout value is a number, got {}",
                                    fmt(other)
                                )))
                            }
                        };
                        layout.insert(k.clone(), v);
                    }
                    name.clone()
                }
                Term::Name(n) => n.clone(),
                other => return Err(bad(format!("a space is a name, got {}", fmt(other)))),
            };
            space = at;
        }
        let (dtype, shape) = match ty {
            Term::View(name, dims) if dims.iter().all(|d| matches!(d, Term::Int(..))) => (
                name.clone(),
                dims.iter().map(|d| d.as_int().unwrap()).collect(),
            ),
            _ => {
                return Err(bad(format!(
                    "an L1 buffer is DTYPE[constant dims], got {}",
                    fmt(ty)
                )))
            }
        };
        let b = Buffer {
            dtype,
            shape,
            space,
            layout,
        };
        b.check()?;
        Ok(b)
    }

    pub fn check(&self) -> Result<()> {
        let keys: Vec<&str> = self.layout.keys().map(String::as_str).collect();
        let want: Option<[&str; 2]> = match self.dtype.as_str() {
            "mx7a" => Some(["gm", "nk"]),
            "mx7b" => Some(["gn", "nk"]),
            _ => None,
        };
        if let Some(w) = want.filter(|_| !crate::mutant::on("num.buffer/no-key-check")) {
            let mut have: Vec<&str> = keys
                .iter()
                .copied()
                .filter(|k| *k != "t" && *k != "node")
                .collect();
            have.sort();
            let mut w = w.to_vec();
            w.sort();
            if have != w {
                return Err(bad(format!(
                    "{} wants @dram({}=...)",
                    self.dtype,
                    w.join(", ")
                )));
            }
        }
        let known = ["mx7a", "mx7b"].contains(&self.dtype.as_str())
            || DTYPES.iter().any(|(d, _)| *d == self.dtype);
        if !known {
            if crate::mutant::on("l1.body/wrong-reason") {
                return Err(bad(format!("layout keys: {keys:?}")));
            }
            return Err(bad(format!("no buffer dtype {}", self.dtype)));
        }
        let allowed = ["gm", "gn", "nk", "tile", "ieee", "t", "node"];
        if keys.iter().any(|k| !allowed.contains(k)) {
            return Err(bad(format!(
                "layout keys: gm gn nk tile ieee t node; got {keys:?}"
            )));
        }
        if self.layout.contains_key("ieee") && !self.layout.contains_key("tile") {
            return Err(bad("ieee= reads a tile= layout's values as IEEE fp16"));
        }
        if self.layout.contains_key("node") && self.space != "stage" {
            return Err(bad("node= places a buffer in that node's staging: @stage"));
        }
        if keys.iter().any(|k| *k != "node") && self.shape.len() < 2 {
            return Err(bad(format!("a {keys:?} layout is of [.., M, N] slabs")));
        }
        Ok(())
    }

    /// The host value's shape: the buffer's, each slab transposed under `t=1`.
    pub fn host_shape(&self) -> Vec<i128> {
        let mut s = self.shape.clone();
        if self.layout.get("t").and_then(LayoutVal::int).unwrap_or(0) != 0 {
            let n = s.len();
            s.swap(n - 2, n - 1);
        }
        s
    }

    /// Bytes the buffer holds on `m`: an MXFP7 buffer one entry
    /// (`Mx7::entry_bytes`) per `lanes x kblock` values, whole entries.
    pub fn bytes(&self, m: &Machine) -> Result<u64> {
        if crate::mutant::on("l1.body/nbytes") {
            return Ok(0);
        }
        let n: i128 = self.shape.iter().product();
        if !self.dtype.starts_with("mx7") {
            let item = DTYPES.iter().find(|(d, _)| *d == self.dtype).unwrap().1;
            return Ok(n as u64 * item);
        }
        if crate::mutant::on("num.buffer/mx7-two-bytes") {
            return Ok(2 * n as u64);
        }
        let f = match crate::mutant::on("l1.facts/mx7-byte") {
            true => return Ok(n as u64),
            false => Mx7::of(m)?,
        };
        let values = (f.lanes * f.kblock) as u64;
        Ok((n as u64).div_ceil(values) * f.entry_bytes() as u64)
    }

    /// What the host writes for an FP16-valued buffer (f16, mx7a, mx7b):
    /// `x` the host value's FP16 bits in `host_shape` order; each `[M, N]`
    /// slab transposed under `t=1`, then packed by the layout (MXFP7 as
    /// `machine`'s entry, `tensor::Mx7`).
    pub fn pack(&self, x: &[u16], machine: &Machine) -> Result<Vec<u8>> {
        if self.dtype != "f16" && !self.dtype.starts_with("mx7") {
            return Err(bad(format!("{} is not an FP16-valued buffer", self.dtype)));
        }
        let shape: Vec<usize> = self.shape.iter().map(|d| *d as usize).collect();
        let n = shape.len();
        let (m, k) = if n >= 2 {
            (shape[n - 2], shape[n - 1])
        } else {
            (1, shape[0])
        };
        let total: usize = shape.iter().product();
        if x.len() != total {
            return Err(bad(format!("{} values for a buffer of {total}", x.len())));
        }
        let t = self.layout.get("t").and_then(LayoutVal::int).unwrap_or(0) != 0;
        let mut v: Vec<u16> = x.to_vec();
        if t {
            for s in v.chunks_mut(m * k) {
                let src = s.to_vec();
                for i in 0..m {
                    for j in 0..k {
                        s[i * k + j] = src[j * m + i];
                    }
                }
            }
        }
        let int = |key: &str| self.layout.get(key).and_then(LayoutVal::int).unwrap_or(0) as usize;
        let f = match self.dtype.starts_with("mx7") {
            true => Some(Mx7::of(machine)?),
            false => None,
        };
        let one = |s: &[u16]| -> Result<Vec<u8>> {
            match (self.dtype.as_str(), &f) {
                ("mx7a", Some(f)) => super::tensor::pack_a(s, m, k, int("gm"), int("nk"), f),
                ("mx7b", Some(f)) => super::tensor::pack_b(s, m, k, int("gn"), int("nk"), f),
                _ => {
                    let bytes = |h: &u16| match crate::mutant::on("num.buffer/f16-big-endian") {
                        true => h.to_be_bytes(),
                        false => h.to_le_bytes(),
                    };
                    Ok(match self.tiled(s) {
                        Some(t) => t.iter().flat_map(bytes).collect(),
                        None => s.iter().flat_map(bytes).collect(),
                    })
                }
            }
        };
        let slabbed = n > 2 && self.layout.keys().any(|k| k != "t" && k != "node");
        if !slabbed {
            return one(&v);
        }
        let mut out = Vec::new();
        for s in v.chunks(m * k) {
            out.extend(one(s)?);
        }
        Ok(out)
    }

    /// What the host writes for float `values` (`host_shape` order): an
    /// FP16-valued buffer's values as fp16 saturating at ±65504 (RUL.057),
    /// an f32 buffer's as binary32, then packed as [`Buffer::pack_host`].
    pub fn pack_values(&self, values: &[f64], machine: &Machine) -> Result<Vec<u8>> {
        let f16 = |v: &[f64]| -> Vec<u8> {
            v.iter()
                .flat_map(|x| f16_saturating(*x).to_le_bytes())
                .collect()
        };
        let host = match self.host_dtype() {
            "f16" => f16(values),
            "f32" if crate::mutant::on("num.buffer/f32-as-f16") => {
                return Ok(f16(values));
            }
            "f32" => values
                .iter()
                .flat_map(|x| (*x as f32).to_le_bytes())
                .collect(),
            other => return Err(bad(format!("{other} is not a float buffer"))),
        };
        self.pack_host(&host, machine)
    }

    /// The dtype of the host's values before packing: f16 for MXFP7.
    pub fn host_dtype(&self) -> &str {
        match self.dtype.starts_with("mx7") {
            true => "f16",
            false => &self.dtype,
        }
    }

    /// Bytes of one host value.
    fn host_item(&self) -> usize {
        DTYPES
            .iter()
            .find(|(d, _)| *d == self.host_dtype())
            .unwrap()
            .1 as usize
    }

    /// The last two dims `(M, N)` of a slab (`(1, N)` for one dim).
    fn slab(&self) -> (usize, usize) {
        let s: Vec<usize> = self.shape.iter().map(|d| *d as usize).collect();
        match s.len() {
            0 => (1, 1),
            1 => (1, s[0]),
            n => (s[n - 2], s[n - 1]),
        }
    }

    fn transposed(&self) -> bool {
        self.layout.get("t").and_then(LayoutVal::int).unwrap_or(0) != 0
    }

    /// What the host writes for any buffer: `host` the host value's bytes
    /// (`host_dtype`, little-endian, `host_shape` order). Each `[M, N]` slab
    /// is transposed under `t=1`, then packed by the layout: MXFP7 as
    /// `machine`'s entry, `tile=` sub-tiled, else row-major as given.
    pub fn pack_host(&self, host: &[u8], machine: &Machine) -> Result<Vec<u8>> {
        let item = self.host_item();
        let total: usize = self.shape.iter().product::<i128>() as usize;
        if host.len() != total * item {
            return Err(bad(format!(
                "{} bytes for a buffer of {total} {} values",
                host.len(),
                self.host_dtype()
            )));
        }
        if self.host_dtype() == "f16" {
            let x: Vec<u16> = host
                .as_chunks::<2>()
                .0
                .iter()
                .map(|&b| u16::from_le_bytes(b))
                .collect();
            return self.pack(&x, machine);
        }
        let (m, n) = self.slab();
        let mut v = host.to_vec();
        if self.transposed() {
            for s in v.chunks_mut(m * n * item) {
                let src = s.to_vec();
                for i in 0..m {
                    for j in 0..n {
                        let (to, from) = ((i * n + j) * item, (j * m + i) * item);
                        s[to..to + item].copy_from_slice(&src[from..from + item]);
                    }
                }
            }
        }
        let Some((gm, gn)) = self.tile() else {
            return Ok(v);
        };
        let (gm, gn) = (gm as usize, gn as usize);
        let mut out = Vec::with_capacity(v.len());
        for s in v.chunks(m * n * item) {
            for ti in 0..m / (4 * gm) {
                for tj in 0..n / (4 * gn) {
                    for a in 0..gm {
                        for b in 0..gn {
                            for i in 0..4 {
                                for j in 0..4 {
                                    let at = (((ti * gm + a) * 4 + i) * n + (tj * gn + b) * 4 + j)
                                        * item;
                                    out.extend_from_slice(&s[at..at + item]);
                                }
                            }
                        }
                    }
                }
            }
        }
        Ok(out)
    }

    /// What the host reads back: `raw` the buffer's bytes as float64 values
    /// in `host_shape` order. A `tile=` layout's values are the clusters'
    /// fp16 (`tensor::fp16_value`), or IEEE fp16 with `ieee=1`. An MXFP7
    /// buffer is read through its fp16 source, not here.
    pub fn unpack(&self, raw: &[u8]) -> Result<Vec<f64>> {
        if self.dtype.starts_with("mx7") {
            return Err(bad("an MXFP7 buffer is read back through its fp16 source"));
        }
        let total: usize = self.shape.iter().product::<i128>() as usize;
        let item = self.host_item();
        if raw.len() != total * item {
            return Err(bad(format!(
                "{} bytes for a buffer of {total} {} values",
                raw.len(),
                self.dtype
            )));
        }
        let ieee = self
            .layout
            .get("ieee")
            .and_then(LayoutVal::int)
            .unwrap_or(0)
            != 0;
        let mut v: Vec<f64> = match self.tile() {
            Some(_) => {
                let h: Vec<u16> = raw
                    .as_chunks::<2>()
                    .0
                    .iter()
                    .map(|&b| u16::from_le_bytes(b))
                    .collect();
                let h = self.untiled(&h).expect("a tile= layout");
                h.into_iter()
                    .map(|b| match ieee {
                        true => ieee_f16(b),
                        false => cluster_f16(b),
                    })
                    .collect()
            }
            None => raw
                .chunks_exact(item)
                .map(|b| scalar(&self.dtype, b))
                .collect(),
        };
        if self.transposed() {
            let (m, n) = self.slab();
            for s in v.chunks_mut(m * n) {
                let src = s.to_vec();
                for i in 0..n {
                    for j in 0..m {
                        s[i * m + j] = src[j * n + i];
                    }
                }
            }
        }
        Ok(v)
    }

    /// The tile layout's `(gm, gn)`, when it has one.
    pub fn tile(&self) -> Option<(i128, i128)> {
        match self.layout.get("tile") {
            Some(LayoutVal::Dims(d)) if d.len() == 2 => Some((d[0], d[1])),
            _ => None,
        }
    }

    /// Host fp16 values (row-major, `[.., M, N]`) as the `tile=` layout's
    /// bytes: tiles of gm x gn 4x4 sub-tiles, row-major tiles, row-major
    /// sub-tiles in a tile, element (i, j) of a sub-tile at 4*i + j.
    pub fn tiled(&self, x: &[u16]) -> Option<Vec<u16>> {
        let (gm, gn) = self.tile()?;
        let (m, n) = (
            self.shape[self.shape.len() - 2] as usize,
            self.shape[self.shape.len() - 1] as usize,
        );
        let (gm, gn) = (gm as usize, gn as usize);
        let slab = m * n;
        let mut out = Vec::with_capacity(x.len());
        let (tm, tn) = (m / (4 * gm), n / (4 * gn));
        let column_major = crate::mutant::on("num.layout/tiles-column-major");
        for s in x.chunks(slab) {
            for o in 0..tm * tn {
                let (ti, tj) = match column_major {
                    true => (o % tm, o / tm),
                    false => (o / tn, o % tn),
                };
                {
                    for a in 0..gm {
                        for b in 0..gn {
                            for i in 0..4 {
                                for j in 0..4 {
                                    let row = (ti * gm + a) * 4 + i;
                                    let col = (tj * gn + b) * 4 + j;
                                    out.push(s[row * n + col]);
                                }
                            }
                        }
                    }
                }
            }
        }
        Some(out)
    }

    /// The `tile=` layout's fp16 bit patterns back in row-major order.
    pub fn untiled(&self, raw: &[u16]) -> Option<Vec<u16>> {
        let (gm, gn) = self.tile()?;
        let (m, n) = (
            self.shape[self.shape.len() - 2] as usize,
            self.shape[self.shape.len() - 1] as usize,
        );
        let (gm, gn) = (gm as usize, gn as usize);
        let slab = m * n;
        let mut out = vec![0u16; raw.len() - raw.len() % slab.max(1)];
        let swap = crate::mutant::on("l1.layout/subtile-transposed");
        for (k, s) in raw.chunks_exact(slab).enumerate() {
            let mut at = 0;
            for ti in 0..m / (4 * gm) {
                for tj in 0..n / (4 * gn) {
                    for a in 0..gm {
                        for b in 0..gn {
                            for i in 0..4 {
                                for j in 0..4 {
                                    let (i, j) = if swap { (j, i) } else { (i, j) };
                                    let row = (ti * gm + a) * 4 + i;
                                    let col = (tj * gn + b) * 4 + j;
                                    out[k * slab + row * n + col] = s[at];
                                    at += 1;
                                }
                            }
                        }
                    }
                }
            }
        }
        Some(out)
    }
}
