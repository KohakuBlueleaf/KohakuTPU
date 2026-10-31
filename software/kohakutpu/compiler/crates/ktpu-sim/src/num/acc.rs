//! The accumulator's FP22 (NUM.ACC): S1 E7 M14, biased 63, FP16 at its drain.

use super::mx7::{Entry, KBLOCK, LANES};

const BIAS: i32 = 63;
const SIG_BITS: i32 = 15;

/// `x` rounded to FP22, nearest even.
pub fn round(x: f64) -> f64 {
    if x == 0.0 || !x.is_finite() {
        return x;
    }
    let exp = x.abs().log2().floor() as i32;
    let unit = 2f64.powi(exp - SIG_BITS + 1);
    let r = (x / unit).round_ties_even() * unit;
    if r.abs() < 2f64.powi(1 - BIAS) {
        0.0
    } else {
        r
    }
}

/// FP22 to FP16 at a drain (NUM.ACC.004-.005): no subnormals, no infinities.
pub fn drain(x: f64) -> u16 {
    if x == 0.0 {
        return 0;
    }
    let sign = (x.is_sign_negative() as u16) << 15;
    let a = x.abs();
    let mut e = a.log2().floor() as i32;
    let mut f = ((a / 2f64.powi(e) - 1.0) * 1024.0).round_ties_even() as i32;
    if f == 1024 {
        f = 0;
        e += 1;
    }
    let exp = e + 15;
    if exp <= 0 {
        0
    } else if exp >= 31 {
        sign | 0x7BFF
    } else {
        sign | (exp as u16) << 10 | f as u16
    }
}

/// One K block's integer partial of element (i, j) and its exponent sum
/// (NUM.ACC.002): the value is `part * 2^(sum - anchor - 6)`.
fn partial(a: &Entry, b: &Entry, i: usize, j: usize) -> (i64, i64) {
    let part: i64 = (0..KBLOCK)
        .map(|k| i64::from(a.q[i][k]) * i64::from(b.q[j][k]))
        .sum();
    let ((ea, ma), (eb, mb)) = (a.scale(i), b.scale(j));
    (part * ma * mb, ea + eb)
}

/// The 4 x 4 sub-tile of one issue's K blocks: a pair aligns to the larger
/// exponent sum, the smaller truncated, with no zero test (NUM.ACC.008).
pub fn subtile(blocks: &[(&Entry, &Entry)], anchor: i64) -> [f64; LANES * LANES] {
    let mut out = [0.0; LANES * LANES];
    for i in 0..LANES {
        for j in 0..LANES {
            let mut parts = blocks.iter().map(|(a, b)| partial(a, b, i, j));
            let (mut part, mut sum) = parts.next().expect("a block");
            for (p, s) in parts {
                if s > sum {
                    part = (part >> (s - sum).min(30)) + p;
                    sum = s;
                } else {
                    part += p >> (sum - s).min(30);
                }
            }
            out[4 * i + j] = round(part as f64 * 2f64.powi((sum - anchor - 6) as i32));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_drain_saturates_and_flushes() {
        assert_eq!(drain(1e9), 0x7BFF);
        assert_eq!(drain(-1e9), 0xFBFF);
        assert_eq!(drain(1e-9), 0);
        assert_eq!(drain(-1e-9), 0);
        assert_eq!(drain(1.0), 0x3C00);
    }

    #[test]
    fn rounding_keeps_fifteen_bits() {
        let x = 1.0 + 2f64.powi(-14);
        assert_eq!(round(x), x);
        assert_eq!(round(1.0 + 2f64.powi(-15)), 1.0);
    }
}
