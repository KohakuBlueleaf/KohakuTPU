//! The tier-0 L2 spec vectors (`software/tests/spec/vectors/l2`) through
//! `ktpu_l2::spec`: each ported runner's output equals the vector's
//! `expect`, and with the `mutants` feature every mutant it names changes it.
//! A frozen vector a ruling supersedes runs with its ruled input and output
//! (`spec::SUPERSEDED`).
//! `KTPU_SPEC=REGEX` narrows the vectors.

use std::path::{Path, PathBuf};

use ktpu_l2::spec;
use ktpu_machine::Machine;
use regex::Regex;
use serde_json::Value;

fn vectors() -> Vec<PathBuf> {
    fn walk(d: &Path, out: &mut Vec<PathBuf>) {
        for e in std::fs::read_dir(d).unwrap().flatten() {
            let p = e.path();
            if p.is_dir() {
                walk(&p, out);
            } else if p.extension().map(|x| x == "json").unwrap_or(false) {
                out.push(p);
            }
        }
    }
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/spec/vectors/l2");
    let mut out = Vec::new();
    walk(&dir, &mut out);
    out.sort();
    out
}

/// ISA.VC.183's `pool` (the `isa.vector.params` vector), and its mutant.
#[test]
fn pool_param() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/spec/vectors/isa/vector/ISA.VC.183.pool_param.json");
    let v: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let m = Machine::named(v["machine"].as_str().unwrap()).unwrap();
    assert_eq!(
        spec::run_param("pool", &m, None).unwrap(),
        Some(v["expect"]["pool"].clone())
    );
    assert_eq!(spec::param("vlmax", &m), None);
    if cfg!(feature = "mutants") {
        let got = spec::run_param("pool", &m, Some("isa.vector/links")).unwrap();
        assert_ne!(got, Some(v["expect"]["pool"].clone()));
    }
}

/// ISA.VC.183's landing pool (the `isa.vector.links` vectors), and its
/// mutant.
#[test]
fn links_vectors() {
    for stem in ["ISA.VC.183.pool", "ISA.VC.183.too_big"] {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!(
            "../../../tests/spec/vectors/isa/vector/{stem}.json"
        ));
        let v: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let m = Machine::named(v["machine"].as_str().unwrap()).unwrap();
        let sends: Vec<i64> = v["input"]["sends"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_i64().unwrap())
            .collect();
        assert_eq!(
            spec::run_links(&sends, &m, None).unwrap(),
            v["expect"],
            "{stem}"
        );
        if cfg!(feature = "mutants") {
            let got = spec::run_links(&sends, &m, Some("isa.vector/links")).unwrap();
            assert_ne!(got, v["expect"], "{stem}: isa.vector/links survives");
        }
    }
}

#[test]
fn spec_vectors() {
    let only = std::env::var("KTPU_SPEC").unwrap_or_default();
    let only_re = Regex::new(&only).expect("KTPU_SPEC is a regex");
    let runners = spec::runners();
    let (mut pass, mut killed, mut skipped, mut reversed) = (0, 0, 0, 0);
    let mut fails: Vec<String> = Vec::new();
    let (mut ruled, mut superseded): (Vec<String>, Vec<String>) = (Vec::new(), Vec::new());
    for path in vectors() {
        let name = path.file_stem().unwrap().to_string_lossy().to_string();
        if !only_re.is_match(&name) {
            continue;
        }
        let v: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let runner = v["runner"].as_str().unwrap_or("");
        if v["tier"].as_i64().unwrap_or(0) != 0 || !runners.contains(&runner) {
            skipped += 1;
            continue;
        }
        let (input, expect) = spec::ruled(&name, &v["input"], &v["expect"]);
        let m = Machine::named(v["machine"].as_str().unwrap_or("multimesh_v9t6")).unwrap();
        match spec::run(runner, &input, Some(&m), None) {
            Ok(got) if got == expect => {
                if let Some(s) = spec::SUPERSEDED.iter().find(|s| s.vector == name) {
                    if let (Some(mu), true) = (s.mutant, cfg!(feature = "mutants")) {
                        match spec::run(runner, &input, Some(&m), Some(mu)) {
                            Ok(old) if old == v["expect"] => reversed += 1,
                            got => fails.push(format!(
                                "{name}: under {mu} gives {got:?}, the frozen {}",
                                v["expect"]
                            )),
                        }
                    }
                    superseded.push(name.clone());
                } else if v["status"] == "ruling" {
                    ruled.push(name.clone());
                } else {
                    pass += 1;
                }
            }
            Ok(got) => {
                fails.push(format!("{name}: got {got}, want {expect}"));
                continue;
            }
            Err(e) => {
                fails.push(format!("{name}: {e}"));
                continue;
            }
        }
        if !cfg!(feature = "mutants") {
            continue;
        }
        for mu in v["mutants"].as_array().cloned().unwrap_or_default() {
            let mu = mu.as_str().unwrap_or("");
            if spec::skipped_mutants().iter().any(|(n, _)| *n == mu) {
                continue;
            }
            match spec::run(runner, &input, Some(&m), Some(mu)) {
                Ok(got) if got == expect => fails.push(format!("{name}: mutant {mu} survives")),
                _ => killed += 1,
            }
        }
    }
    eprintln!(
        "l2 spec: {pass} frozen pass, rulings followed {ruled:?}, superseded met with their ruled input {superseded:?} ({reversed} frozen again under their mutant), {killed} mutants killed, {} fail, {skipped} not ported",
        fails.len()
    );
    for f in &fails {
        eprintln!("FAIL {f}");
    }
    assert!(fails.is_empty());
    if only.is_empty() {
        assert_eq!(superseded.len(), spec::SUPERSEDED.len());
        if cfg!(feature = "mutants") {
            let with = spec::SUPERSEDED.iter().filter(|s| s.mutant.is_some());
            assert_eq!(reversed, with.count());
        }
    }
}
