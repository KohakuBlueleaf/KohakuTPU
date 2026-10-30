//! The back-to-back run's joined totals are a totals-free run's, on every
//! verify golden case with runs, from `check` and from `check_scheduled`.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use ktpu_l0::Facts;
use ktpu_machine::Machine;
use serde_json::Value;

use super::{from_l0, ordered, run, schedule, Held, Nodes, Totals};
use crate::target::Target;

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("hex"))
        .collect()
}

/// Each run's bound packages per mesh.
fn runs(g: &Value, f: &Facts) -> Vec<Nodes> {
    g["runs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|run| {
            run.as_object()
                .unwrap()
                .iter()
                .map(|(mesh, m)| {
                    let bytes = unhex(m["bound"].as_str().unwrap());
                    (
                        mesh.parse().unwrap(),
                        ktpu_l0::Package::from_bytes(&bytes, f).unwrap(),
                    )
                })
                .collect()
        })
        .collect()
}

#[test]
fn joined_totals_are_a_prepass_totals() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/verify");
    let mut paths: Vec<PathBuf> = fs::read_dir(&dir)
        .map(|rd| rd.flatten().map(|e| e.path()).collect())
        .unwrap_or_default();
    paths.sort();
    let (mut compared, mut fails) = (0, Vec::new());
    for p in &paths {
        if p.extension().is_none_or(|x| x != "json") {
            continue;
        }
        let g: Value = serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap();
        if g["runs"].is_null() {
            continue;
        }
        let m = Machine::named(g["machine"].as_str().unwrap()).unwrap();
        let n = g["target"]["meshes"].as_array().unwrap().len() as u32;
        let t = Target::from_machine(&m, Some(n)).unwrap();
        let runs = runs(&g, &Facts::of(&m));
        let mut joined = vec![ordered(
            Held::new(),
            BTreeMap::new(),
            Totals::new(),
            &runs,
            &t,
        )];
        if let Ok(first) = schedule(&runs[0], &Held::new(), &t) {
            let pkgs = first
                .packages
                .iter()
                .map(|(&m, p)| (m, vec![from_l0(p)]))
                .collect();
            joined.push(ordered(first.held, pkgs, first.totals, &runs[1..], &t));
        }
        for (pkgs, totals) in joined.into_iter().flatten() {
            compared += 1;
            if run(&pkgs, &t, None).tally() != totals {
                fails.push(format!("{}", g["case"]));
            }
        }
    }
    eprintln!("joined totals: {compared} back-to-back runs");
    assert!(compared > 0 && fails.is_empty(), "{}", fails.join("\n"));
}
