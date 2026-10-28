//! The spec vectors whose runner ktpu-kernels ports (`ktpu_kernels::spec`):
//! each passes, and (with the `mutants` feature) fails under every mutant
//! it names.

use ktpu_kernels::spec;
use ktpu_machine::Machine;
use serde_json::Value;
use std::path::{Path, PathBuf};

fn vectors(dir: &Path, out: &mut Vec<(PathBuf, Value)>) {
    for e in std::fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            vectors(&p, out);
        } else if p.extension().is_some_and(|x| x == "json") {
            let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
            if v["runner"]
                .as_str()
                .is_some_and(|r| spec::runners().contains(&r))
            {
                out.push((p, v));
            }
        }
    }
}

#[test]
fn ported_vectors_pass_and_their_mutants_break_them() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/spec/vectors");
    let mut vs = vec![];
    vectors(&root, &mut vs);
    assert_eq!(
        vs.len(),
        10,
        "5 l2.lanes, 4 isa.cluster.plan, 1 num.stat.words"
    );
    let (mut witnessed, mut fails) = (0, vec![]);
    for (path, v) in &vs {
        let name = path.file_name().unwrap().to_string_lossy();
        let m = v["machine"].as_str().map(|b| Machine::named(b).unwrap());
        let runner = v["runner"].as_str().unwrap();
        let got = spec::run(runner, &v["input"], m.as_ref(), None);
        if got.as_ref() != Ok(&v["expect"]) {
            fails.push(format!("{name}: {got:?}"));
        }
        if cfg!(feature = "mutants") {
            for mu in v["mutants"].as_array().into_iter().flatten() {
                let mu = mu.as_str().unwrap();
                match spec::run(runner, &v["input"], m.as_ref(), Some(mu)) {
                    Ok(g) if g == v["expect"] => fails.push(format!("{name}: {mu} breaks nothing")),
                    _ => witnessed += 1,
                }
            }
        }
    }
    eprintln!("{} vectors, {witnessed} mutant witnesses", vs.len());
    assert!(fails.is_empty(), "{fails:#?}");
}
