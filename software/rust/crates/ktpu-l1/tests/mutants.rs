//! Every mutant a frozen, ported L1 vector names (`--features mutants`):
//! each with a knob must change the vector's answer, and each without one
//! must be listed with its reason.

#![cfg(feature = "mutants")]

mod common;

use ktpu_l1::spec;

#[test]
fn every_named_mutant_breaks_its_vector() {
    let ported = spec::runners();
    let knobs = spec::mutants();
    let skipped: Vec<&str> = spec::skipped_mutants().into_iter().map(|s| s.0).collect();
    let (mut killed, mut survived, mut missing, mut skips) = (0, Vec::new(), Vec::new(), 0);
    let mut named = std::collections::BTreeSet::new();
    for (name, v) in common::vectors("l1", "") {
        let runner = v["runner"].as_str().unwrap();
        if !ported.contains(&runner) || v["status"] != "frozen" {
            continue;
        }
        let m = common::machine_of(&v);
        let want = &v["expect"];
        let run = |mutant| spec::run(runner, &v["input"], m.as_ref(), mutant);
        if run(None).as_ref() != Ok(want) {
            continue;
        }
        for mu in v["mutants"].as_array().into_iter().flatten() {
            let mu = mu.as_str().unwrap();
            named.insert(mu.to_string());
            if knobs.contains(&mu) {
                match run(Some(mu)) {
                    Ok(g) if &g == want => survived.push(format!("{name} ~ {mu}")),
                    _ => killed += 1,
                }
            } else if skipped.contains(&mu) {
                skips += 1;
            } else {
                missing.push(format!("{name} ~ {mu}"));
            }
        }
    }
    let unused: Vec<&&str> = knobs.iter().filter(|k| !named.contains(**k)).collect();
    println!(
        "killed {killed}, skipped {skips}; survived {survived:?}; no knob {missing:?}; knobs no vector names {unused:?}"
    );
    assert!(survived.is_empty() && missing.is_empty());
}
