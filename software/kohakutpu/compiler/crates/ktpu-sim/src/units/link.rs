//! Bursts between units (ISA.VC.180-.182, L1.MG.010).

use crate::mem::Word;

/// A receiving unit of a mesh.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Place {
    Vc(usize),
    Mg(usize),
    /// By its mesh coordinate, as a word names it.
    At(u32, u32),
}

/// `mesh` none: the sender's.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Target {
    pub mesh: Option<usize>,
    pub place: Place,
}

/// What a burst carries: words, or an accumulator's sub-tiles at full precision.
#[derive(Clone, Debug, PartialEq)]
pub enum Data {
    Words(Vec<Word>),
    Acc(Vec<[f64; 16]>),
}

/// A burst on its way: buffer `buf` at word `at`, counted by the receiver if `count`.
#[derive(Clone, Debug, PartialEq)]
pub struct Landing {
    pub to: Target,
    pub buf: u32,
    pub at: usize,
    pub data: Data,
    pub count: bool,
}
