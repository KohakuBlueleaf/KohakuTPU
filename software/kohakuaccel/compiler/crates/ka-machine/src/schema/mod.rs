//! The sections of a machine file every KohakuAccel machine has
//! (`docs/spec/stack/machine.md`). Every struct refuses an unknown key, so a
//! new fact in the generator is a new field here.

pub mod card;
pub mod die;
pub mod firmware;
pub mod system;

use serde::{Deserialize, Serialize, Serializer};

pub use card::{Card, Clock, Dram, Host};
pub use die::{Address, Coord, Noc, Staging, UnitDepths};
pub use firmware::{Boot, Debug, Fw, FwMemory, Heap, Package, Queue};
pub use system::{Engine, Il, Mover, MoverModes, Node};

/// A fact no compile path reads: the runtime's, a generator's or a tool's.
/// Every other fact is in a machine's digest, so a field is keyed unless its
/// type says otherwise. It serialises as null, which keeps it out of the
/// digest and of a path read.
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(transparent)]
pub struct Runtime<T>(T);

impl<T> Runtime<T> {
    pub fn new(value: T) -> Runtime<T> {
        Runtime(value)
    }

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
