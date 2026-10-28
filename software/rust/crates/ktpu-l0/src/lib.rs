//! L0 packages: wire format, steps, relocations, binding, the dispatch-engine
//! lowering, the boot block (docs/spec/stack/l0/*.md).
//!
//! `format` is the package and its bytes, `build` the step-by-step builder,
//! `program` an L1 program lowered to a builder, `engine` the dispatch-engine
//! lowering, `mover` the node mover's register writes, `boot` the boot block.

pub mod boot;
pub mod build;
pub mod engine;
pub mod error;
pub mod facts;
pub mod format;
pub mod link;
pub mod mover;
pub mod mutant;
pub mod program;
pub mod replay;
pub mod spec;
pub mod units;
pub mod word;

pub use build::fields::IsaAddresses;
pub use build::{AddrField, Addresses, PackageBuilder, Segment};
pub use error::{Refusal, Result};
pub use facts::{Coord, Facts};
pub use format::bind::{apply_reloc, bind_addresses};
pub use format::wire::checksum;
pub use format::{Alu, Buffer, Cond, Kind, Op, Package, Reloc, Step, Unit};
pub use program::{BuildOpts, Patch, Program, Resident, Stmt};
pub use word::{Word, WordOps};
