//! The number formats: E8M15 lanes (NUM.E8, NUM.F16), MXFP7, the accumulator.
//! Lane functions round correctly where silicon is faithful (NUM.E8.003).

pub mod acc;
pub mod mx7;

use ktpu_isa::numerics::{e8m15, f16_saturating, ieee_f16};

pub const ONE: u32 = 0x3F8000;
const SIGN: u32 = 1 << 23;
const EXP_MASK: u32 = 0xFF;
const MANT_BITS: u32 = 15;
const MANT_MASK: u32 = (1 << MANT_BITS) - 1;

fn exp_of(x: u32) -> u32 {
    (x >> MANT_BITS) & EXP_MASK
}

/// An E8M15 word's value (NUM.E8.001): exponent 0 is a signed zero.
pub fn value(x: u32) -> f64 {
    let (e, m) = (exp_of(x), x & MANT_MASK);
    let mag = match e {
        0 => 0.0,
        255 if m == 0 => f64::INFINITY,
        255 => f64::NAN,
        _ => (1.0 + f64::from(m) / f64::from(1 << MANT_BITS)) * 2f64.powi(e as i32 - 127),
    };
    if x & SIGN != 0 {
        -mag
    } else {
        mag
    }
}

/// `x` rounded to a lane (NUM.E8.002).
pub fn lane(x: f64) -> u32 {
    e8m15(x)
}

/// FP16 to E8M15, exact (NUM.F16.001-.002).
pub fn from_f16(b: u16) -> u32 {
    let (sign, e, m) = (
        (b as u32 >> 15) << 23,
        (b as u32 >> 10) & 0x1F,
        b as u32 & 0x3FF,
    );
    match (e, m) {
        (0, 0) => sign,
        (31, m) => sign | EXP_MASK << MANT_BITS | m << 5,
        _ => e8m15(ieee_f16(b)),
    }
}

/// E8M15 to FP16 (NUM.F16.003-.005).
pub fn to_f16(x: u32) -> u16 {
    let sign = ((x >> 23) as u16) << 15;
    match (exp_of(x), x & MANT_MASK) {
        (0, _) => sign,
        (255, 0) => sign | 0x7C00,
        (255, m) => sign | 0x7E00 | ((m >> 5) & 0x1FF) as u16,
        _ => f16_saturating(value(x)),
    }
}

/// FP32 to E8M15 (NUM.E8.004).
pub fn from_f32(b: u32) -> u32 {
    let (sign, e, m) = (b >> 31, (b >> 23) & 0xFF, b & 0x7F_FFFF);
    match (e, m) {
        (0, _) => sign << 23,
        (255, 0) => sign << 23 | EXP_MASK << MANT_BITS,
        (255, m) => sign << 23 | EXP_MASK << MANT_BITS | ((m >> 8) | 1 << 14),
        _ => e8m15(f64::from(f32::from_bits(b))),
    }
}

/// E8M15 to FP32, exact (NUM.E8.004).
pub fn to_f32(x: u32) -> u32 {
    if exp_of(x) == 0 {
        (x >> 23) << 31
    } else {
        (x >> 23) << 31 | exp_of(x) << 23 | (x & MANT_MASK) << 8
    }
}

/// `a * b + c`: a zero product passes `c` bit for bit (NUM.E8.008-.009).
pub fn fma(a: u32, b: u32, c: u32) -> u32 {
    let p = value(a) * value(b);
    if p == 0.0 {
        return c;
    }
    lane(p + value(c))
}

/// A picked operand through the FMA (ISA.VC.058): `-0` comes back `+0`.
pub fn picked(x: u32) -> u32 {
    if exp_of(x) == 0 {
        0
    } else {
        x
    }
}

pub fn neg(x: u32) -> u32 {
    picked(x ^ SIGN)
}

pub fn abs(x: u32) -> u32 {
    picked(x & !SIGN)
}

/// `vsel`'s test: `c`'s low 23 bits (ISA.VC.054).
pub fn nonzero(c: u32) -> bool {
    c & (SIGN - 1) != 0
}

pub fn lt(a: u32, b: u32) -> bool {
    value(a) < value(b)
}

pub fn gt(a: u32, b: u32) -> bool {
    value(a) > value(b)
}

pub fn eq(a: u32, b: u32) -> bool {
    value(a) == value(b)
}

pub fn exp2(a: u32) -> u32 {
    lane(value(a).exp2())
}

pub fn log2(a: u32) -> u32 {
    lane(value(a).log2())
}

pub fn inv(a: u32) -> u32 {
    lane(1.0 / value(a))
}

pub fn rsqrt(a: u32) -> u32 {
    lane(1.0 / value(a).sqrt())
}

/// `vexp2d`: `2^(a - floor(b))` and whether the lane's sticky bit sets
/// (ISA.VC.055, .062, .065), `tau` the machine's bound.
pub fn exp2d(a: u32, b: u32, tau: u32) -> (u32, bool) {
    let (x, y) = (value(a), value(b).clamp(-256.0, 255.0).floor());
    let r = (x - y).exp2();
    // A NaN `a`, or a positive one at +inf or past 2^8, decodes to a special that
    // latches whatever `b` is (v2_alu.v:373-374, 754).
    let special = x.is_nan() || (x > 0.0 && exp_of(a) > 134);
    (
        lane(r),
        special
            || (x.abs() < 128.0
                && r.partial_cmp(&2f64.powi(tau as i32)) != Some(std::cmp::Ordering::Less)),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_f16_comes_back() {
        for b in 0..=u16::MAX {
            let got = to_f16(from_f16(b));
            let nan = b & 0x7C00 == 0x7C00 && b & 0x3FF != 0;
            assert!(nan || got == b, "{b:#x} came back {got:#x}");
        }
    }

    #[test]
    fn a_zero_product_passes_the_addend() {
        assert_eq!(fma(0, ONE, 0x80_0000), 0x80_0000);
        assert_eq!(fma(0x80_0000, ONE, 0), 0);
    }

    #[test]
    fn a_picked_zero_loses_its_sign() {
        assert_eq!(picked(0x80_0000), 0);
        assert_eq!(picked(ONE), ONE);
    }
}
