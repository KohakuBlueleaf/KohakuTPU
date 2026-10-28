//! Index-only values evaluated while planning (`ktpugrad/fold.py`): an ast
//! subtree that reads no buffer, over the loop nest, with numpy 2's arrays
//! and promotion (NEP 50) and tinygrad's integer semantics. Nothing here
//! reads a buffer.

pub use ktpu_isa::numerics::{f16_saturating as f16_bits, ieee_f16 as f16_value};

use crate::uop::{Arg, Graph, Id};

/// The numpy element types the evaluation holds.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Ty {
    Bool,
    I8,
    I16,
    I32,
    I64,
    U8,
    U16,
    U32,
    U64,
    F16,
    F32,
    F64,
}

impl Ty {
    pub fn is_float(self) -> bool {
        matches!(self, Ty::F16 | Ty::F32 | Ty::F64)
    }

    fn is_signed(self) -> bool {
        matches!(self, Ty::I8 | Ty::I16 | Ty::I32 | Ty::I64)
    }

    fn bits(self) -> u32 {
        match self {
            Ty::Bool => 1,
            Ty::I8 | Ty::U8 => 8,
            Ty::I16 | Ty::U16 | Ty::F16 => 16,
            Ty::I32 | Ty::U32 | Ty::F32 => 32,
            Ty::I64 | Ty::U64 | Ty::F64 => 64,
        }
    }

    fn int(signed: bool, bits: u32) -> Ty {
        match (signed, bits) {
            (true, 8) => Ty::I8,
            (true, 16) => Ty::I16,
            (true, 32) => Ty::I32,
            (true, _) => Ty::I64,
            (false, 8) => Ty::U8,
            (false, 16) => Ty::U16,
            (false, 32) => Ty::U32,
            (false, _) => Ty::U64,
        }
    }

    fn float(bits: u32) -> Ty {
        match bits {
            16 => Ty::F16,
            32 => Ty::F32,
            _ => Ty::F64,
        }
    }

    /// numpy's `result_type` of two arrays (no weak scalars).
    pub fn promote(a: Ty, b: Ty) -> Ty {
        use Ty::*;
        if a == b {
            return a;
        }
        if a == Bool {
            return b;
        }
        if b == Bool {
            return a;
        }
        match (a.is_float(), b.is_float()) {
            (true, true) => Ty::float(a.bits().max(b.bits())),
            (true, false) => Ty::float(a.bits().max(Ty::holds(b))),
            (false, true) => Ty::float(b.bits().max(Ty::holds(a))),
            (false, false) => {
                if a.is_signed() == b.is_signed() {
                    Ty::int(a.is_signed(), a.bits().max(b.bits()))
                } else {
                    let (s, u) = if a.is_signed() { (a, b) } else { (b, a) };
                    if s.bits() > u.bits() {
                        s
                    } else if u.bits() < 64 {
                        Ty::int(true, 2 * u.bits())
                    } else {
                        F64
                    }
                }
            }
        }
    }

    /// The narrowest float bits holding every value of int type `t`.
    fn holds(t: Ty) -> u32 {
        match t.bits() {
            1 | 8 => 16,
            16 => 32,
            _ => 64,
        }
    }

    /// The numpy type of a tinygrad scalar dtype (`fold.np_dtype`).
    pub fn of(dtype: &str) -> Option<Ty> {
        Some(match dtype {
            "bool" => Ty::Bool,
            "char" => Ty::I8,
            "short" => Ty::I16,
            "int" => Ty::I32,
            "long" => Ty::I64,
            "uchar" => Ty::U8,
            "ushort" => Ty::U16,
            "uint" => Ty::U32,
            "ulong" => Ty::U64,
            "half" => Ty::F16,
            "float" => Ty::F32,
            "double" => Ty::F64,
            _ => return None,
        })
    }
}

/// f64 as the host's saturating FP16 write (RNE), back as f64.
pub fn to_f16(v: f64) -> f64 {
    f16_value(f16_bits(v))
}

