//! An expression as vector-core statements (`kernels/expr.py` `emit`):
//! each op one instruction or a fixed pattern of them -- a square root
//! through `rsqrt`, a sine through a reduction to [-pi, pi] and a
//! polynomial, a truncation through a rounding constant.

use super::{fold, Expr, OpName, RedOp, E};
use crate::trace::vector::Fold;
use crate::trace::{fail, Arg, Trace, R};
use ktpu_graph::ValueOp as Op;
use std::collections::HashMap;
use std::f64::consts::PI;

/// sqrt's floor under its rsqrt (rsqrt(0) is infinite).
const TINY: f64 = 1e-30;
/// 2pi in two parts, the first exact in few bits so `n * HI` is exact.
const TWO_PI_HI: f64 = 6.28125;
/// sin(r) on [-pi, pi]: odd Taylor terms through r^11.
const SIN_COEF: [f64; 6] = [
    1.0,
    -1.0 / 6.0,
    1.0 / 120.0,
    -1.0 / 5040.0,
    1.0 / 362880.0,
    -1.0 / 39916800.0,
];

fn two_pi_lo() -> f64 {
    2.0 * PI - TWO_PI_HI
}

/// `e` over `values` (operand i -> a traced value): a node reached twice
/// is emitted once; a constant subexpression folds to a number.
pub fn emit(t: &mut Trace, e: &E, values: &HashMap<usize, Arg>) -> R<Arg> {
    emit_over(t, e, values, Fold::default())
}

/// [`emit`], every reduction under `fold`.
pub fn emit_over(t: &mut Trace, e: &E, values: &HashMap<usize, Arg>, fold: Fold) -> R<Arg> {
    let mut memo = HashMap::new();
    Emitter { t, values, fold }.go(e, &mut memo)
}

/// The lane arithmetic the rounding patterns are written in: the trace's
/// (each op an instruction) or lane values' (`Lanes`, each result rounded
/// to the lane format), so the lowering's own pattern is what a lane
/// computes.
pub trait Arith {
    type X: Copy;
    fn num(&self, x: f64) -> Self::X;
    fn add(&mut self, a: Self::X, b: Self::X) -> R<Self::X>;
    fn sub(&mut self, a: Self::X, b: Self::X) -> R<Self::X>;
    /// A compare (`Cmplt`, `Cmpgt`): 1 where it holds, else 0.
    fn cmp(&mut self, op: Op, a: Self::X, b: Self::X) -> R<Self::X>;
    /// `c ? a : b`.
    fn sel(&mut self, c: Self::X, a: Self::X, b: Self::X) -> R<Self::X>;
    fn abs(&mut self, a: Self::X) -> R<Self::X>;
    /// The lane's significand bits (`vc.sig_bits`).
    fn sig_bits(&self) -> i64;
}

/// `1.5 * 2^(sig_bits - 1)`: added and subtracted, a lane value
/// `|x| < 2^(sig_bits - 2)` rounds to a whole number.
fn round_const(sig_bits: i64) -> f64 {
    let half = (2f64).powi(sig_bits as i32 - 1);
    match crate::mutants::on("num.expr/round-constant-2-15") {
        true => half,
        false => 1.5 * half,
    }
}

/// x rounded to the nearest whole number (`|x| < 2^(sig_bits - 2)`).
pub fn round<A: Arith>(a: &mut A, x: A::X) -> R<A::X> {
    let c = a.num(round_const(a.sig_bits()));
    let s = a.add(x, c)?;
    a.sub(s, c)
}

