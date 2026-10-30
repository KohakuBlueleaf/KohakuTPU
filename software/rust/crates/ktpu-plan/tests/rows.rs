//! `rows.run` called directly (`plan_dump.SYNTHETIC`, the statistic-first
//! path no front end reaches) takes the Python steps; every path
//! `plan_dump.PATHS` names is taken by some golden, each case's paths by
//! the Rust planner too.

mod common;

use std::collections::{BTreeSet, HashMap};

use common::{goldens, knobs, rows_call};
use ktpu_plan::run;
use ktpu_plan::tensor::Session;
use ktpu_plan::{lower, Case};
use serde_json::Value;

/// Every path the dump records, each a golden must take.
const PATHS: &[&str] = &[
    "run._ungated",
    "run._enumerated",
    "run._lane_copies",
    "rows._stat_first",
    "rows.partials",
];

/// The named paths the Rust planner takes replaying `case` at its knobs
/// (its runs, its direct rows calls, its kp log).
fn rust_paths(case: &Case) -> BTreeSet<String> {
    let m = case.machine();
    let k = knobs(&case.case);
    let mut out = BTreeSet::new();
    for r in &case.runs {
        let Ok(l) = lower::plan(&case.plans[r.plan].uops) else {
            continue;
        };
        let x = match run::execute_with(l, &r.state, &m, k) {
            Ok(x) | Err((_, x)) => x,
        };
        out.extend(x.paths.iter().map(|p| p.to_string()));
    }
    for c in &case.rows {
        out.extend(rows_call(c, &m, k).0.x.paths.iter().map(|p| p.to_string()));
    }
    let mut s = Session::new(&m);
    s.r.knobs = k;
    let mut names: HashMap<String, usize> = HashMap::new();
    for k in &case.kp {
        if let (Ok(got), Some(name)) = (s.replay(&k.op, &k.args, &names), &k.out) {
            if let Some(t) = got.out {
                names.entry(name.clone()).or_insert(t);
            }
        }
    }
    out.extend(s.r.x.paths.iter().map(|p| p.to_string()));
    out
}

#[test]
fn every_direct_rows_call_takes_the_python_steps() {
    let (mut calls, mut bad) = (0, Vec::new());
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        let m = case.machine();
        for (i, c) in case.rows.iter().enumerate() {
            calls += 1;
            let (r, err) = rows_call(c, &m, knobs(&case.case));
            let (have, want) = (
                common::normal(&r.x.steps, true),
                common::want(&c.steps, true),
            );
            if have != want || err != c.error {
                let at = have
                    .iter()
                    .zip(&want)
                    .position(|(x, y)| x != y)
                    .unwrap_or(have.len().min(want.len()));
                bad.push(format!(
                    "{} call {i}: step {at} of {}/{}: {}; error {err:?} want {:?}",
                    case.case,
                    have.len(),
                    want.len(),
                    common::first_diff(
                        have.get(at).unwrap_or(&Value::Null),
                        want.get(at).unwrap_or(&Value::Null)
                    ),
                    c.error
                ));
            }
        }
    }
    assert!(calls >= 2, "{calls} direct rows calls");
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

/// `plan_dump.SYNTHETIC`: each case and the path it is written to reach.
const SYNTHETIC: &[(&str, &str)] = &[
    ("synthetic.ungated", "run._ungated"),
    ("synthetic.enumerated", "run._enumerated"),
    ("synthetic.lane_copies", "run._lane_copies"),
    ("synthetic.stat_first", "rows._stat_first"),
    ("synthetic.stat_first_two", "rows._stat_first"),
];

#[test]
fn every_synthetic_case_takes_its_path_in_both_planners() {
    let mut bad = Vec::new();
    for (name, path) in SYNTHETIC {
        let f = goldens()
            .into_iter()
            .find(|f| f.file_stem().is_some_and(|s| s == *name));
        let Some(f) = f else {
            bad.push(format!("{name}: no golden"));
            continue;
        };
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        let rust = rust_paths(&case);
        if !case.paths.iter().any(|p| p == path) || !rust.contains(*path) || case.error.is_some() {
            bad.push(format!(
                "{name}: Python {:?}, Rust {rust:?}, error {:?}",
                case.paths, case.error
            ));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

#[test]
fn every_case_takes_the_python_paths() {
    let mut bad = Vec::new();
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        let want: BTreeSet<String> = case.paths.iter().cloned().collect();
        let have = rust_paths(&case);
        if have != want {
            bad.push(format!("{}: Rust {have:?}, Python {want:?}", case.case));
        }
    }
    assert!(bad.is_empty(), "{} differ:\n{}", bad.len(), bad.join("\n"));
}

#[test]
fn every_recorded_path_is_taken_by_a_golden() {
    let mut taken: Vec<String> = Vec::new();
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        taken.extend(case.paths);
    }
    let missing: Vec<&&str> = PATHS
        .iter()
        .filter(|p| !taken.iter().any(|t| t == **p))
        .collect();
    assert!(missing.is_empty(), "no golden takes {missing:?}");
}
