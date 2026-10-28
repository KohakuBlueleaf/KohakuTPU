//! Tier-0 spec vectors run against the Rust stack.
//!
//! Each stage crate's `spec` module is a [`Suite`]: the runners it ports
//! (by the Python runner name, returning what that runner returns), a `run`
//! that runs one under a mutant, and the mutants it has a knob for. A vector
//! passes when its answer equals `expect` (frozen and ruling alike: the
//! accepted rulings are spec for the Rust stack); a frozen vector's answer
//! under each mutant it names must differ. Vectors whose runner has no port
//! are skipped by name.

pub mod runners;
pub mod vectors;

use ktpu_machine::Machine;
pub use runners::Suite;
use std::collections::BTreeMap;
pub use vectors::Vector;

/// One vector run.
#[derive(Clone, Debug, PartialEq)]
pub struct Outcome {
    pub id: String,
    pub clause: String,
    /// `frozen` or `ruling`.
    pub status: String,
    /// None: passed; else what differs.
    pub failure: Option<String>,
    /// Run with a ruling's input and output in place of the vector's.
    pub superseded: bool,
    /// Each mutant the vector names: killed, survived, or skipped (why).
    pub mutants: Vec<(String, Witness)>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Witness {
    Killed,
    Survived,
    Skipped(String),
}

/// Every tier-0 vector with a port, run (and, frozen and passing, run under
/// each mutant it names).
pub fn run(vectors: &[Vector]) -> Vec<Outcome> {
    let suites = runners::by_runner();
    vectors
        .iter()
        .filter(|v| v.tier == 0)
        .filter_map(|v| Some((v, *suites.get(v.runner.as_str())?)))
        .map(|(v, s)| outcome(v, s))
        .collect()
}

fn outcome(v: &Vector, s: &Suite) -> Outcome {
    let mut o = Outcome {
        id: v.id.clone(),
        clause: v.clause.clone(),
        status: v.status.clone(),
        failure: None,
        superseded: false,
        mutants: Vec::new(),
    };
    let machine = match v.machine.as_deref().map(Machine::named).transpose() {
        Ok(m) => m,
        Err(e) => {
            o.failure = Some(format!("machine: {e}"));
            return o;
        }
    };
    let stem = v.id.rsplit('/').next().unwrap_or(&v.id);
    let (input, want) = (s.ruled)(stem, &v.input, &v.expect);
    o.superseded = input != v.input || want != v.expect;
    let run = |mutant| (s.run)(&v.runner, &input, machine.as_ref(), mutant);
    o.failure = match run(None) {
        Ok(g) if g == want => None,
        Ok(g) => Some(format!("got {g}\nwant {want}")),
        Err(e) => Some(format!("runner failed: {e}")),
    };
    if o.failure.is_some() || v.status != "frozen" {
        return o;
    }
    let knobs = (s.mutants)();
    let skipped: BTreeMap<_, _> = (s.skipped_mutants)().into_iter().collect();
    for m in &v.mutants {
        let w = if knobs.contains(&m.as_str()) {
            match run(Some(m)) {
                Ok(g) if g == want => Witness::Survived,
                _ => Witness::Killed,
            }
        } else {
            let why = skipped.get(m.as_str()).copied();
            Witness::Skipped(why.unwrap_or("no Rust knob, not listed").to_string())
        };
        o.mutants.push((m.clone(), w));
    }
    o
}

/// `{runner: tier-0 vector count}` of runners with a port and without one.
pub fn coverage(vectors: &[Vector]) -> (BTreeMap<String, usize>, BTreeMap<String, usize>) {
    let suites = runners::by_runner();
    let (mut with, mut without) = (BTreeMap::new(), BTreeMap::new());
    for v in vectors.iter().filter(|v| v.tier == 0) {
        let side = if suites.contains_key(v.runner.as_str()) {
            &mut with
        } else {
            &mut without
        };
        *side.entry(v.runner.clone()).or_insert(0) += 1;
    }
    (with, without)
}

/// The names of tier-0 runners with no port.
pub fn unported(vectors: &[Vector]) -> Vec<String> {
    coverage(vectors).1.into_keys().collect()
}