/// Toward zero (RUL.060): `|x| >= 2^(sig_bits - 1)` is already whole;
/// below it the nearest whole number by a rounding constant of x's sign,
/// `2^(sig_bits - 1)`, less one where it overshot away from 0.
pub fn trunc<A: Arith>(a: &mut A, x: A::X) -> R<A::X> {
    let whole = (2f64).powi(a.sig_bits() as i32 - 1);
    let neg = a.cmp(Op::Cmplt, x, a.num(0.0))?;
    let s = match crate::mutants::on("num.expr/round-constant-2-15") {
        true => a.num(whole),
        false => a.sel(neg, a.num(-whole), a.num(whole))?,
    };
    let up = a.add(x, s)?;
    let r = a.sub(up, s)?;
    let over = a.cmp(Op::Cmpgt, r, x)?;
    let floor = a.sub(r, over)?;
    let under = a.cmp(Op::Cmplt, r, x)?;
    let ceil = a.add(r, under)?;
    let small = a.sel(neg, ceil, floor)?;
    let ax = a.abs(x)?;
    let inside = a.cmp(Op::Cmplt, ax, a.num(whole))?;
    a.sel(inside, small, x)
}

/// Lane values: each result through `value`, the lane format's rounding
/// (a number operand too, as a lane holds it).
pub struct Lanes<F: Fn(f64) -> f64> {
    pub value: F,
    pub sig_bits: i64,
}

impl<F: Fn(f64) -> f64> Arith for Lanes<F> {
    type X = f64;
    fn num(&self, x: f64) -> f64 {
        (self.value)(x)
    }
    fn add(&mut self, a: f64, b: f64) -> R<f64> {
        Ok((self.value)(a + b))
    }
    fn sub(&mut self, a: f64, b: f64) -> R<f64> {
        Ok((self.value)(a - b))
    }
    fn cmp(&mut self, op: Op, a: f64, b: f64) -> R<f64> {
        let holds = match op {
            Op::Cmplt => a < b,
            Op::Cmpgt => a > b,
            Op::Cmpeq => a == b,
            other => return fail("API.READ.001", format!("{other:?} is not a compare")),
        };
        Ok((self.value)(f64::from(u8::from(holds))))
    }
    fn sel(&mut self, c: f64, a: f64, b: f64) -> R<f64> {
        Ok((self.value)(if c != 0.0 { a } else { b }))
    }
    fn abs(&mut self, a: f64) -> R<f64> {
        Ok((self.value)(a.abs()))
    }
    fn sig_bits(&self) -> i64 {
        self.sig_bits
    }
}

struct Emitter<'a> {
    t: &'a mut Trace,
    values: &'a HashMap<usize, Arg>,
    fold: Fold,
}

impl Arith for Emitter<'_> {
    type X = Arg;
    fn num(&self, x: f64) -> Arg {
        Arg::Num(x)
    }
    fn add(&mut self, a: Arg, b: Arg) -> R<Arg> {
        Emitter::add(self, a, b)
    }
    fn sub(&mut self, a: Arg, b: Arg) -> R<Arg> {
        Emitter::sub(self, a, b)
    }
    fn cmp(&mut self, op: Op, a: Arg, b: Arg) -> R<Arg> {
        Emitter::cmp(self, op, a, b)
    }
    fn sel(&mut self, c: Arg, a: Arg, b: Arg) -> R<Arg> {
        self.math(Op::Sel, &[c, a, b])
    }
    fn abs(&mut self, a: Arg) -> R<Arg> {
        self.math(Op::Abs, &[a])
    }
    fn sig_bits(&self) -> i64 {
        self.t.facts.sig_bits
    }
}

