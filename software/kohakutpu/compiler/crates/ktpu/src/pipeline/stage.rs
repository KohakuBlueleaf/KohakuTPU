//! The stages of a compile and what the pipeline asks of the stack.

use crate::output::{Finding, PackageOut, Scratch};
use ktpu_machine::{Machine, Refusal};
use std::collections::BTreeMap;

/// One cached stage, in pipeline order. Each keeps its own cache directory
/// (`<root>/<name>/`) and version, so an edit to one keeps the others'
/// entries.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Stage {
    /// Tracer graph -> L2 text.
    Graph,
    /// L2 -> L2: the passes.
    Passes,
    /// L2 -> L1: the lowering.
    Lower,
    /// L1 -> L1: the image scheduler.
    Schedule,
    /// L1 -> each node's relocatable package, dispatches in the order the
    /// dispatch scheduler found.
    Order,
    /// L1 -> the static checker's findings.
    Verify,
}

impl Stage {
    pub const ALL: [Stage; 6] = [
        Stage::Graph,
        Stage::Passes,
        Stage::Lower,
        Stage::Schedule,
        Stage::Order,
        Stage::Verify,
    ];

    /// The stage's cache directory name.
    pub fn name(self) -> &'static str {
        match self {
            Stage::Graph => "graph",
            Stage::Passes => "passes",
            Stage::Lower => "lower",
            Stage::Schedule => "schedule",
            Stage::Order => "order",
            Stage::Verify => "verify",
        }
    }
}

/// The stack the pipeline runs: one pure function a stage, typed IR between
/// them, and the text forms the spec fixes (L2 / L1 text) for cache keys and
/// dumps. `Stack` is the stage crates; tests may bring their own.
pub trait Backend: Sync {
    type L2: Send;
    type L1: Send;
    /// Each node's program, built from L1 (not cached: the order and verify
    /// stages are cached on the L1 text they come from).
    type Programs: Sync;

    /// The version of `stage`'s code: part of its cache key; None: the
    /// stage's code has no version and is not cached.
    fn version(&self, stage: Stage) -> Option<String>;

    fn graph_to_l2(&self, graph_json: &str, m: &Machine) -> Result<Self::L2, Refusal>;
    fn parse_l2(&self, text: &str) -> Result<Self::L2, Refusal>;
    fn print_l2(&self, l2: &Self::L2) -> String;
    /// The module's first fn (the kernel compiled when no name is given).
    fn l2_name(&self, l2: &Self::L2) -> Result<String, Refusal>;
    /// The parameters of `fn name.l2`: the buffers its caller passes (every
    /// other parameter of its L1 body is scratch, held in arenas).
    fn caller(&self, l2: &Self::L2, name: &str) -> Result<Vec<String>, Refusal>;
    fn passes(&self, l2: &Self::L2, name: &str, m: &Machine) -> Result<Self::L2, Refusal>;
    fn lower(&self, l2: &Self::L2, name: &str, m: &Machine) -> Result<Self::L1, Refusal>;

    fn parse_l1(&self, text: &str) -> Result<Self::L1, Refusal>;
    fn print_l1(&self, l1: &Self::L1) -> String;
    fn schedule(&self, l1: &Self::L1, m: &Machine) -> Result<Self::L1, Refusal>;

    /// The node programs of `fn name.l1`. `caller`: the parameters the
    /// caller binds, the others scratch in arenas (`Self::scratch`); None:
    /// every parameter the caller's.
    fn programs(
        &self,
        l1: &Self::L1,
        name: &str,
        caller: Option<&[String]>,
        m: &Machine,
    ) -> Result<Self::Programs, Refusal>;
    /// The arenas `programs` holds scratch in.
    fn scratch(&self, progs: &Self::Programs) -> Vec<Scratch>;
    /// Each node's package, relocatable (no address in its bytes), its
    /// dispatches in an order the dispatch scheduler runs to the end.
    fn order(
        &self,
        progs: &Self::Programs,
        m: &Machine,
    ) -> Result<BTreeMap<u32, PackageOut>, Refusal>;
    fn check(&self, progs: &Self::Programs, m: &Machine) -> Result<Vec<Finding>, Refusal>;
    /// Whether a run with `finding` is refused.
    fn refuses(&self, finding: &Finding) -> bool;
}
