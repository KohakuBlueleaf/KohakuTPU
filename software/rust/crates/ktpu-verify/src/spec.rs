//! What the `isa.verify` spec runner returns, from built runs.

use std::collections::BTreeSet;

use serde_json::{json, Map, Value};

use crate::check::{check, l1_touched, schedule, Held, Nodes};
use crate::target::Target;

/// Runners hosted here: none; `isa.verify` and `l1.order` take L1 text and
/// are hosted in ktpu-spec on `verify_json`.
pub fn runners() -> Vec<&'static str> {
    Vec::new()
}

pub fn run(
    runner: &str,
    _input: &Value,
    _machine: Option<&ktpu_machine::Machine>,
    _mutant: Option<&str>,
) -> Result<Value, String> {
    Err(format!("no runner {runner} in ktpu-verify"))
}

/// The checker mutants (`mutant::NAMES`), active through `mutant::with`
/// with the `mutants` feature.
pub fn mutants() -> Vec<&'static str> {
    crate::mutant::NAMES.to_vec()
}

pub fn skipped_mutants() -> Vec<(&'static str, &'static str)> {
    Vec::new()
}

/// `{"findings": [kind, ...]}` (sorted, each once) for `runs`; with
/// `want_l1`, also `"l1"`: each vector core of mesh 0 (`vc<index>`) that
/// touched its L1 in the first run alone, its words read and written.
pub fn verify_json(runs: &[Nodes], t: &Target, want_l1: bool) -> Value {
    let kinds: BTreeSet<&str> = check(runs, t).iter().map(|f| f.kind.name()).collect();
    let mut out = Map::new();
    out.insert(
        "findings".into(),
        Value::from(kinds.into_iter().collect::<Vec<_>>()),
    );
    let Some(first) = runs.first() else {
        return Value::Object(out);
    };
    if !want_l1 {
        return Value::Object(out);
    }
    let Ok(s) = schedule(first, &Held::new(), t) else {
        return Value::Object(out);
    };
    let vcs = t.meshes.first().map(|m| m.vc.clone()).unwrap_or_default();
    let mut l1 = Map::new();
    for (name, r, w) in l1_touched(&s.packages, t) {
        let index = vcs
            .iter()
            .position(|&(x, y)| name == format!("vc(0:{x},{y})"));
        if let (Some(i), false) = (index, r.is_empty() && w.is_empty()) {
            l1.insert(format!("vc{i}"), json!({"read": r, "written": w}));
        }
    }
    out.insert("l1".into(), Value::Object(l1));
    Value::Object(out)
}
