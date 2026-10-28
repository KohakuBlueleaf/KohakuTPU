//! Runner forms that span crates, hosted here (each needs crates its owner
//! cannot depend on).

#[cfg(all(feature = "isa", feature = "l2"))]
mod isa;
#[cfg(feature = "stack")]
mod isa_stack;
#[cfg(all(feature = "kernels", feature = "stack"))]
mod kernels;
#[cfg(feature = "stack")]
mod l0;
#[cfg(feature = "stack")]
mod l1;
#[cfg(feature = "stack")]
mod num;
#[cfg(feature = "plan")]
mod plan;

#[cfg(all(feature = "isa", feature = "l2"))]
pub use isa::{isa_mutants, isa_run, isa_runners, isa_skipped_mutants};
#[cfg(all(feature = "kernels", feature = "stack"))]
pub use kernels::{kernels_run, kernels_runners};
#[cfg(feature = "stack")]
pub use l0::l0_run;
#[cfg(feature = "stack")]
pub use l1::{l1_mutants, l1_run, l1_skipped_mutants};
#[cfg(feature = "stack")]
pub use num::{num_mutants, num_run, num_runners, num_skipped_mutants};
#[cfg(feature = "plan")]
pub use plan::{plan_mutants, plan_run, plan_skipped_mutants};
