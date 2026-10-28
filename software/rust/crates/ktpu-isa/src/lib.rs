//! Unit ISAs and encoders: cluster, vector core, SRU, mover
//! (`docs/spec/stack/isa/*.md`).
//!
//! [`Isa::new`] builds every unit's layout from one [`Machine`]; each typed
//! instruction encodes against it (`ClusterInst::encode(&isa.cluster)`), and
//! a value outside its field is an `Err` naming the clause it breaks.

pub mod layout;
pub mod mutant;
pub mod numerics;
pub mod reloc;
pub mod spec;
pub mod unit;

pub use ktpu_machine::{Coord, Machine, Refusal, UnitKind};
pub use layout::{Decoded, Field, Format, InstSet, W256};
pub use reloc::{addresses, AddressField, Segment, Target};
pub use unit::cluster::{ClusterCfg, ClusterInst, ClusterIsa};
pub use unit::mover::{Level, Move, MoveMode, MoverIsa, MoverOp, RegWrite, Walk};
pub use unit::sru::{SruInst, SruIsa};
pub use unit::vector::{VWord, VecCfg, VecCu, VecIsa};

/// Every unit's encoding for one machine.
#[derive(Clone, Debug)]
pub struct Isa {
    pub cluster: ClusterIsa,
    pub vector: VecIsa,
    pub sru: SruIsa,
    pub mover: MoverIsa,
}

impl Isa {
    pub fn new(m: &Machine) -> Result<Isa, Refusal> {
        Ok(Isa {
            cluster: ClusterIsa::new(m)?,
            vector: VecIsa::new(m)?,
            sru: SruIsa::new(m),
            mover: MoverIsa::new(m),
        })
    }

    /// The encodings as the machine defines them, no mutant applied: what a
    /// model of the hardware decodes against.
    pub fn reference(m: &Machine) -> Result<Isa, Refusal> {
        Ok(Isa {
            cluster: ClusterIsa::of(ClusterCfg::of(m)?),
            vector: VecIsa::of(VecCfg::reference(m)?),
            sru: SruIsa::of(m.die.noc.data_bits),
            mover: MoverIsa::of(m),
        })
    }
}
