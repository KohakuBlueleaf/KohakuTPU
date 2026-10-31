//! The spec vectors' mutants ktpu-plan carries (`runners/api.py` `@mutant`):
//! each vector's answer or plan golden, replayed with each mutant it lists
//! active, plans or runs differently from the Python planner (or panics);
//! every listed `tg.lower/*` and `tg.run/*` mutant is one ktpu-plan carries.
#![cfg(feature = "mutants")]

mod common;

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};

use common::goldens;
use ktpu_machine::Machine;
use ktpu_plan::{class, lower, mutant, run, spec, Case, Step};
use serde_json::Value;

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn normal(steps: &[Step]) -> Vec<Value> {
    common::normal(steps, true)
}

/// Whether `case` replays as the Python planner recorded it.
fn same(case: &Case, m: &Machine) -> bool {
    for p in &case.plans {
        let ok = match (lower::plan(&p.uops), &p.plan, &p.refused) {
            (Ok(l), Some(want), _) => lower::plan_json(&l.graph, &l.plan) == *want,
            (Err(r), _, Some(msg)) => {
                r.message == *msg && p.refused_class.as_deref() == Some(class::of(&r.clause))
            }
            _ => false,
        };
        if !ok {
            return false;
        }
    }
    for r in &case.runs {
        let Ok(l) = lower::plan(&case.plans[r.plan].uops) else {
            return false;
        };
        let (x, err) = match run::execute_with(l, &r.state, m, common::knobs(&case.case)) {
            Ok(x) => (x, None),
            Err((e, x)) => (x, Some(e)),
        };
        let ok_err = match (&err, &r.error) {
            (None, None) => true,
            (Some(e), Some(w)) => *w == class::text(e),
            _ => false,
        };
        let bases: Vec<String> = r.end.keys().cloned().collect();
        if !ok_err || normal(&x.steps) != common::want(&r.steps, true) || x.records(&bases) != r.end
        {
            return false;
        }
    }
    true
}

/// Whether `case` replays differently (or panics) with mutant `name` active.
fn kills(name: &'static str, case: &Case, m: &Machine) -> bool {
    if name == "memory/unsafe-bounds" {
        return case.runs.iter().any(|r| {
            let run = || {
                let l = lower::plan(&case.plans[r.plan].uops).unwrap();
                let x = match run::execute(l, &r.state, m) {
                    Ok(x) => x,
                    Err((_, x)) => x,
                };
                normal(&x.steps)
            };
            let plain = run();
            let changed = mutant::with(name, run);
            plain != changed
        });
    }
    let replayed = catch_unwind(AssertUnwindSafe(|| mutant::with(name, || same(case, m))));
    !matches!(replayed, Ok(true))
}

/// The tensor API's dtype check (NUM.MEM.001): no golden makes an int
/// tensor, so its mutant is killed by the call itself.
const INT_OK: &str = "num.api/int-tensor-accepted";

/// An int32 tensor is refused (API.TEN.001) and accepted only under `name`.
fn int_tensor_accepted_only_under(name: &'static str) -> bool {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let make = || {
        let host = ktpu_plan::input::Digest {
            bytes: 16,
            sha256: String::new(),
        };
        ktpu_plan::tensor::Session::new(&m)
            .tensor(&[4], "int32", host)
            .map_err(|r| r.clause)
    };
    make() == Err("API.TEN.001".into()) && mutant::with(name, make).is_ok()
}

/// A vector's golden kills each carried mutant it lists where the golden
/// replays what the mutant changes (plans for `tg.lower`, runs for the
/// others); every carried mutant is killed by its vectors or by the test
/// files' goldens (`test_tinygrad`, `test_tensor`).
#[test]
fn every_listed_mutant_changes_its_vectors_plans_or_runs() {
    let cases: Vec<Case> = goldens()
        .iter()
        .map(|f| serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap())
        .collect();
    let mut killed: HashMap<&str, Vec<String>> = HashMap::new();
    let mut bad = Vec::new();
    for case in &cases {
        let vector = root()
            .join("../tests/spec/vectors")
            .join(format!("{}.json", case.from));
        let Ok(text) = std::fs::read_to_string(&vector) else {
            continue;
        };
        let v: Value = serde_json::from_str(&text).unwrap();
        let m = case.machine();
        for name in v["mutants"].as_array().into_iter().flatten() {
            let name = name.as_str().unwrap();
            let Some(name) = mutant::NAMES.iter().copied().find(|n| *n == name) else {
                if name.starts_with("tg.lower/") || name.starts_with("tg.run/") {
                    bad.push(format!("{}: {name} is not carried", case.case));
                }
                continue;
            };
            let replays = if name.starts_with("tg.lower/") {
                !case.plans.is_empty()
            } else {
                !case.runs.is_empty()
            };
            let answer = |mutant| spec::run("tg.run", &v["input"], None, mutant);
            let moved = v["runner"] == "tg.run" && answer(Some(name)) != answer(None);
            if moved || kills(name, case, &m) {
                killed.entry(name).or_default().push(case.case.clone());
            } else if replays {
                bad.push(format!("{}: {name} survives", case.case));
            }
        }
    }
    for name in mutant::NAMES.iter().copied() {
        if killed.contains_key(name) {
            continue;
        }
        for case in cases
            .iter()
            .filter(|c| name == "memory/unsafe-bounds" || c.from.starts_with("test_"))
        {
            let m = case.machine();
            if kills(name, case, &m) {
                killed.entry(name).or_default().push(case.case.clone());
                break;
            }
        }
    }
    if int_tensor_accepted_only_under(INT_OK) {
        killed
            .entry(INT_OK)
            .or_default()
            .push("int32 tensor".into());
    }
    for name in mutant::NAMES {
        eprintln!("{name}: killed by {:?}", killed.get(name));
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
    let alive: Vec<&str> = mutant::NAMES
        .iter()
        .copied()
        .filter(|n| !killed.contains_key(n))
        .collect();
    assert!(alive.is_empty(), "carried but never killed: {alive:?}");
}
