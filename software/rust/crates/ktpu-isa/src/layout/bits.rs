//! `W256`: one 256-bit unit payload, four little-endian 64-bit limbs.

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::fmt;
use std::ops::{BitAnd, BitOr, BitOrAssign};

#[derive(Clone, Copy, Default, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct W256(pub [u64; 4]);

impl W256 {
    pub const ZERO: W256 = W256([0; 4]);
    pub const BITS: u32 = 256;

    pub fn from_u64(v: u64) -> W256 {
        W256([v, 0, 0, 0])
    }

    pub fn from_u128(v: u128) -> W256 {
        W256([v as u64, (v >> 64) as u64, 0, 0])
    }

    /// The low 64 bits.
    pub fn low_u64(self) -> u64 {
        self.0[0]
    }

    pub fn is_zero(self) -> bool {
        self.0 == [0; 4]
    }

    /// Bits `[lo + width - 1 : lo]`, `width` at most 64.
    pub fn get(self, lo: u32, width: u32) -> u64 {
        debug_assert!(width <= 64 && lo + width <= 256);
        if width == 0 {
            return 0;
        }
        let limb = (lo / 64) as usize;
        let off = lo % 64;
        let mut v = self.0[limb] >> off;
        if off != 0 && limb + 1 < 4 {
            v |= self.0[limb + 1] << (64 - off);
        }
        v & mask(width)
    }

    /// Whether every bit in `[lo + width - 1 : lo]` is zero, any width.
    pub fn is_zero_in(self, lo: u32, width: u32) -> bool {
        let mut at = lo;
        let end = lo + width;
        while at < end {
            let w = (end - at).min(64);
            if self.get(at, w) != 0 {
                return false;
            }
            at += w;
        }
        true
    }

    /// `self` with `value` (its low `width` bits, at most 64) or-ed in at `lo`.
    pub fn with(self, lo: u32, width: u32, value: u64) -> W256 {
        self | W256::from_u64(value & mask(width)).shift_left(lo)
    }

    pub fn shift_left(self, n: u32) -> W256 {
        if n >= 256 {
            return W256::ZERO;
        }
        let limbs = (n / 64) as usize;
        let bits = n % 64;
        let mut out = [0u64; 4];
        for i in (limbs..4).rev() {
            let src = i - limbs;
            out[i] = self.0[src] << bits;
            if bits != 0 && src > 0 {
                out[i] |= self.0[src - 1] >> (64 - bits);
            }
        }
        W256(out)
    }

    pub fn shift_right(self, n: u32) -> W256 {
        if n >= 256 {
            return W256::ZERO;
        }
        let limbs = (n / 64) as usize;
        let bits = n % 64;
        let mut out = [0u64; 4];
        for (i, slot) in out.iter_mut().enumerate().take(4 - limbs) {
            let src = i + limbs;
            *slot = self.0[src] >> bits;
            if bits != 0 && src + 1 < 4 {
                *slot |= self.0[src + 1] << (64 - bits);
            }
        }
        W256(out)
    }

    /// The payload as the package stores it: 32 bytes, little-endian.
    pub fn to_le_bytes(self) -> [u8; 32] {
        let mut out = [0u8; 32];
        for (i, limb) in self.0.iter().enumerate() {
            out[i * 8..i * 8 + 8].copy_from_slice(&limb.to_le_bytes());
        }
        out
    }

    pub fn from_le_bytes(bytes: [u8; 32]) -> W256 {
        let mut out = [0u64; 4];
        for (i, limb) in out.iter_mut().enumerate() {
            *limb = u64::from_le_bytes(bytes[i * 8..i * 8 + 8].try_into().unwrap());
        }
        W256(out)
    }

    /// `0x...` hex, no leading zeros (Python's `hex()`).
    pub fn to_hex(self) -> String {
        let mut s = String::new();
        for limb in self.0.iter().rev() {
            if s.is_empty() {
                if *limb != 0 {
                    s = format!("{limb:x}");
                }
            } else {
                s.push_str(&format!("{limb:016x}"));
            }
        }
        if s.is_empty() {
            s.push('0');
        }
        format!("0x{s}")
    }

    /// Hex with or without `0x`; None when it is not hex or exceeds 256 bits.
    pub fn from_hex(text: &str) -> Option<W256> {
        let h = text.strip_prefix("0x").unwrap_or(text);
        if h.is_empty() || h.len() > 64 || !h.bytes().all(|b| b.is_ascii_hexdigit()) {
            return None;
        }
        let padded = format!("{h:0>64}");
        let mut out = [0u64; 4];
        for (i, limb) in out.iter_mut().enumerate() {
            let at = 64 - 16 * (i + 1);
            *limb = u64::from_str_radix(&padded[at..at + 16], 16).ok()?;
        }
        Some(W256(out))
    }
}

/// The low `width` bits set, `width` at most 64.
pub fn mask(width: u32) -> u64 {
    if width >= 64 {
        u64::MAX
    } else {
        (1u64 << width) - 1
    }
}

impl BitOr for W256 {
    type Output = W256;
    fn bitor(self, o: W256) -> W256 {
        W256(std::array::from_fn(|i| self.0[i] | o.0[i]))
    }
}

impl BitOrAssign for W256 {
    fn bitor_assign(&mut self, o: W256) {
        *self = *self | o;
    }
}

impl BitAnd for W256 {
    type Output = W256;
    fn bitand(self, o: W256) -> W256 {
        W256(std::array::from_fn(|i| self.0[i] & o.0[i]))
    }
}

impl fmt::Debug for W256 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_hex())
    }
}

impl fmt::Display for W256 {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_hex())
    }
}

impl Serialize for W256 {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_hex())
    }
}

impl<'de> Deserialize<'de> for W256 {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<W256, D::Error> {
        let s = String::deserialize(d)?;
        W256::from_hex(&s)
            .ok_or_else(|| serde::de::Error::custom(format!("not a 256-bit hex: {s}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shifts_and_fields_cross_limbs() {
        let w = W256::from_u64(0xABCD).shift_left(60);
        assert_eq!(w.get(60, 16), 0xABCD);
        assert_eq!(w.shift_right(60), W256::from_u64(0xABCD));
        let top = W256::ZERO.with(252, 4, 0xF);
        assert_eq!(top.to_hex(), format!("0xf{}", "0".repeat(63)));
        assert_eq!(W256::from_hex(&top.to_hex()), Some(top));
        assert_eq!(W256::from_le_bytes(top.to_le_bytes()), top);
        assert!(top.is_zero_in(0, 252) && !top.is_zero_in(200, 56));
    }
}
