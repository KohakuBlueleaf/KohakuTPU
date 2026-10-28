//! `rows.run` called directly (`plan_dump.SYNTHETIC`, the statistic-first
//! path no front end reaches) takes the Python steps; every path
//! `plan_dump.PATHS` names is taken by some golden, each case's paths by
//! the Rust planner too.

mod common;

use std::collections::{BTreeMap, BTreeSet, HashMap};

use common::goldens;
use ktpu_machine::Machine;
use ktpu_plan::exec::Exec;
use ktpu_plan::expr::{self, E};
use ktpu_plan::program::RowsCall;
use ktpu_plan::run::{self, Operand, Runner};
use ktpu_plan::tensor::Session;
use ktpu_plan::uop::Graph;
use ktpu_plan::{class, lower, program, Addr, Case};
use serde_json::Value;

/// Every path the dump records, each a golden must take.
const PATHS: &[&str] = &[
    "run._ungated",
    "run._enumerated",
    "run._lane_copies",
    "rows._stat_first",
    "rows.partials",
];

fn num(v: &Value) -> f64 {
    match v.as_str() {
        Some("inf") => f64::INFINITY,
        Some("-inf") => f64::NEG_INFINITY,
        Some(_) => f64::NAN,
        None => v.as_f64().unwrap(),
    }
}

/// An expression from its node table.
fn expr_of(t: &Value) -> E {
    let mut nodes: Vec<E> = Vec::new();
    for n in t["nodes"].as_array().unwrap() {
        let at = |k: &Value| nodes[k.as_u64().unwrap() as usize].clone();
        let e = if let Some(i) = n.get("load") {
            expr::load(i.as_u64().unwrap() as usize)
        } else if let Some(c) = n.get("const") {
            expr::constant(num(c))
        } else if let Some(r) = n.get("reduce") {
            expr::reduce(r.as_str().unwrap(), at(&n["arg"]))
        } else {
            let args = n["args"].as_array().unwrap().iter().map(at).collect();
            expr::op(n["op"].as_str().unwrap(), args)
        };
        nodes.push(e);
    }
    nodes[t["root"].as_u64().unwrap() as usize].clone()
}

fn operand(v: &Value) -> Operand {
    Operand {
        kind: v["kind"].as_str().unwrap().into(),
        dtype: v["dtype"].as_str().unwrap().into(),
        offset: v["offset"].as_i64().unwrap(),
        pitch: v["pitch"].as_i64().unwrap(),
        rows: v["rows"].as_i64().unwrap(),
    }
}

/// `rows.run` called with a golden call's arguments: the runner after, the
/// refusal as Python prints it.
fn rows_call<'a>(c: &RowsCall, m: &'a Machine) -> (Runner<'a>, Option<String>) {
    let a = &c.args;
    let ops: Vec<Operand> = a["operands"]
        .as_array()
        .unwrap()
        .iter()
        .map(operand)
        .collect();
    let binds: BTreeMap<String, Addr> = serde_json::from_value(a["binds"].clone()).unwrap();
    let out_at: Addr = serde_json::from_value(a["out_at"].clone()).unwrap();
    let mut r = Runner::new(m, Graph::default(), Exec::default());
    let got = r.rows_run(
        &expr_of(&a["e"]["expr"]),
        &ops,
        &binds,
        a["R"].as_i64().unwrap(),
        a["W"].as_i64().unwrap(),
        &operand(&a["out"]),
        &out_at,
        &a["info"],
    );
    let got = got.and(program::compile(&mut r.x.steps, m));
    (r, got.err().map(|e| class::text(&e)))
}

/// The named paths the Rust planner takes replaying `case` (its runs, its
/// direct rows calls, its kp log).
fn rust_paths(case: &Case) -> BTreeSet<String> {
    let m = case.machine();
    let mut out = BTreeSet::new();
    for r in &case.runs {
        let Ok(l) = lower::plan(&case.plans[r.plan].uops) else {
            continue;
        };
        let x = match run::execute(l, &r.state, &m) {
            Ok(x) | Err((_, x)) => x,
        };
        out.extend(x.paths.iter().map(|p| p.to_string()));
    }
    for c in &case.rows {
        out.extend(rows_call(c, &m).0.x.paths.iter().map(|p| p.to_string()));
    }
    let mut s = Session::new(&m);
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
            let (r, err) = rows_call(c, &m);
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
