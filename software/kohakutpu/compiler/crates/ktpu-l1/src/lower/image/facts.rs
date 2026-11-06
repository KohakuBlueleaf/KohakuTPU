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
    pub l1_words: u32,
    /// Flits an interlink packet carries (`il.packet_beats`).
    pub packet_beats: u32,
    /// Silicon flaw F-002: the interlink sends a one-flit packet before its
    /// flit shows (`flaws.F-002.present`).
    pub il_tail: bool,
    /// Unit reads must avoid single-beat MAG pieces.
    pub read_single: bool,
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
            l1_words: m.vc.l1_words,
            packet_beats: m.il.packet_beats,
            il_tail: m.flaws.f002.present,
            read_single: m.flaws.f014.present,
        }
    }
}

/// The vector ISA for a core of `regs` registers on `m`.
pub fn isa_for(m: &Machine, regs: u32) -> Result<VecIsa> {
    let mut isa = VecIsa::new(m)?;
    isa.cfg.regs = regs;
    Ok(isa)
}
