//! What an image reads of the machine: the core's facts and its ISA, and how unit names resolve.

use super::*;

/// A unit name resolved on the program's machine: `(mesh, (x, y))`; `home`
/// is the image's mesh.
pub trait Units {
    fn resolve(&self, st: &Stmt, t: &Term) -> Result<(u32, (u32, u32))>;
    fn home(&self) -> u32;
}

/// The core facts the encoder checks against.
#[derive(Clone, Debug)]
pub struct CoreFacts {
    pub regs: u32,
    pub sregs: u32,
    pub kregs: u32,
    pub preds: u32,
    pub marks: u32,
    pub descriptors: u32,
    pub dims: u32,
    pub chunks: u32,
    pub vlmax: u32,
    pub ainc_bits: u32,
    /// Whether the unpack and pack engines have a second queue (`vc.dualq`).
    pub dualq: bool,
}

impl CoreFacts {
    pub fn of(m: &Machine, regs: u32) -> CoreFacts {
        CoreFacts {
            regs,
            sregs: m.vc.sregs,
            kregs: m.vc.kregs,
            preds: m.vc.preds,
            marks: m.vc.marks,
            descriptors: m.vc.descriptors,
            dims: m.vc.descriptor_dims,
            chunks: if on("l1.img/chunks") { 4 } else { m.vc.chunks },
            vlmax: m.vc.vlmax,
            ainc_bits: m.vc.ainc_bits,
            dualq: m.vc.dualq != 0,
        }
    }
}

/// The vector ISA for a core of `regs` registers on `m`.
pub fn isa_for(m: &Machine, regs: u32) -> Result<VecIsa> {
    let mut isa = VecIsa::new(m)?;
    isa.cfg.regs = regs;
    Ok(isa)
}
