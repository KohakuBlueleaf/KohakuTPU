//! What the framework reads of a machine: the sections every KohakuAccel
//! machine file has, canonical JSON, refusals, and [`MachineFacts`], the view
//! a project's machine gives the framework crates.

pub mod canon;
pub mod refusal;
pub mod schema;

use std::collections::BTreeMap;

pub use refusal::{refuse, Refusal};
pub use schema::*;

/// The schema files this crate defines, in file-name order.
pub const SCHEMA_SOURCES: [(&str, &str); 5] = [
    ("card.rs", include_str!("schema/card.rs")),
    ("die.rs", include_str!("schema/die.rs")),
    ("firmware.rs", include_str!("schema/firmware.rs")),
    ("mod.rs", include_str!("schema/mod.rs")),
    ("system.rs", include_str!("schema/system.rs")),
];

/// A project's machine as the framework reads it.
pub trait MachineFacts {
    fn machine_name(&self) -> &str;
    /// The node count of the card (its meshes).
    fn node_count(&self) -> u32;
    fn fw(&self) -> &Fw;
    fn node(&self) -> &Node;
    fn unit_depths(&self) -> &UnitDepths;
    fn il(&self) -> &Il;
    fn mover(&self) -> &Mover;
    fn noc(&self) -> &Noc;
    fn word_bytes(&self) -> u32;
    fn fetch_port(&self) -> Coord;
    /// Unit type name (`"MG"`) -> coordinates, the units a package may name.
    fn package_units(&self) -> BTreeMap<String, Vec<Coord>>;
    /// The project's unit fault code names.
    fn unit_faults(&self) -> &BTreeMap<String, u32>;
}
