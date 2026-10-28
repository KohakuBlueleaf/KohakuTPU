//! Shared numeric boundaries: E8M15 constants (RUL.056) and FP16 writes
//! and consumer-specific reads (RUL.057).

/// `x` as an E8M15 word, rounded to nearest even: no subnormals (below
/// 2^-126 flushes to a signed zero), 2^128 and up infinite, NaN quiet.
pub fn e8m15(x: f64) -> u32 {
    if x.is_nan() {
        return 0x7FC000;
    }
    let s = x.is_sign_negative() as u32;
    if x.is_infinite() {
        return s << 23 | 0xFF << 15;
    }
    if x == 0.0 {
        return s << 23;
    }
    if x.abs() < 2f64.powi(-126) && !crate::mutant::on("num.e8m15/subnormals") {
        return s << 23;
    }
    let bits = x.abs().to_bits();
    let (mant, e2) = match bits >> 52 {
        0 => (bits & ((1 << 52) - 1), -1074i64),
        e => (bits & ((1 << 52) - 1) | 1 << 52, e as i64 - 1075),
    };
    let len = 64 - mant.leading_zeros() as i64;
    let mut big = len - 1 + e2;
    let shift = 16 - len;
    let mut q = if shift >= 0 {
        mant << shift
    } else {
        let n = (-shift) as u32;
        let q = mant >> n;
        let rem = mant & ((1 << n) - 1);
        let half = 1u64 << (n - 1);
        if crate::mutant::on("num.e8m15/truncate") {
            q
        } else if rem > half || (rem == half && q & 1 == 1) {
            q + 1
        } else {
            q
        }
    };
    if q == 1 << 16 {
        q >>= 1;
        big += 1;
    }
    let e = big + 127;
    if e >= 255 {
        return s << 23 | 0xFF << 15;
    }
    if e <= 0 {
        return s << 23;
    }
    s << 23 | (e as u32) << 15 | (q as u32 & 0x7FFF)
}

/// A host value rounded to the lane's significand and exponent range.
pub fn e8m15_value(x: f64) -> f64 {
    if !x.is_finite() || x == 0.0 {
        return x;
    }
    let tiny = if crate::mutant::on("num.e8m15/subnormals") {
        -149
    } else {
        -126
    };
    if x.abs() < 2f64.powi(tiny) {
        return 0.0f64.copysign(x);
    }
    let sig = if crate::mutant::on("num.e8m15/significand-17") {
        17
    } else {
        16
    };
    let exp = ((x.abs().to_bits() >> 52) & 0x7ff) as i32 - 1023;
    let unit = 2f64.powi(exp - sig + 1);
    let rounded = (x / unit).round_ties_even() * unit;
    let huge = if crate::mutant::on("num.e8m15/no-overflow") {
        129
    } else {
        128
    };
    if rounded.abs() >= 2f64.powi(huge) {
        f64::INFINITY.copysign(x)
    } else {
        rounded
    }
}

/// IEEE fp16 bits as float64, including subnormals, infinities and NaNs.
pub fn ieee_f16(b: u16) -> f64 {
    let sign = (b as u64 >> 15) << 63;
    let (exp, frac) = ((b >> 10) & 0x1F, (b & 0x3FF) as u64);
    let mag = match exp {
        0 => frac as f64 * 2f64.powi(-24),
        0x1F => return f64::from_bits(sign | 0x7FF << 52 | frac << 42),
        e => (1.0 + frac as f64 / 1024.0) * 2f64.powi(e as i32 - 15),
    };
    f64::from_bits(sign | mag.to_bits())
}

/// Host FP16 writes: RNE, finite overflow saturated at ±65504, inf/NaN kept.
pub fn f16_saturating(x: f64) -> u16 {
    let sign = (x.is_sign_negative() as u16) << 15;
    if x.is_nan() {
        return 0x7E00;
    }
    if x.is_infinite() {
        return sign | 0x7C00;
    }
    let a = x.abs();
    if a >= 65504.0 {
        if crate::mutant::on("num.fp16/no-saturation") && a >= 65520.0 {
            return sign | 0x7C00;
        }
        return sign | 0x7BFF;
    }
    if a < 2f64.powi(-14) {
        return sign | (a * 2f64.powi(24)).round_ties_even() as u16;
    }
    let bits = a.to_bits();
    let mut e = ((bits >> 52) & 0x7FF) as i32 - 1023;
    let mut f = ((a / 2f64.powi(e) - 1.0) * 1024.0).round_ties_even() as i32;
    if f == 1024 {
        f = 0;
        e += 1;
    }
    sign | ((e + 15) as u16) << 10 | f as u16
}

/// Accumulator FP16 reads: exponent 0 is signed zero, 31 an ordinary binade.
pub fn fp16_value(bits: u16) -> f64 {
    let exp = ((bits >> 10) & 0x1F) as i32;
    let mag = match exp {
        0 => 0.0,
        e => (1.0 + (bits & 0x3FF) as f64 / 1024.0) * 2f64.powi(e - 15),
    };
    if bits & 0x8000 != 0 {
        -mag
    } else {
        mag
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rul_056_rne_range_and_signed_zero() {
        for (x, want) in [
            (1.0 + 3.0 * 2f64.powi(-17), 0x3F8001),
            (1.0 + 2f64.powi(-16), 0x3F8000),
            (1e39, 0x7F8000),
            (-1e39, 0xFF8000),
            (-0.0, 0x800000),
            (f64::from_bits(2f64.powi(-126).to_bits() - 1), 0),
            (2f64.powi(-126), 0x008000),
        ] {
            assert_eq!(e8m15(x), want, "{x}");
        }
        assert_eq!(e8m15(f64::NAN), 0x7FC000);
        #[cfg(feature = "mutants")]
        {
            let tiny = f64::from_bits(2f64.powi(-126).to_bits() - 1);
            assert_ne!(
                crate::mutant::with("num.e8m15/subnormals", || e8m15(tiny)),
                e8m15(tiny)
            );
        }
    }

    #[test]
    fn rul_057_writes_saturate_and_reads_depend_on_consumer() {
        for x in [70000.0, 1e6, 65520.0, 65504.0] {
            assert_eq!(f16_saturating(x), 0x7BFF);
            assert_eq!(f16_saturating(-x), 0xFBFF);
        }
        assert_eq!(f16_saturating(f64::INFINITY), 0x7C00);
        assert_eq!(f16_saturating(f64::NEG_INFINITY), 0xFC00);
        assert_eq!(f16_saturating(1.0 + 2f64.powi(-11)), 0x3C00);
        assert_eq!(f16_saturating(2f64.powi(-24)), 1);
        assert_eq!(ieee_f16(0x7C00), f64::INFINITY);
        assert_eq!(fp16_value(0x7C00), 65536.0);
        assert_eq!(ieee_f16(1), 2f64.powi(-24));
        assert_eq!(fp16_value(1), 0.0);
        assert!(fp16_value(0x8000).is_sign_negative());
        #[cfg(feature = "mutants")]
        assert_ne!(
            crate::mutant::with("num.fp16/no-saturation", || f16_saturating(70000.0)),
            f16_saturating(70000.0)
        );
    }
}