impl Emitter<'_> {
    fn go(&mut self, e: &E, memo: &mut HashMap<*const Expr, Arg>) -> R<Arg> {
        let key = std::rc::Rc::as_ptr(e);
        if let Some(v) = memo.get(&key) {
            return Ok(*v);
        }
        let out = match &**e {
            Expr::Load(i) => match self.values.get(i) {
                Some(v) => *v,
                None => return fail("API.READ.001", format!("no operand {i}")),
            },
            Expr::Const(x) => Arg::Num(*x),
            Expr::Reduce(r, x) => {
                let a = self.go(x, memo)?;
                self.reduce(*r, a)?
            }
            Expr::Op(o, args) => {
                let mut a = vec![];
                for x in args {
                    a.push(self.go(x, memo)?);
                }
                self.op(*o, &a)?
            }
        };
        memo.insert(key, out);
        Ok(out)
    }

    fn math(&mut self, op: Op, args: &[Arg]) -> R<Arg> {
        Ok(Arg::Val(self.t.math(op, args)?))
    }

    /// `a + b` as Python's operators do it: numbers fold, a number first
    /// stays first.
    fn add(&mut self, a: Arg, b: Arg) -> R<Arg> {
        match (a, b) {
            (Arg::Num(x), Arg::Num(y)) => Ok(Arg::Num(x + y)),
            _ => self.math(Op::Add, &[a, b]),
        }
    }

    fn sub(&mut self, a: Arg, b: Arg) -> R<Arg> {
        match (a, b) {
            (Arg::Num(x), Arg::Num(y)) => Ok(Arg::Num(x - y)),
            _ => self.math(Op::Sub, &[a, b]),
        }
    }

    fn mul(&mut self, a: Arg, b: Arg) -> R<Arg> {
        match (a, b) {
            (Arg::Num(x), Arg::Num(y)) => Ok(Arg::Num(x * y)),
            _ => self.math(Op::Mul, &[a, b]),
        }
    }

    /// A value op with a number operand moved second (`max`, `min`).
    fn value_first(&mut self, op: Op, a: Arg, b: Arg) -> R<Arg> {
        match a {
            Arg::Num(_) => self.math(op, &[b, a]),
            Arg::Val(_) => self.math(op, &[a, b]),
        }
    }

    fn op(&mut self, o: OpName, a: &[Arg]) -> R<Arg> {
        let nums: Option<Vec<f64>> = a
            .iter()
            .map(|x| match x {
                Arg::Num(v) => Some(*v),
                Arg::Val(_) => None,
            })
            .collect();
        if let Some(nums) = nums {
            return Ok(Arg::Num(fold(o, &nums)));
        }
        let num = |x: &Arg| matches!(x, Arg::Num(_));
        match o {
            OpName::Add => self.add(a[0], a[1]),
            OpName::Sub => self.sub(a[0], a[1]),
            OpName::Mul => self.mul(a[0], a[1]),
            OpName::Div => match a[1] {
                Arg::Num(0.0) => fail("TG.PLAN.016", "a division by the constant 0"),
                Arg::Num(y) => self.mul(a[0], Arg::Num(1.0 / y)),
                Arg::Val(_) => {
                    let r = self.math(Op::Inv, &[a[1]])?;
                    self.mul(a[0], r)
                }
            },
            OpName::Max | OpName::Or => self.value_first(Op::Max, a[0], a[1]),
            OpName::Min => self.value_first(Op::Min, a[0], a[1]),
            OpName::Neg => self.math(Op::Neg, &[a[0]]),
            OpName::Abs => self.math(Op::Abs, &[a[0]]),
            OpName::Recip => self.math(Op::Inv, &[a[0]]),
            OpName::Exp2 => self.math(Op::Exp2, &[a[0]]),
            OpName::Log2 => self.math(Op::Log2, &[a[0]]),
            OpName::Rsqrt => self.math(Op::Rsqrt, &[a[0]]),
            OpName::Sqrt => {
                let m = self.math(Op::Max, &[a[0], Arg::Num(TINY)])?;
                let r = self.math(Op::Rsqrt, &[m])?;
                self.mul(a[0], r)
            }
            OpName::Pow => {
                let (base, ex) = (a[0], a[1]);
                let l = self.math(Op::Log2, &[base])?;
                let m = if num(&ex) {
                    self.mul(l, ex)?
                } else {
                    self.mul(ex, l)?
                };
                self.math(Op::Exp2, &[m])
            }
            OpName::Sin => self.sin(a[0]),
            OpName::Trunc => self.trunc(a[0]),
            OpName::Cmplt => self.cmp(Op::Cmplt, a[0], a[1]),
            OpName::Cmpgt => self.cmp(Op::Cmpgt, a[0], a[1]),
            OpName::Cmpeq => self.cmp(Op::Cmpeq, a[0], a[1]),
            OpName::Cmpne => {
                let m = self.cmp(Op::Cmpeq, a[0], a[1])?;
                self.sub(Arg::Num(1.0), m)
            }
            OpName::Where => match a[0] {
                Arg::Num(c) => Ok(if c != 0.0 { a[1] } else { a[2] }),
                Arg::Val(_) => self.math(Op::Sel, a),
            },
            OpName::Fma => {
                if a.iter().any(num) {
                    let p = self.mul(a[0], a[1])?;
                    self.add(p, a[2])
                } else {
                    self.math(Op::Fma, a)
                }
            }
            OpName::And => self.mul(a[0], a[1]),
            OpName::Xor => {
                let d = self.sub(a[0], a[1])?;
                self.math(Op::Abs, &[d])
            }
            OpName::Not => self.sub(Arg::Num(1.0), a[0]),
            OpName::Id => Ok(a[0]),
        }
    }

    /// A compare of two values or a value and a number; a number first is
    /// the mirrored compare.
    fn cmp(&mut self, op: Op, a: Arg, b: Arg) -> R<Arg> {
        if let Arg::Num(_) = a {
            let mirrored = match op {
                Op::Cmplt => Op::Cmpgt,
                Op::Cmpgt => Op::Cmplt,
                other => other,
            };
            return self.math(mirrored, &[b, a]);
        }
        self.math(op, &[a, b])
    }

    fn trunc(&mut self, x: Arg) -> R<Arg> {
        trunc(self, x)
    }

    /// sin x: n = round(x / 2pi), r = x - n * 2pi (two-part 2pi), then the
    /// odd polynomial in r.
    fn sin(&mut self, x: Arg) -> R<Arg> {
        let scaled = self.mul(x, Arg::Num(1.0 / (2.0 * PI)))?;
        let n = round(self, scaled)?;
        let hi = self.mul(n, Arg::Num(TWO_PI_HI))?;
        let rh = self.sub(x, hi)?;
        let lo = self.mul(n, Arg::Num(two_pi_lo()))?;
        let r = self.sub(rh, lo)?;
        let r2 = self.mul(r, r)?;
        let mut acc = Arg::Num(SIN_COEF[SIN_COEF.len() - 1]);
        for c in SIN_COEF[..SIN_COEF.len() - 1].iter().rev() {
            let p = self.mul(r2, acc)?;
            acc = self.add(p, Arg::Num(*c))?;
        }
        self.mul(r, acc)
    }

    fn rsum(&mut self, x: Arg) -> R<Arg> {
        Ok(Arg::Val(self.t.reduce_over(Op::Rsum, x, self.fold)?))
    }

    fn reduce(&mut self, r: RedOp, x: Arg) -> R<Arg> {
        match r {
            RedOp::Sum => self.rsum(x),
            RedOp::Max => Ok(Arg::Val(self.t.reduce_over(Op::Rmax, x, self.fold)?)),
            RedOp::Prod => {
                // sign * 2^sum(log2 |x|), 0 if any element is 0: statistic
                // math holds no compare, so the parity is arithmetic.
                let zero = self.cmp(Op::Cmpeq, x, Arg::Num(0.0))?;
                let neg = self.cmp(Op::Cmplt, x, Arg::Num(0.0))?;
                let ax = self.math(Op::Abs, &[x])?;
                let nz = self.add(ax, zero)?;
                let l = self.math(Op::Log2, &[nz])?;
                let s = self.rsum(l)?;
                let mag = self.math(Op::Exp2, &[s])?;
                let n = self.rsum(neg)?;
                let h = self.mul(n, Arg::Num(0.5))?;
                let h = self.sub(h, Arg::Num(0.25))?;
                let half = round(self, h)?;
                let twice = self.mul(half, Arg::Num(2.0))?;
                let odd = self.sub(n, twice)?;
                let odd2 = self.mul(odd, Arg::Num(2.0))?;
                let sign = self.sub(Arg::Num(1.0), odd2)?;
                let sm = self.mul(sign, mag)?;
                let zs = self.rsum(zero)?;
                let any = self.math(Op::Min, &[zs, Arg::Num(1.0)])?;
                let keep = self.sub(Arg::Num(1.0), any)?;
                self.mul(sm, keep)
            }
        }
    }
}
