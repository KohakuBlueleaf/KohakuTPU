//! The schedule's random numbers: xorshift64*, reproducible from a seed.

/// Seed 0 always takes the first choice, the schedule a reader expects.
#[derive(Clone, Debug)]
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(seed)
    }

    /// An index below `n`.
    pub fn pick(&mut self, n: usize) -> usize {
        if self.0 == 0 {
            return 0;
        }
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        (self.0.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 33) as usize % n
    }
}