/// An n-d array: row-major values, held as i128 (ints, bools 0 / 1) or f64
/// (floats, rounded to their type after each op).
#[derive(Clone, Debug, PartialEq)]
pub struct Arr {
    pub shape: Vec<usize>,
    pub ty: Ty,
    pub ints: Vec<i128>,
    pub floats: Vec<f64>,
}

impl Arr {
    fn int(ty: Ty, shape: Vec<usize>, v: Vec<i128>) -> Arr {
        let v = v.into_iter().map(|x| wrap(ty, x)).collect();
        Arr {
            shape,
            ty,
            ints: v,
            floats: Vec::new(),
        }
    }

    fn float(ty: Ty, shape: Vec<usize>, v: Vec<f64>) -> Arr {
        let v = v.into_iter().map(|x| round(ty, x)).collect();
        Arr {
            shape,
            ty,
            ints: Vec::new(),
            floats: v,
        }
    }

    pub fn scalar_int(v: i64) -> Arr {
        Arr::int(Ty::I64, Vec::new(), vec![v as i128])
    }

    pub fn len(&self) -> usize {
        self.shape.iter().product()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Element `i` as f64.
    pub fn f(&self, i: usize) -> f64 {
        if self.ty.is_float() {
            self.floats[i]
        } else {
            self.ints[i] as f64
        }
    }

    /// Element `i` as an integer (floats truncated, as a C cast).
    pub fn i(&self, i: usize) -> i128 {
        if self.ty.is_float() {
            self.floats[i] as i128
        } else {
            self.ints[i]
        }
    }

    /// `astype(ty)`.
    pub fn cast(&self, ty: Ty) -> Arr {
        let n = self.len();
        if ty == Ty::Bool {
            return Arr::int(
                ty,
                self.shape.clone(),
                (0..n).map(|i| (self.f(i) != 0.0) as i128).collect(),
            );
        }
        if ty.is_float() {
            Arr::float(ty, self.shape.clone(), (0..n).map(|i| self.f(i)).collect())
        } else {
            Arr::int(ty, self.shape.clone(), (0..n).map(|i| self.i(i)).collect())
        }
    }

    /// The array broadcast to `shape`.
    pub fn broadcast(&self, shape: &[usize]) -> Arr {
        let idx = gather(&self.shape, shape);
        if self.ty.is_float() {
            Arr {
                shape: shape.to_vec(),
                ty: self.ty,
                ints: Vec::new(),
                floats: idx.iter().map(|i| self.floats[*i]).collect(),
            }
        } else {
            Arr {
                shape: shape.to_vec(),
                ty: self.ty,
                ints: idx.iter().map(|i| self.ints[*i]).collect(),
                floats: Vec::new(),
            }
        }
    }
}

fn wrap(ty: Ty, x: i128) -> i128 {
    match ty {
        Ty::Bool => (x != 0) as i128,
        _ => {
            let bits = ty.bits();
            let m = 1i128 << bits;
            let v = x.rem_euclid(m);
            if ty.is_signed() && v >= m / 2 {
                v - m
            } else {
                v
            }
        }
    }
}

fn round(ty: Ty, x: f64) -> f64 {
    match ty {
        Ty::F16 => to_f16(x),
        Ty::F32 => x as f32 as f64,
        _ => x,
    }
}

/// numpy's broadcast shape.
pub fn broadcast_shape(a: &[usize], b: &[usize]) -> Option<Vec<usize>> {
    let n = a.len().max(b.len());
    let mut out = vec![0; n];
    for i in 0..n {
        let x = if i + a.len() >= n {
            a[i + a.len() - n]
        } else {
            1
        };
        let y = if i + b.len() >= n {
            b[i + b.len() - n]
        } else {
            1
        };
        out[i] = if x == y || y == 1 {
            x
        } else if x == 1 {
            y
        } else {
            return None;
        };
    }
    Some(out)
}

/// For each element of `to` (row-major), the index into an array of shape
/// `from` broadcast to it.
fn gather(from: &[usize], to: &[usize]) -> Vec<usize> {
    let n: usize = to.iter().product();
    let off = to.len() - from.len();
    let mut strides = vec![0usize; to.len()];
    let mut s = 1;
    for i in (0..from.len()).rev() {
        strides[i + off] = if from[i] == 1 { 0 } else { s };
        s *= from[i];
    }
    let mut out = Vec::with_capacity(n);
    let mut idx = vec![0usize; to.len()];
    for _ in 0..n {
        out.push(idx.iter().zip(&strides).map(|(i, s)| i * s).sum());
        for d in (0..to.len()).rev() {
            idx[d] += 1;
            if idx[d] < to[d] {
                break;
            }
            idx[d] = 0;
        }
    }
    out
}

/// A refusal of evaluation (a buffer read, an op with no folding).
#[derive(Clone, Debug, PartialEq)]
pub struct NotConst(pub String);

fn binary(
    a: &Arr,
    b: &Arr,
    ty: Ty,
    f: impl Fn(f64, f64) -> f64,
    g: impl Fn(i128, i128) -> i128,
) -> Result<Arr, NotConst> {
    let shape = broadcast_shape(&a.shape, &b.shape)
        .ok_or_else(|| NotConst("shapes do not broadcast".into()))?;
    let (x, y) = (a.cast(ty).broadcast(&shape), b.cast(ty).broadcast(&shape));
    let n = x.len();
    Ok(if ty.is_float() {
        Arr::float(
            ty,
            shape,
            (0..n).map(|i| f(x.floats[i], y.floats[i])).collect(),
        )
    } else {
        Arr::int(ty, shape, (0..n).map(|i| g(x.ints[i], y.ints[i])).collect())
    })
}

fn compare(
    a: &Arr,
    b: &Arr,
    f: impl Fn(f64, f64) -> bool,
    g: impl Fn(i128, i128) -> bool,
) -> Result<Arr, NotConst> {
    let ty = Ty::promote(a.ty, b.ty);
    let shape = broadcast_shape(&a.shape, &b.shape)
        .ok_or_else(|| NotConst("shapes do not broadcast".into()))?;
    let (x, y) = (a.cast(ty).broadcast(&shape), b.cast(ty).broadcast(&shape));
    let n = x.len();
    let v = if ty.is_float() {
        (0..n)
            .map(|i| f(x.floats[i], y.floats[i]) as i128)
            .collect()
    } else {
        (0..n).map(|i| g(x.ints[i], y.ints[i]) as i128).collect()
    };
    Ok(Arr::int(Ty::Bool, shape, v))
}

/// A float-producing unary function (`np.sqrt` and the like): ints become
/// float64, a half stays half (computed in float32).
fn unary_float(a: &Arr, f: impl Fn(f64) -> f64) -> Arr {
    let ty = if a.ty.is_float() { a.ty } else { Ty::F64 };
    let n = a.len();
    Arr::float(
        ty,
        a.shape.clone(),
        (0..n)
            .map(|i| {
                let v = a.f(i);
                if ty == Ty::F64 {
                    f(v)
                } else {
                    f(v as f32 as f64) as f32 as f64
                }
            })
            .collect(),
    )
}

fn floor_div(a: i128, b: i128) -> i128 {
    if b == 0 {
        0
    } else {
        a.div_euclid(b)
            - if (a.rem_euclid(b) != 0) && (b < 0) {
                1
            } else {
                0
            }
    }
}

fn floor_mod(a: i128, b: i128) -> i128 {
    if b == 0 {
        0
    } else {
        a - b * floor_div(a, b)
    }
}

/// `fold.evaluate`: `u` over the loop nest `ranges` (one axis each, in that
/// order), broadcastable to their extents.
pub fn evaluate(
    g: &Graph,
    u: Id,
    ranges: &[Id],
    memo: &mut std::collections::HashMap<Id, Arr>,
) -> Result<Arr, NotConst> {
    if let Some(a) = memo.get(&u) {
        return Ok(a.clone());
    }
    let n = g.get(u);
    let out = match n.op.as_str() {
        "RANGE" => {
            let at = ranges
                .iter()
                .position(|r| *r == u)
                .ok_or_else(|| NotConst("a range outside the loop nest".into()))?;
            let e = extent(g, u).ok_or_else(|| NotConst("a range of no constant extent".into()))?
                as usize;
            let mut shape = vec![1; ranges.len()];
            shape[at] = e;
            Arr::int(Ty::I64, shape, (0..e as i128).collect())
        }
        "CONST" => match &n.arg {
            Arg::Int(v) => Arr::int(Ty::I64, Vec::new(), vec![*v as i128]),
            Arg::Bool(b) => Arr::int(Ty::Bool, Vec::new(), vec![*b as i128]),
            Arg::Float(_) => Arr::float(Ty::F64, Vec::new(), vec![n.arg.float().unwrap()]),
            _ => return Err(NotConst(format!("a constant {:?}", n.arg))),
        },
        "INDEX" | "PARAM" | "LOAD" => {
            return Err(NotConst("a buffer read is data, not a constant".into()))
        }
        op => {
            let src = n.src.clone();
            let a: Vec<Arr> = src
                .iter()
                .map(|s| evaluate(g, *s, ranges, memo))
                .collect::<Result<_, _>>()?;
            alu(op, &n.dtype, &a)?
        }
    };
    memo.insert(u, out.clone());
    Ok(out)
}

fn alu(op: &str, dtype: &str, a: &[Arr]) -> Result<Arr, NotConst> {
    let p = |i: usize, j: usize| Ty::promote(a[i].ty, a[j].ty);
    Ok(match op {
        "CAST" => {
            let ty = Ty::of(dtype).ok_or_else(|| NotConst(format!("a cast to {dtype}")))?;
            a[0].cast(ty)
        }
        "ADD" => binary(&a[0], &a[1], p(0, 1), |x, y| x + y, |x, y| x + y)?,
        "SUB" => binary(&a[0], &a[1], p(0, 1), |x, y| x - y, |x, y| x - y)?,
        "MUL" => binary(&a[0], &a[1], p(0, 1), |x, y| x * y, |x, y| x * y)?,
        "FDIV" => {
            let ty = p(0, 1);
            let ty = if ty.is_float() { ty } else { Ty::F64 };
            binary(&a[0], &a[1], ty, |x, y| x / y, |_, _| 0)?
        }
        "CDIV" | "CMOD" => {
            let ty = p(0, 1);
            let div = op == "CDIV";
            binary(
                &a[0],
                &a[1],
                ty,
                move |x, y| {
                    let q = (x.abs() / y.abs()).floor();
                    let q = if (x < 0.0) != (y < 0.0) { -q } else { q };
                    if div {
                        q
                    } else {
                        x - y * q
                    }
                },
                move |x, y| {
                    let q = if y == 0 { 0 } else { x.abs() / y.abs() };
                    let q = if (x < 0) != (y < 0) { -q } else { q };
                    if div {
                        q
                    } else {
                        x - y * q
                    }
                },
            )?
        }
        "FLOORDIV" => binary(&a[0], &a[1], p(0, 1), |x, y| (x / y).floor(), floor_div)?,
        "FLOORMOD" => binary(
            &a[0],
            &a[1],
            p(0, 1),
            |x, y| {
                let r = x % y;
                if r != 0.0 && (r < 0.0) != (y < 0.0) {
                    r + y
                } else {
                    r
                }
            },
            floor_mod,
        )?,
        "MAX" => binary(&a[0], &a[1], p(0, 1), f64::max, |x, y| x.max(y))?,
        "NEG" => {
            let z = Arr::int(a[0].ty, Vec::new(), vec![0]);
            binary(&z, &a[0], a[0].ty, |x, y| x - y, |x, y| x - y)?
        }
        "CMPLT" => compare(&a[0], &a[1], |x, y| x < y, |x, y| x < y)?,
        "CMPNE" => compare(&a[0], &a[1], |x, y| x != y, |x, y| x != y)?,
        "CMPEQ" => compare(&a[0], &a[1], |x, y| x == y, |x, y| x == y)?,
        "WHERE" => {
            let ty = Ty::promote(a[1].ty, a[2].ty);
            let s1 = broadcast_shape(&a[1].shape, &a[2].shape)
                .ok_or_else(|| NotConst("shapes".into()))?;
            let shape =
                broadcast_shape(&a[0].shape, &s1).ok_or_else(|| NotConst("shapes".into()))?;
            let c = a[0].cast(Ty::Bool).broadcast(&shape);
            let x = a[1].cast(ty).broadcast(&shape);
            let y = a[2].cast(ty).broadcast(&shape);
            let n = c.len();
            if ty.is_float() {
                Arr::float(
                    ty,
                    shape,
                    (0..n)
                        .map(|i| {
                            if c.ints[i] != 0 {
                                x.floats[i]
                            } else {
                                y.floats[i]
                            }
                        })
                        .collect(),
                )
            } else {
                Arr::int(
                    ty,
                    shape,
                    (0..n)
                        .map(|i| if c.ints[i] != 0 { x.ints[i] } else { y.ints[i] })
                        .collect(),
                )
            }
        }
        "AND" => binary(&a[0], &a[1], p(0, 1), |_, _| f64::NAN, |x, y| x & y)?,
        "OR" => binary(&a[0], &a[1], p(0, 1), |_, _| f64::NAN, |x, y| x | y)?,
        "XOR" => binary(&a[0], &a[1], p(0, 1), |_, _| f64::NAN, |x, y| x ^ y)?,
        "SHL" => binary(
            &a[0],
            &a[1],
            p(0, 1),
            |_, _| f64::NAN,
            |x, y| x << (y as u32 & 127),
        )?,
        "SHR" => binary(
            &a[0],
            &a[1],
            p(0, 1),
            |_, _| f64::NAN,
            |x, y| x >> (y as u32 & 127),
        )?,
        "RECIPROCAL" => unary_float(&a[0], |x| 1.0 / x),
        "SQRT" => unary_float(&a[0], f64::sqrt),
        "EXP2" => unary_float(&a[0], f64::exp2),
        "LOG2" => unary_float(&a[0], f64::log2),
        "SIN" => unary_float(&a[0], f64::sin),
        "TRUNC" => {
            if a[0].ty.is_float() {
                unary_float(&a[0], f64::trunc)
            } else {
                a[0].clone()
            }
        }
        "MULACC" => {
            let m = binary(&a[0], &a[1], p(0, 1), |x, y| x * y, |x, y| x * y)?;
            let t = Ty::promote(m.ty, a[2].ty);
            binary(&m, &a[2], t, |x, y| x + y, |x, y| x + y)?
        }
        "POW" => binary(&a[0], &a[1], p(0, 1), f64::powf, |x, y| {
            if y < 0 {
                0
            } else {
                x.pow(y as u32)
            }
        })?,
        _ => return Err(NotConst(format!("no constant folding of {op}"))),
    })
}

/// A range's step count, when its bound is a constant int.
pub fn extent(g: &Graph, r: Id) -> Option<i64> {
    let u = g.get(r);
    if u.op != "RANGE" || u.src.is_empty() {
        return None;
    }
    match &g.get(u.src[0]).arg {
        Arg::Int(n) if g.op(u.src[0]) == "CONST" => Some(*n),
        _ => None,
    }
}

/// `fold.index_only`: whether `u` reads no buffer (ranges and constants).
pub fn index_only(g: &Graph, u: Id, memo: &mut std::collections::HashMap<Id, bool>) -> bool {
    if let Some(v) = memo.get(&u) {
        return *v;
    }
    let out = match g.op(u) {
        "INDEX" | "PARAM" | "LOAD" | "REDUCE" => false,
        "RANGE" | "CONST" => true,
        _ => {
            let src = g.src(u).to_vec();
            src.iter().all(|s| index_only(g, *s, memo))
        }
    };
    memo.insert(u, out);
    out
}
