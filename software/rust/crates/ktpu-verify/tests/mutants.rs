//! Every checker mutant breaks each spec vector that names it: the golden
//! case `<vector>~<mutant>` checked under the mutant gives other than the
//! vector's expect (`isa.verify`), or another order than the checker's
//! (`l1.order`).
#![cfg(feature = "mutants")]

use std::fs;
use std::path::{Path, PathBuf};

use ktpu_l0::{Facts, Package};
use ktpu_machine::Machine;
use ktpu_verify::{mutant, schedule, spec, Held, Nodes, Target};
use serde_json::Value;

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

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
                    let p = Package::from_bytes(&unhex(m["bound"].as_str().unwrap()), f).unwrap();
                    (mesh.parse().unwrap(), p)
                })
                .collect()
        })
        .collect()
}

#[test]
fn mutants_break_their_vectors() {
    let dir = root().join("golden/verify");
    let mut paths: Vec<PathBuf> = fs::read_dir(&dir)
        .map(|rd| rd.flatten().map(|e| e.path()).collect())
        .unwrap_or_default();
    paths.sort();
    let (mut witnessed, mut fails) = (0, Vec::new());
    for p in &paths {
        if p.extension().is_none_or(|x| x != "json") {
            continue;
        }
        let g: Value = serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap();
        let Some(name) = g["mutant"].as_str() else {
            continue;
        };
        let Some(&name) = mutant::NAMES.iter().find(|&&n| n == name) else {
            continue;
        };
        if g["runs"].is_null() {
            continue;
        }
        let vid = g["vector"].as_str().unwrap();
        let path = root()
            .join("../tests/spec/vectors")
            .join(format!("{vid}.json"));
        let v: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        let m = Machine::named(g["machine"].as_str().unwrap()).unwrap();
        let n = g["target"]["meshes"].as_array().unwrap().len() as u32;
        let t = Target::from_machine(&m, Some(n)).unwrap();
        let runs = runs(&g, &Facts::of(&m));
        let broken = if v["runner"] == "isa.verify" {
            let want_l1 = v["input"]["l1"].as_bool().unwrap_or(false);
            let got = mutant::with(name, || spec::verify_json(&runs, &t, want_l1));
            got != v["expect"]
        } else {
            let first = schedule(&runs[0], &Held::new(), &t).map(|s| s.packages);
            let got = mutant::with(name, || {
                schedule(&runs[0], &Held::new(), &t).map(|s| s.packages)
            });
            got != first
        };
        if broken {
            witnessed += 1;
        } else {
            fails.push(format!("{} does not break {vid}", name));
        }
    }
    eprintln!("checker mutants: {witnessed} witnessed");
    assert!(witnessed > 0 && fails.is_empty(), "{}", fails.join("\n"));
}
