//! One module per unit ISA.

pub mod cluster;
pub mod mover;
pub mod sru;
pub mod vector;

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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::unit::vector::core::CoreFormats;

    #[test]
    fn credit_word_is_what_the_narrowest_credit_word_encodes() {
        let f = CoreFormats::new();
        let wait = |n: u64| {
            f.wait
                .encode(&[("waiter", 0), ("counter", 1), ("n", n as i64)])
        };
        let post = |n: u64| {
            f.post
                .encode(&[("dst_x", 1), ("dst_y", 0), ("n", n as i64)])
        };
        assert_eq!(CREDIT_WORD, 512);
        assert!(wait(CREDIT_WORD).is_ok() && post(CREDIT_WORD).is_ok());
        // Twice the count no longer fits the VWAIT that waits for it.
        assert!(wait(2 * CREDIT_WORD).is_err());
        assert!(post(2 * CREDIT_WORD).is_ok());
    }
}
