//! L1: the `.ktpu` text (parse, print, module, expansion), node lowering to
//! L0, vector images and the image scheduler (docs/spec/stack/l1/program.md).

pub mod build;
pub mod error;
pub mod layout;
pub mod lower;
pub mod mutant;
pub mod sched;
pub mod spec;
pub mod text;

pub use error::{Error, Result};
pub use text::{emit, fmt, parse, read, Body, Definition, Module, Stmt, Term};
