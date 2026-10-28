//! The planner sizes by the machine (2p.3: the machine file is the only
//! source of a unit fact): each fact `Hw` reads follows the machine, the
//! product's tiling follows the cluster, and a machine differing in one
//! fact changes some recorded run's steps or refuses it by clause.

mod common;

use std::panic::{catch_unwind, AssertUnwindSafe};

use common::goldens;
use ktpu_machine::Machine;
use ktpu_plan::hw::Hw;
use ktpu_plan::run::matmul::Mm;
use ktpu_plan::{lower, run, Case};
use serde_json::Value;

fn board() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

fn with(m: &Machine, fact: &str, v: i64) -> Machine {
    m.with_fact(fact, v.into())
        .unwrap_or_else(|e| panic!("{fact} = {v}: {e}"))
}

#[test]
fn every_fact_is_the_machines() {
    let m = board();
    let base = Hw::of(&m);
    type Get = fn(&Hw) -> i64;
    let cases: [(&str, i64, Get); 8] = [
        ("die.word_bytes", 64, |h| h.word),
        ("vc.lanes", 8, |h| h.lanes),
        ("vc.vlmax", 64, |h| h.vlmax),
        ("mg.lanes", 8, |h| h.band),
        ("mg.kblock", 64, |h| h.kblock),
        ("mg.kstep", 4, |h| h.kstep),
        ("mg.tiles", 64, |h| h.acc_subtiles),
        ("mg.l1_bank_entries", 128, |h| h.bank),
    ];
    for (fact, v, get) in cases {
        assert_ne!(
            get(&base),
            v,
            "{fact}: pick a value the board does not have"
        );
        assert_eq!(get(&Hw::of(&with(&m, fact, v))), v, "{fact}");
    }
    assert_eq!(Hw::of(&with(&m, "die.word_bytes", 64)).halves, 32);
}

#[test]
fn the_product_tiles_by_the_cluster() {
    let m = board();
    let mm = |m: &Machine, n: i64| Mm::plan(&Hw::of(m), 1, 256, n, 256, true, None);
    let got = mm(&m, 256).unwrap();
    assert_eq!((got.tm, got.tn, got.nk), (256, 256, 2));
    let e = mm(&with(&m, "mg.kblock", 64), 32).unwrap_err();
    assert_eq!(e.clause, "TG.RUN.019");
    assert!(mm(&m, 32).is_ok());
    let small = mm(&with(&m, "mg.tiles", 64), 256).unwrap();
    assert!(
        small.tn < got.tn,
        "fewer subtiles, narrower N tile: {small:?}"
    );
    let bank = mm(&with(&m, "mg.l1_bank_entries", 16), 256).unwrap();
    assert!(
        bank.tm < got.tm,
        "a smaller bank, a shorter M tile: {bank:?}"
    );
}

/// Each recorded run on `m`: its steps as JSON, or the refusal's clause.
fn outcomes(cases: &[Case], m: &Machine) -> Vec<Result<Value, String>> {
    let mut out = Vec::new();
    for c in cases {
        for r in &c.runs {
            let l = lower::plan(&c.plans[r.plan].uops).expect("a run's ast plans");
            let got = catch_unwind(AssertUnwindSafe(|| {
                run::execute(l, &r.state, m)
                    .map(|x| Value::Array(common::normal(&x.steps, true)))
                    .map_err(|(e, _)| e.clause)
            }));
            out.push(got.unwrap_or_else(|_| Err("panic".into())));
        }
    }
    out
}

#[test]
fn a_machine_differing_in_one_fact_plans_differently() {
    let cases: Vec<Case> = goldens()
        .iter()
        .map(|f| serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap())
        .filter(|c: &Case| c.machine == "multimesh_v9t6" && c.nodes == 1)
        .collect();
    assert!(!cases.is_empty());
    let m = board();
    let base = outcomes(&cases, &m);
    assert!(base.iter().all(|o| o.is_ok()), "the board plans every run");
    let mut bad = Vec::new();
    for (fact, v) in [
        ("die.word_bytes", 64),
        ("vc.lanes", 8),
        ("vc.vlmax", 64),
        ("mg.lanes", 8),
        ("mg.kblock", 64),
        ("mg.tiles", 64),
        ("mg.l1_bank_entries", 16),
    ] {
        let got = outcomes(&cases, &with(&m, fact, v));
        let changed = got.iter().zip(&base).filter(|(a, b)| a != b).count();
        let panics = got.iter().filter(|o| *o == &Err("panic".into())).count();
        eprintln!("{fact} = {v}: {changed} of {} runs change", base.len());
        if changed == 0 {
            bad.push(format!("{fact}: no run changes"));
        }
        if panics > 0 {
            bad.push(format!("{fact}: {panics} runs panic instead of refusing"));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}
