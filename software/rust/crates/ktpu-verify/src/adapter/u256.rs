//! A 256-bit payload word, four 64-bit limbs, least significant first
//! (L0.PKG.027).

#[derive(Clone, Copy, PartialEq, Eq, Hash, Default, Debug, PartialOrd, Ord)]
pub struct U256(pub [u64; 4]);

impl U256 {
    pub const ZERO: U256 = U256([0; 4]);

    pub fn from_u64(v: u64) -> Self {
        U256([v, 0, 0, 0])
    }

    /// The word from 32 little-endian bytes.
    pub fn from_le_bytes(b: &[u8]) -> Self {
        let mut l = [0u64; 4];
        for (i, limb) in l.iter_mut().enumerate() {
            let mut w = [0u8; 8];
            w.copy_from_slice(&b[i * 8..i * 8 + 8]);
            *limb = u64::from_le_bytes(w);
        }
        U256(l)
    }

    pub fn to_le_bytes(&self) -> [u8; 32] {
        let mut out = [0u8; 32];
        for (i, limb) in self.0.iter().enumerate() {
            out[i * 8..i * 8 + 8].copy_from_slice(&limb.to_le_bytes());
        }
        out
    }

    /// Bits `[lo, lo + width)`, `width` at most 64; bits past 255 read zero.
    pub fn bits(&self, lo: u32, width: u32) -> u64 {
        if width == 0 || lo >= 256 {
            return 0;
        }
        let limb = (lo / 64) as usize;
        let off = lo % 64;
        let mut v = self.0[limb] >> off;
        if off != 0 && limb + 1 < 4 {
            v |= self.0[limb + 1] << (64 - off);
        }
        if width >= 64 {
            v
        } else {
            v & ((1u64 << width) - 1)
        }
    }

    /// Bits `[lo, lo + width)` set to `v` (taken modulo 2^width); bits past
    /// 255 are dropped.
    pub fn set_bits(&mut self, lo: u32, width: u32, v: u64) {
        for i in 0..width {
            let at = lo + i;
            if at >= 256 {
                break;
            }
            let (limb, off) = ((at / 64) as usize, at % 64);
            if (v >> i) & 1 == 1 {
                self.0[limb] |= 1 << off;
            } else {
                self.0[limb] &= !(1 << off);
            }
        }
    }

    pub fn low(&self) -> u64 {
        self.0[0]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bits_cross_limbs() {
        let mut w = U256::ZERO;
        w.set_bits(60, 8, 0xA5);
        assert_eq!(w.bits(60, 8), 0xA5);
        assert_eq!(w.0[0] >> 60, 0x5);
        assert_eq!(w.0[1] & 0xF, 0xA);
        w.set_bits(248, 8, 0x7F);
        assert_eq!(w.bits(248, 8), 0x7F);
        assert_eq!(w.bits(250, 64), 0x1F);
    }

    #[test]
    fn bytes_round_trip_le() {
        let mut b = [0u8; 32];
        b[0] = 1;
        b[31] = 0x80;
        let w = U256::from_le_bytes(&b);
        assert_eq!(w.0[0], 1);
        assert_eq!(w.0[3], 0x80 << 56);
        assert_eq!(w.to_le_bytes(), b);
    }
}
