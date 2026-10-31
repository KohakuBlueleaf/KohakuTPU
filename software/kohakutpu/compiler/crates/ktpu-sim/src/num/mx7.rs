//! MXFP7 entries (NUM.MX7): four 32-byte words holding 4 lanes x 32 K of int7
//! under one E5M3 scale a lane.

use crate::mem::{Word, WORD};

pub const LANES: usize = 4;
pub const KBLOCK: usize = 32;
pub const WORDS: usize = 4;
pub const ENTRY_BYTES: usize = WORDS * WORD;
const PER: usize = KBLOCK / WORDS;
const SBIAS: i64 = 20;
const QMAX: i64 = 63;
/// `round(4096 * 8 / m8)` for m8 = 8..15 (NUM.MX7.003).
const RECIP: [i64; 8] = [4096, 3641, 3277, 2979, 2731, 2520, 2341, 2185];

/// One entry as a cluster holds it: int7 by (lane, k), the scale byte by lane.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub q: [[i8; KBLOCK]; LANES],
    pub field: [u8; LANES],
}

impl Entry {
    pub fn empty() -> Entry {
        Entry {
            q: [[0; KBLOCK]; LANES],
            field: [0; LANES],
        }
    }

    /// The significands of side A (`b` false) or side B, little-endian words.
    pub fn read(bytes: &[u8], b: bool) -> Entry {
        let mut e = Entry::empty();
        for l in 0..LANES {
            e.field[l] = bytes[3 - l];
        }
        for w in 0..WORDS {
            let word = &bytes[w * WORD..(w + 1) * WORD];
            for s in 0..LANES * PER {
                let (l, kk) = if b {
                    (s % LANES, s / LANES)
                } else {
                    (s / PER, s % PER)
                };
                let at = 256 - 7 * (s + 1);
                let mut v = 0u32;
                for i in 0..7 {
                    let bit = at + i;
                    v |= u32::from(word[bit / 8] >> (bit % 8) & 1) << i;
                }
                e.q[l][w * PER + kk] = ((v << 25) as i32 >> 25) as i8;
            }
        }
        e
    }

    pub fn write(&self, b: bool) -> [Word; WORDS] {
        let mut out = [[0u8; WORD]; WORDS];
        for (w, word) in out.iter_mut().enumerate() {
            for l in 0..LANES {
                word[3 - l] = self.field[l];
            }
            for s in 0..LANES * PER {
                let (l, kk) = if b {
                    (s % LANES, s / LANES)
                } else {
                    (s / PER, s % PER)
                };
                let at = 256 - 7 * (s + 1);
                let v = self.q[l][w * PER + kk] as u8 & 0x7F;
                for i in 0..7 {
                    let bit = at + i;
                    word[bit / 8] |= (v >> i & 1) << (bit % 8);
                }
            }
        }
        out
    }

    /// One scale's `(exponent field, m8)`.
    pub fn scale(&self, lane: usize) -> (i64, i64) {
        let f = i64::from(self.field[lane]);
        (f >> 3, 8 + (f & 7))
    }
}

/// `(es, m8)` of a block whose largest magnitude bits are `peak` (NUM.MX7.002).
fn scale(peak: i64) -> (i64, i64) {
    let ef = (peak >> 10) & 0x1F;
    let frac = peak & 0x3FF;
    let (mut sig, mut ep) = if ef == 0 {
        (frac, 1)
    } else {
        (1 << 10 | frac, ef)
    };
    if sig == 0 {
        return (0, 8);
    }
    while sig < 1 << 10 {
        sig *= 2;
        ep -= 1;
    }
    let m8 = (sig + 125) / 126;
    let es = if m8 >= 16 { ep - 20 } else { ep - 21 };
    let m8 = if m8 >= 16 { m8 - 8 } else { m8 };
    (es.clamp(-SBIAS, 31 - SBIAS), m8)
}

/// One FP16 value's int7 under `(es, m8)` (NUM.MX7.003).
fn quantise(b: u16, es: i64, m8: i64) -> i8 {
    let b = i64::from(b);
    let ef = (b >> 10) & 0x1F;
    let frac = b & 0x3FF;
    let (sig, e) = if ef == 0 {
        (frac, 1)
    } else {
        (1 << 10 | frac, ef)
    };
    let prod = sig * RECIP[(m8 - 8) as usize];
    let sh = 25 + 12 + es - e;
    let mag = if sh > 40 {
        0
    } else {
        ((prod >> sh) + ((prod >> (sh - 1)) & 1)).min(QMAX)
    };
    (if b >> 15 != 0 { -mag } else { mag }) as i8
}

/// The entry of 4 x 32 FP16 values, lane by lane (the quantiser's block).
pub fn entry(x: &[[u16; KBLOCK]; LANES]) -> Entry {
    let mut e = Entry::empty();
    for l in 0..LANES {
        let peak = x[l]
            .iter()
            .map(|b| i64::from(*b & 0x7FFF))
            .max()
            .unwrap_or(0);
        let (es, m8) = scale(peak);
        e.field[l] = (((es + SBIAS) & 0x1F) << 3 | (m8 - 8)) as u8;
        for k in 0..KBLOCK {
            e.q[l][k] = quantise(x[l][k], es, m8);
        }
    }
    e
}

/// The real value of int7 `q` under scale byte `field`.
pub fn value(q: i8, field: u8) -> f64 {
    let (e, m) = (i32::from(field >> 3), f64::from(8 + (field & 7)));
    f64::from(q) * 2f64.powi(e - SBIAS as i32) * m / 8.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> [[u16; KBLOCK]; LANES] {
        let mut x = [[0u16; KBLOCK]; LANES];
        for (l, row) in x.iter_mut().enumerate() {
            for (k, v) in row.iter_mut().enumerate() {
                *v = (0x3C00 + (l * 977 + k * 131) as u16) | ((k as u16 & 1) << 15);
            }
        }
        x
    }

    #[test]
    fn an_entry_reads_back_from_either_packing() {
        let e = entry(&sample());
        for b in [false, true] {
            let words = e.write(b);
            assert_eq!(Entry::read(&words.concat(), b), e);
        }
    }

    #[test]
    fn a_quantised_value_is_near_its_source() {
        let x = sample();
        let e = entry(&x);
        for l in 0..LANES {
            for k in 0..KBLOCK {
                let want = ktpu_isa::numerics::ieee_f16(x[l][k]);
                let scale = value(1, e.field[l]);
                assert!(
                    (value(e.q[l][k], e.field[l]) - want).abs() <= scale,
                    "{l} {k}"
                );
            }
        }
    }
}
