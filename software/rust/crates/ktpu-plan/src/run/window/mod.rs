//! Window contractions and tabled sums (`ktpugrad/window.py`, `run._window`,
//! `_lay`, `_padded`, `_pad_copy`, `_lane_copies`, `_uploaded`,
//! `_table_only`, `_enumerated`, `_ungated`; TG.RUN.004, .010, .021).
//!
//! A read's coordinates (`Dim`) are its ranges grouped: ranges at one
//! stride, and ranges a gate couples; a gate is a box of them. Each side of
//! a window contraction is laid out as [outer.., K.., lane], the lane a K
//! range both sides read alone, padded to whole words.

mod coords;
mod find;
mod lay;
mod tabled;
mod terms;
mod ungated;

use std::collections::HashMap;
use std::rc::Rc;

use ktpu_machine::Refusal;

use super::boxes::Axes;
use super::rows::{all_reduces, prune, reductions_of};
use super::{Bufs, Runner};
use crate::exec::{flat, View};
use crate::expr::{self, Expr, E};
use crate::fold;
use crate::lower::{Access, Plan, Slot, Table};
use crate::uop::{Arg, Id};
use terms::{factors, one_sum, weights, How};

/// One coordinate of a read: `sum(mult * range)` at `stride`; its valid
/// coordinates `[a, b)` counted from the least one.
#[derive(Clone, Debug, PartialEq)]
pub struct Dim {
    pub stride: i64,
    pub terms: Vec<(Id, i64)>,
    pub bx: (i64, i64),
}

impl Dim {
    fn ranges(&self) -> impl Iterator<Item = Id> + '_ {
        self.terms.iter().map(|(r, _)| *r)
    }

    fn has(&self, r: Id) -> bool {
        self.terms.iter().any(|(x, _)| *x == r)
    }
}

/// One operand: its read, its coordinates (largest stride first), the
/// lane's coordinate (None: a virtual lane of one).
#[derive(Clone, Debug)]
pub struct Side {
    pub acc: Access,
    pub dims: Vec<Dim>,
    pub lane: Option<usize>,
}

impl Side {
    fn dim(&self, r: Id) -> usize {
        self.dims
            .iter()
            .position(|d| d.has(r))
            .expect("a range of the side")
    }

    fn mult(&self, r: Id) -> i64 {
        let d = &self.dims[self.dim(r)];
        d.terms.iter().find(|(x, _)| *x == r).unwrap().1
    }

    fn reads(&self) -> Vec<Id> {
        self.dims.iter().flat_map(|d| d.ranges()).collect()
    }
}

/// A window contraction (`window.Contraction`).
pub struct Contraction {
    pub plan: Plan,
    pub red: E,
    pub scale: f64,
    pub a: Side,
    pub b: Side,
    pub bt: Vec<Id>,
    pub ma: Vec<Id>,
    pub nb: Vec<Id>,
    pub ks: Vec<Id>,
    pub pad: i64,
}

/// Every coordinate of a grid of extents `ext`, row-major.
fn grid(ext: &[i64]) -> Vec<Vec<i64>> {
    let mut out = vec![vec![]];
    for e in ext {
        out = out
            .into_iter()
            .flat_map(|c| {
                (0..*e).map(move |i| {
                    let mut c = c.clone();
                    c.push(i);
                    c
                })
            })
            .collect();
    }
    out
}

fn gcd(a: i64, b: i64) -> i64 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

fn same_map(a: &[(Id, i64)], b: &[(Id, i64)]) -> bool {
    a.len() == b.len() && a.iter().all(|x| b.contains(x))
}
