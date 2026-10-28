//! A vector-core `image` to IMEM words (L1.IMG.001-.051, L1.VC.003-.009).
//!
//! CHKVL / XB / USTR / PSTR are not written: the encoder tracks the core's
//! configuration and emits one where an op needs a value the core does not
//! hold. Configuration persists across RUNs, so an image enters in an
//! unknown state unless the caller states one. A `loop` body enters in the
//! state its own end leaves, set up once before the loop.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::rc::Rc;

use ktpu_isa::unit::vector::core::{
    CFG_PSTR, CFG_USTR, CR, K_DRAIN, K_FILL, K_MATH, K_PACK, K_PACK1, K_UNPACK, K_UNPACK1, RX, W_A,
    W_D, W_FE, W_M, W_MK, W_P, W_U, XM_BCAST, XM_MERGE, XM_NONE, XM_ROT, XM_XOR,
};
use ktpu_isa::unit::vector::{
    dim, e8m15, Chunk, MathOp, PtrReg, Src as Sel, StrideReg, VWord, VecIsa, WalkMode,
};
use ktpu_machine::{Coord, Machine};

use super::ops;
use crate::error::{Error, Result};
use crate::mutant::on;
use crate::text::{fmt, Definition, Env, Expander, Module, Reader, Stmt, Term, Val};

mod desc;
mod facts;
mod flow;
mod images;
mod math;
mod memory;
mod operand;
mod state;
mod sync;
mod tables;

pub use desc::*;
pub use facts::*;
pub use images::*;
use operand::*;
pub use state::*;
pub use tables::*;

/// Encodes one image body (statements already expanded) to words. `config`
/// counts the configuration words the encoder chose.
pub struct Encoder<'a> {
    pub reader: &'a Reader,
    pub isa: &'a VecIsa,
    pub facts: &'a CoreFacts,
    pub entry: State,
    pub units: Option<&'a dyn Units>,
    pub config: usize,
}

type Words = Vec<u64>;
