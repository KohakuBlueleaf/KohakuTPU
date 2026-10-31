//! Value simulators of each level, ordered not timed: a seed picks any order the
//! guards allow. They read only the text reader, the ISA's fields and numerics.

pub mod l0;
pub mod l1;
pub mod l2;
pub mod machine;
pub mod mem;
pub mod num;
pub mod rng;
pub mod units;

pub use mem::Dram;
pub use rng::Rng;

/// What stops a simulation: a program outside the machine, or a program this
/// simulator cannot yet run (named, so a coverage check can count it).
pub type R<T> = Result<T, String>;
