//! The L1 simulator: an `fn NAME.l1` read from its text and run on a machine.

mod image;
mod node;
pub mod term;

pub use node::{run, Binds};
