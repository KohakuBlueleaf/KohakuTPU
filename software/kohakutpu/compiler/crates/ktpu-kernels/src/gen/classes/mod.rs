//! Shape classes (`kernels/classes`): one fixed instruction stream for every
//! shape of a class, its counts node loops and classed `par`s, its data
//! staged into padded scratch. A class kernel comes with what the class
//! decided ([`Classed::info`]): widths, padding, the lowering taken, what a
//! template's value recipes read.

pub mod fold;
pub mod lanes;
pub mod mm;
pub mod moves;
pub mod rows;
pub mod wide;

use super::vector::{Kind, Operand};
use super::Hw;
use crate::kernel::{Kernel, Port};
use crate::pyrepr::Py;
use crate::trace::buffer::{sl, spec, Buf};
use crate::trace::{fail, Arg, Idx, Trace, R};
use ktpu_graph as g;
use serde_json::Value;

/// The class width a row width takes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Policy {
    Exact,
    Pow2,
    Coarse,
}

impl Policy {
    pub fn name(self) -> &'static str {
        match self {
            Policy::Exact => "exact",
            Policy::Pow2 => "pow2",
            Policy::Coarse => "coarse",
        }
    }

    pub fn parse(s: &str) -> Option<Policy> {
        [Policy::Exact, Policy::Pow2, Policy::Coarse]
            .into_iter()
            .find(|p| p.name() == s)
    }
}

/// Which cores a class runs on: all, or at most one a trip.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cores {
    All,
    Min,
}

impl Cores {
    pub fn name(self) -> &'static str {
        match self {
            Cores::All => "all",
            Cores::Min => "min",
        }
    }

    pub fn parse(s: &str) -> Option<Cores> {
        [Cores::All, Cores::Min].into_iter().find(|c| c.name() == s)
    }

    /// Cores of `cores` that `trips` trips run on.
    pub fn used(self, cores: i64, trips: i64) -> i64 {
        match self {
            Cores::All => cores,
            Cores::Min => cores.min(trips),
        }
    }
}

/// A shape class: its column block, its widest row band, its policies.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ShapeClass {
    pub kblock: i64,
    pub max_block: i64,
    pub policy: Policy,
    pub cores: Cores,
}

impl ShapeClass {
    /// The class width of rows `width` wide (at most `max_block`).
    pub fn width(&self, width: i64) -> R<i64> {
        if width <= 0 || width % self.kblock != 0 {
            return fail(
                "API.ROW.008",
                format!("{width} columns are not whole blocks of {}", self.kblock),
            );
        }
        Ok(match self.policy {
            Policy::Exact => width,
            Policy::Coarse if width <= self.max_block / 2 => self.kblock.max(self.max_block / 2),
            Policy::Coarse => self.max_block,
            Policy::Pow2 => self.kblock.max(1 << (64 - (width - 1).leading_zeros())),
        })
    }

    pub fn py(&self) -> Py {
        Py::Obj(
            "ShapeClass",
            vec![
                ("kblock", Py::Int(self.kblock)),
                ("max_block", Py::Int(self.max_block)),
                ("policy", Py::str(self.policy.name())),
                ("cores", Py::str(self.cores.name())),
            ],
        )
    }
}

/// The class route's measured knobs (`KTPU_CLASS_*`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Schedule {
    /// Row bands a classed image's loop holds.
    pub band: i64,
    /// A wide stream's block (a [`ShapeClass::kblock`] when `None`).
    pub wide_width: Option<i64>,
    /// Whole-trip rows read in place, not staged.
    pub rows_direct: bool,
    pub wide_direct: bool,
}

/// The knobs at their defaults.
pub const SCHEDULE: Schedule = Schedule {
    band: 4,
    wide_width: None,
    rows_direct: false,
    wide_direct: false,
};

/// A class kernel and what the class decided.
#[derive(Clone, Debug, PartialEq)]
pub struct Classed {
    pub kernel: Kernel,
    pub info: Value,
}

/// `e` over [R, W] rows of `operands` into `out`, in class `class`.
pub struct Rows<'a> {
    pub e: &'a crate::expr::E,
    pub operands: &'a [Operand],
    pub r: i64,
    pub w: i64,
    pub out: &'a Operand,
    pub bands: Option<i64>,
    pub class: &'a ShapeClass,
    pub schedule: &'a Schedule,
}

/// Trips core `c` of `used` takes of `trips` dealt one at a time.
pub(super) fn core_trips(trips: i64, used: i64, c: &Idx) -> Idx {
    Idx::Int(trips + used - 1).sub(c).div(used)
}

/// The first row of core `c`'s trip `k` of `rows` rows.
pub(super) fn trip_row(c: &Idx, k: &Idx, used: i64, rows: i64) -> Idx {
    c.add(k.mul(used)).mul(rows)
}

/// A kernel's ports and the specs given for them.
pub(super) type Ports = (Vec<Port>, Vec<(String, g::Spec)>);

/// The `in{first + i}` ports of `operands` over [R, W] rows.
pub(super) fn in_ports(operands: &[Operand], first: usize, r: i64, w: i64, hw: &Hw) -> R<Ports> {
    let (mut ports, mut given) = (vec![], vec![]);
    for (i, o) in operands.iter().enumerate() {
        let shape = super::vector::port_shape(o, r, w, hw)?;
        let port = format!("in{}", first + i);
        ports.push(Port::input(&port, &shape, g::Dtype::F16));
        given.push((port, spec(&shape, o.dtype, vec![], g::BufSpace::Dram)?));
    }
    Ok((ports, given))
}

/// `dst`'s every byte 0, from a word of zeros walked over `rows` rows of
/// `words` words.
pub(super) fn zero(t: &mut Trace, dst: &Buf, rows: i64, words: i64, hw: &Hw) -> R<()> {
    let seed = t.buffer(&[hw.lanes], g::Dtype::F16, vec![])?;
    t.fill_value(&seed.whole(), 0, None, None)?;
    let wb = hw.word_bytes;
    t.copy_classed(
        &dst.whole(),
        &seed.whole(),
        (&[(rows, 0), (words, 0)], None),
        (&[(rows, words * wb), (words, wb)], None),
        Some(1),
    )
}

/// A scalar operand's lane, as every row reads it.
pub(super) fn scalar(t: &mut Trace, buf: &Buf, o: &Operand, hw: &Hw) -> R<Arg> {
    let sr = hw.stat_rows();
    let start = o.offset.div_euclid(sr) * sr;
    let l = t.load(&buf.at(&[sl(start, start + sr)])?, false)?;
    Ok(Arg::Val(t.unpack(l, Some(o.offset.rem_euclid(sr)))?))
}

/// Whether `o` reads rows of a [R, W] view.
pub(super) fn rows_major(o: &Operand) -> bool {
    matches!(o.kind, Kind::Full | Kind::Tile)
}
