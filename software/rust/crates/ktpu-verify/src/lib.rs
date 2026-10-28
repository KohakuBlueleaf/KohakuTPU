//! Static verifier over L0 packages (l1/program.md s15, verify.md).
//!
//! A kernel is checked as the runs it makes: each run's bound
//! `ktpu_l0::Package` per mesh, as built (unordered). `schedule` orders each
//! segment's dispatches on the abstract machine; `check` schedules every run
//! against the vector cores the runs before left and runs them back to back,
//! returning every finding, each named by its clause; `gate` refuses a run
//! with a finding of a refusing kind, with findings cached by `key`.

pub mod adapter;
pub mod check;
mod cluster;
pub mod lines;
pub mod lint;
pub mod model;
mod vector;
pub use lint::lint;
pub mod mutant;
pub(crate) mod sim;
pub mod spec;
pub mod target;

pub use adapter::u256::U256;
pub use check::gate::{gate, key, overlaps, GateError, Mode, SOURCE_DIGEST as VERSION};
pub use check::{
    check, check_many, check_scheduled, l1_touched, schedule, Held, HeldCore, Nodes, ScheduleError,
    Scheduled,
};
pub use model::finding::{Finding, Kind};
pub use target::{Facts, MeshUnits, Target, UnitKind};

/// The finding kinds a run is refused for (L1.VER.015).
pub const REFUSE: [&str; 9] = [
    "deadlock", "rest", "port", "race", "bounds", "route", "line", "cluster", "vector",
];
