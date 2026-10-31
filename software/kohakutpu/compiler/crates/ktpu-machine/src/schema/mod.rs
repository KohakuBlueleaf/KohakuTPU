//! The `machine` section of a machine file, one type per section
//! (`docs/spec/stack/machine.md`). Every struct refuses an unknown key, so a
//! new fact in the generator is a new field here.

pub mod card;
pub mod die;
pub mod firmware;
pub mod kinds;
pub mod system;

use serde::{Deserialize, Serialize, Serializer};

pub use card::{Card, Clock, Dram, Host};
pub use die::{Coord, Die, Noc, Staging, UnitDepths, Units};
pub use firmware::{Boot, Debug, Fw, FwMemory, Heap, Package, Queue};
pub use kinds::{Mg, Simt, Sr, SrFacts, SrProfile, Vc, VcTiming};
pub use system::{Engine, Il, Mover, MoverModes, Node};

/// A fact no compile path reads: the runtime's, a generator's or a tool's.
/// Every other fact is in [`crate::Machine::digest`], so a field is keyed
/// unless its type says otherwise. It serialises as null, which keeps it out
/// of the digest and of [`crate::Machine::get`].
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(transparent)]
pub struct Runtime<T>(T);

impl<T> Runtime<T> {
    /// The fact. A compile path never calls this (`compile_reads_no_runtime_fact`).
    pub fn runtime(&self) -> &T {
        &self.0
    }
}

impl<T> Serialize for Runtime<T> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_none()
    }
}

/// Every fact of one image, nested by path.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Facts {
    pub card: Card,
    pub die: Die,
    pub unit: UnitDepths,
    pub sr: Sr,
    pub simt: Runtime<Simt>,
    pub mg: Mg,
    pub vc: Vc,
    pub mover: Mover,
    pub conversion: Conversion,
    pub node: Node,
    pub il: Il,
    pub fw: Fw,
}

/// Reliability policy, independent of conversion geometry and ISA support.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Conversion {
    pub fp16_mx7: Fp16Mx7,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PackingBackend {
    MoverQuant,
    VectorMx7,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum NonfinitePolicy {
    RejectVector,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Fp16Mx7 {
    pub version: u32,
    pub allowed_backends: Vec<PackingBackend>,
    pub preferred_backend: PackingBackend,
    pub nonfinite_policy: Runtime<NonfinitePolicy>,
    #[serde(default = "one_slot")]
    pub vector_slots: i64,
    /// Zero selects all available cores.
    #[serde(default)]
    pub vector_cores: i64,
    #[serde(default)]
    pub vector_looped: bool,
}

fn one_slot() -> i64 {
    1
}
