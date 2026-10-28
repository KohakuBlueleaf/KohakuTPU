//! The tinygrad kernel ast: its UOp graph, the index values folded while
//! planning, and indices made affine on their domain.

pub mod domain;
pub mod fold;
pub mod uop;
