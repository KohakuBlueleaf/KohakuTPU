//! The kernel generators the tensor planner (`ktpu-plan`) calls: each
//! builds one kernel as a `ktpu_graph::Graph`, the graph the Python
//! generator's trace gives (`software/application/kohakutpu/application/kernels`).
//!
//! [`trace`] records statements as the `kt` tracer does; [`expr`] is the
//! elementwise expression and its emission; [`gen`] holds the generators
//! ([`gen::vector`], [`gen::matmul`], [`gen::convert`], [`gen::transpose`],
//! [`gen::lanes`]). Every hardware fact comes from the `Machine`; a
//! refusal names its clause.

pub mod call;
pub mod expr;
pub mod gen;
pub mod kernel;
pub mod mutants;
pub mod pyrepr;
pub mod spec;
pub mod trace;

pub use call::call;
pub use gen::convert::{cast_kernel, copy_kernel, fill_kernel, walk_kernel};
pub use gen::lanes::relayout_kernel;
pub use gen::matmul::{mm_kernel, quant_kernel, Mm, Side};
pub use gen::transpose::{masks, reverse_kernel, transpose_kernel};
pub use gen::vector::{flat_kernel, rows_kernel, Kind, Operand};
pub use kernel::Kernel;
