//! `ktpu_plan::spec` against the tier-0 `tg.plan` / `tg.run` vectors: each
//! answers its `expect` (`grep` / `params` left out: they need the
//! pipeline); with the `mutants` feature each frozen vector's
//! answer under every mutant it names that ktpu-plan carries differs.

use std::path::{Path, PathBuf};

use ktpu_plan::spec;
use serde_json::Value;

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, out);
        } else if p.extension().is_some_and(|x| x == "json") {
            out.push(p);
        }
    }
}

fn vectors() -> Vec<(String, Value)> {
    let mut files = Vec::new();
    walk(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/spec/vectors"),
        &mut files,
    );
    files.sort();
    files
        .into_iter()
        .filter_map(|f| {
            let v: Value = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
            let ported = v["runner"]
                .as_str()
                .is_some_and(|r| spec::runners().contains(&r));
            (ported && v["tier"] == 0)
                .then(|| (f.file_stem().unwrap().to_string_lossy().into_owned(), v))
        })
        .collect()
}

fn needs_pipeline(v: &Value) -> bool {
    v["input"]["facts"]
        .as_array()
        .is_some_and(|fs| fs.iter().any(|f| f == "grep" || f == "params"))
}

#[test]
fn every_ported_vector_answers_its_expect() {
    let (mut pass, mut piped, mut bad) = (0, Vec::new(), Vec::new());
    for (id, v) in vectors() {
        let got = spec::run(v["runner"].as_str().unwrap(), &v["input"], None, None);
        let mut want = v["expect"].clone();
        if let Some(o) = want.as_object_mut() {
            o.remove("grep");
            o.remove("params");
        }
        match got {
            Ok(g) if g == want && needs_pipeline(&v) => piped.push(id),
            Ok(g) if g == want => pass += 1,
            Ok(g) => bad.push(format!("{id}: got {g}\n  want {want}")),
            Err(e) => bad.push(format!("{id}: {e}")),
        }
    }
    eprintln!("{pass} vectors answer their expect; needing the pipeline: {piped:?}");
    assert!(bad.is_empty(), "{} differ:\n{}", bad.len(), bad.join("\n"));
    assert!(pass >= 30, "{pass} vectors");
}

#[cfg(feature = "mutants")]
#[test]
fn every_carried_mutant_breaks_its_frozen_vectors() {
    let knobs = spec::mutants();
    let skipped: Vec<&str> = spec::skipped_mutants().iter().map(|(n, _)| *n).collect();
    let (mut killed, mut bad) = (0, Vec::new());
    for (id, v) in vectors() {
        if v["status"] != "frozen" || needs_pipeline(&v) {
            continue;
        }
        let runner = v["runner"].as_str().unwrap();
        for m in v["mutants"].as_array().into_iter().flatten() {
            let m = m.as_str().unwrap();
            if !knobs.contains(&m) {
                if !skipped.contains(&m) {
                    bad.push(format!("{id}: {m} is neither carried nor skipped"));
                }
                continue;
            }
            match spec::run(runner, &v["input"], None, Some(m)) {
                Ok(g) if g == v["expect"] => bad.push(format!("{id}: {m} survives")),
                _ => killed += 1,
            }
        }
    }
    eprintln!("{killed} vector mutants killed");
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}
