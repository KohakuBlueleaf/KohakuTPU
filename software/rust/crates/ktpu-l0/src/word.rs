//! The 256-bit payload is `ktpu_isa::W256` (four little-endian limbs); the
//! field operations L0 needs on it besides W256's own.

pub use ktpu_isa::layout::bits::mask as mask64;
pub use ktpu_isa::W256 as Word;

pub trait WordOps: Sized {
    /// `v << bit`, bits past 255 dropped.
    fn placed(v: u64, bit: u32) -> Self;
    /// Ones over bits `[bit, bit + width)`, `width` 0..=64.
    fn field_mask(bit: u32, width: u32) -> Self;
    /// Bits `[bit, bit + width)` := `v` (truncated to `width`), the rest kept.
    fn set(&mut self, bit: u32, width: u32, v: u64);
    /// This word with bits `[bit, bit + width)` cleared.
    fn cleared(self, bit: u32, width: u32) -> Self;
    /// The 64 bits at `bit` (0, 64, 128 or 192).
    fn limb(&self, bit: u32) -> u64;
    fn xor(self, o: Self) -> Self;
    fn not(self) -> Self;
}

impl WordOps for Word {
    fn placed(v: u64, bit: u32) -> Word {
        Word::from_u64(v).shift_left(bit)
    }

    fn field_mask(bit: u32, width: u32) -> Word {
        Word::placed(mask64(width), bit)
    }

    fn set(&mut self, bit: u32, width: u32, v: u64) {
        *self = self.cleared(bit, width) | Word::placed(v & mask64(width), bit);
    }

    fn cleared(self, bit: u32, width: u32) -> Word {
        self & Word::field_mask(bit, width).not()
    }

    fn limb(&self, bit: u32) -> u64 {
        self.get(bit, 64)
    }

    fn xor(self, o: Word) -> Word {
        Word(std::array::from_fn(|i| self.0[i] ^ o.0[i]))
    }

    fn not(self) -> Word {
        Word(self.0.map(|l| !l))
    }
}
