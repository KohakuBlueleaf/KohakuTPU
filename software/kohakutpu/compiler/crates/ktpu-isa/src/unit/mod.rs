//! One module per unit ISA.

pub mod cluster;
pub mod sru;
pub mod vector;

pub use ka_isa::mover;

const fn min(a: u32, b: u32) -> u32 {
    if a < b {
        a
    } else {
        b
    }
}

/// Bits of the narrowest credit count: a cluster or SRU POST's, a VPOST's,
/// and the VWAIT that waits for them.
pub const CREDIT_BITS: u32 = min(
    min(cluster::N_BITS, sru::POST_N_BITS),
    min(vector::core::VPOST_N_BITS, vector::core::VWAIT_N_BITS),
);

/// The largest power-of-two credit count every credit word carries
/// (`1 << (CREDIT_BITS - 1)`, 512): one count a stream splits its credits by.
pub const CREDIT_WORD: u64 = 1 << (CREDIT_BITS - 1);
