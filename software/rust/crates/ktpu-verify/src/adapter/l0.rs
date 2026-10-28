//! `ktpu_l0` packages in the checker's own form, and steps back.

use ktpu_l0::{Op, Package as L0Package, Step as L0Step, Word};

use super::package::{Package, Step, Unit};
use super::u256::U256;

/// `p` as the checker reads it: units, steps and payloads.
pub fn from_l0(p: &L0Package) -> Package {
    Package {
        units: p
            .units
            .iter()
            .map(|u| Unit {
                ty: u.type_code as u16,
                x: u.x as u8,
                y: u.y as u8,
                mesh: u.mesh as u8,
                credit: u.credit as u32,
                fetch: u.fetch as u32,
            })
            .collect(),
        steps: p
            .steps
            .iter()
            .map(|s| Step::new(s.op as u8, s.unit as u16, s.count as u32, s.arg, s.flags))
            .collect(),
        payloads: p.payloads.iter().map(|w| U256(w.0)).collect(),
    }
}

/// A step of the checker's back as an L0 step.
pub fn to_l0(s: &Step) -> L0Step {
    let op = Op::from_code(s.op as u64).unwrap_or(Op::End);
    L0Step::new(op, s.unit as u64, s.count as u64, s.arg, s.flags)
}

/// A payload word as L0 holds it.
pub fn word(w: U256) -> Word {
    Word(w.0)
}
