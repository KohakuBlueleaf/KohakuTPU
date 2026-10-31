//! The core configuration the encoder tracks (chunk selects, vl, strides) and how two paths' states meet.

use super::*;

/// A chunk select: (base chunk, stride 0 or 1).
pub type Nib = (u32, u32);
pub const DEF_NIB: Nib = (0, 1);
pub const FLAT: (i64, i64) = (1, 4);

/// The core's configuration as far as the encoder knows it; None is unknown.
/// `chk` holds the (base, stride) of a, b, c, d; `xb` is (xm, xk, sh, xc,
/// pm, pr).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct State {
    pub vl: Option<u32>,
    pub chk: [Option<Nib>; 4],
    pub xb: [Option<u32>; 6],
    pub ustr: Option<(i64, i64)>,
    pub pstr: Option<(i64, i64)>,
}

pub const UNKNOWN: State = State {
    vl: None,
    chk: [None; 4],
    xb: [None; 6],
    ustr: None,
    pstr: None,
};

/// After core reset: VL `vlmax`, CHK 0x1111, XB 0, flat walks.
pub fn reset(vlmax: u32) -> State {
    State {
        vl: Some(vlmax),
        chk: [Some(DEF_NIB); 4],
        xb: [Some(0); 6],
        ustr: Some(FLAT),
        pstr: Some(FLAT),
    }
}

pub(super) fn one<T: PartialEq + Copy>(x: Option<T>, y: Option<T>) -> Option<T> {
    if x == y {
        x
    } else {
        None
    }
}

/// The fields two paths agree on.
pub fn meet(a: State, b: State) -> State {
    if on("l1.img/skipz-meet") {
        return a;
    }
    let chk = std::array::from_fn(|i| one(a.chk[i], b.chk[i]));
    let xb = std::array::from_fn(|i| one(a.xb[i], b.xb[i]));
    State {
        vl: one(a.vl, b.vl),
        chk,
        xb,
        ustr: one(a.ustr, b.ustr),
        pstr: one(a.pstr, b.pstr),
    }
}
