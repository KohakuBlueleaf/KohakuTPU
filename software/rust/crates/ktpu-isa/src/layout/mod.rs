//! Layout machinery every unit ISA is declared in: the 256-bit payload and
//! the field table.

pub mod bits;
pub mod format;

pub use bits::{mask, W256};
pub use format::{value, Decoded, Field, Format, InstSet};

/// Bits to hold `n` distinct values (`log2` rounded up; 1 for `n` <= 2).
pub fn bits_for(n: u64) -> u32 {
    (64 - n.saturating_sub(1).leading_zeros()).max(1)
}

/// `log2(n)` of a power of two, or None.
pub fn log2_exact(n: u64) -> Option<u32> {
    n.is_power_of_two().then(|| n.trailing_zeros())
}
