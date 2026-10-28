//! The image scheduler against `software/rust/golden/sched/` (written by
//! `software/rust/golden/sched_dump.py`): every image's runs, the RUL.063
//! model's order and cycles for each, the reordered image, `timing::cycles`
//! of the written and the reordered image, and what `schedule_module` keeps.

mod clauses;
#[cfg(feature = "mutants")]
mod mutants;
mod vectors;

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use ktpu_l1::sched::{runs, schedule_module, timing, Ruled};
use ktpu_l1::text::{emit_at, read, Expander};
use ktpu_machine::Machine;
use serde_json::Value;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..")
}

fn cases(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in std::fs::read_dir(dir).unwrap_or_else(|e| panic!("{dir:?}: {e}")) {
        let p = e.unwrap().path();
        if p.is_dir() {
            cases(&p, out);
        } else if p.extension().is_some_and(|x| x == "json") {
            out.push(p);
        }
    }
}

fn int(v: &Value) -> i64 {
    v.as_i64().unwrap()
}

fn order(v: &Value) -> Vec<usize> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_u64().unwrap() as usize)
        .collect()
}

/// Every difference between the Rust scheduler and the case.
fn check(case: &Value, machines: &mut HashMap<String, Machine>) -> Vec<String> {
    let name = case["machine"]
        .as_str()
        .unwrap_or("multimesh_v9t6")
        .to_string();
    let m = machines
        .entry(name.clone())
        .or_insert_with(|| Machine::named(&name).unwrap())
        .clone();
    let text = case["text"].as_str().unwrap();
    let module = match read(text, "<sched>") {
        Ok(md) => md,
        Err(e) => return vec![format!("read: {e}")],
    };
    if case["refusal"].is_string() {
        return match schedule_module(&module, &m) {
            Ok(_) => vec![format!(
                "schedule_module accepts; Python refuses: {}",
                case["refusal"]
            )],
            Err(_) => Vec::new(),
        };
    }
    let mut errs = Vec::new();
    if (module.vregs as i64, module.vcopies as i64) != (int(&case["vregs"]), int(&case["copies"])) {
        errs.push("vregs / copies".into());
    }
    let r = module.reader();
    let macros = module.macro_table();
    let ruled = Ruled::new(&r, &m, module.vregs, module.vcopies).unwrap();
    let fill = (m.vc.fill_alone.0 as i64, m.vc.fill_alone.1 as i64);
    let scheduled = schedule_module(&module, &m);
    if let Ok(s) = &scheduled {
        if s.source != case["scheduled_ruled"] {
            errs.push("schedule_module's source differs".into());
        }
    }
    for im in case["images"].as_array().unwrap() {
        let iname = im["name"].as_str().unwrap();
        let img = &module.images[iname];
        let body = match Expander::new(&macros, &r, true).stmts(&img.stmts, &HashMap::new()) {
            Ok(b) => b,
            Err(e) => {
                errs.push(format!("{iname}: expand: {e}"));
                continue;
            }
        };
        let head = format!("image {}({})\n", img.name, img.params.join(", "));
        let written = format!("{head}{}", emit_at(&body, 1, 48));
        if written != im["written"] {
            errs.push(format!("{iname}: written text differs"));
            continue;
        }
        let got_runs = runs(&body);
        let want_runs = im["runs"].as_array().unwrap();
        if got_runs.len() != want_runs.len() {
            errs.push(format!(
                "{iname}: {} runs, want {}",
                got_runs.len(),
                want_runs.len()
            ));
            continue;
        }
        for (k, (run, want)) in got_runs.iter().zip(want_runs).enumerate() {
            let texts: Vec<String> = run
                .iter()
                .map(|st| emit_at(std::slice::from_ref(st), 0, 48).trim().to_string())
                .collect();
            if Value::from(texts) != want["stmts"] {
                errs.push(format!("{iname} run {k}: statements differ"));
                continue;
            }
            let w = &want["ruled"];
            let got = ruled.order(run).unwrap();
            let all: Vec<usize> = (0..run.len()).collect();
            let cw = ruled.cycles(run, &all).unwrap();
            let co = ruled.cycles(run, &got).unwrap();
            if got != order(&w["order"]) || cw != int(&w["written"]) || co != int(&w["cycles"]) {
                errs.push(format!(
                    "{iname} run {k}: order {got:?} cycles {cw} -> {co}, want {} {} -> {}",
                    w["order"], w["written"], w["cycles"]
                ));
            }
        }
        let tuned = format!("{head}{}", emit_at(&ruled.block(&body).unwrap(), 1, 48));
        if tuned != im["ruled"] {
            errs.push(format!("{iname}: reordered image differs"));
        }
        let (regs, copies) = (module.vregs, module.vcopies);
        let tw = timing::cycles(&written, iname, regs, copies, fill, &m).unwrap();
        if tw != int(&im["cycles_written"]) {
            errs.push(format!(
                "{iname}: timing of written {tw}, want {}",
                im["cycles_written"]
            ));
        }
        if tuned != written {
            let tt = timing::cycles(&tuned, iname, regs, copies, fill, &m).unwrap();
            if tt != int(&im["cycles_tuned"]) {
                errs.push(format!(
                    "{iname}: timing of reordered {tt}, want {}",
                    im["cycles_tuned"]
                ));
            }
        }
        match &scheduled {
            Ok(s) => {
                let d = &s.images[iname];
                let kept = format!("{head}{}", emit_at(&d.stmts, 1, 48));
                if kept != im["chosen"] {
                    errs.push(format!("{iname}: schedule_module keeps another image"));
                }
            }
            Err(e) => errs.push(format!("schedule_module: {e}")),
        }
    }
    errs
}

#[test]
fn scheduler_vectors_give_their_ruled_output() {
    let (n, bad, ruled) = vectors::check_all(&root().join("software/rust/golden/sched/vectors"));
    println!("{n} scheduler vectors, RUL.063 supersedes {ruled:?}");
    assert_eq!(n, 7);
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

#[test]
fn every_sched_golden_case_matches() {
    let mut paths = Vec::new();
    cases(&root().join("software/rust/golden/sched"), &mut paths);
    paths.sort();
    // KTPU_SCHED_CASE=<substring> runs only the cases whose file names hold it.
    paths.retain(|p| !p.parent().is_some_and(|d| d.ends_with("vectors")));
    let only = std::env::var("KTPU_SCHED_CASE").ok();
    if let Some(s) = &only {
        paths.retain(|p| p.to_string_lossy().contains(s.as_str()));
    } else {
        assert!(paths.len() >= 200, "only {} cases", paths.len());
    }
    let mut machines = HashMap::new();
    let mut failed = Vec::new();
    let (mut runs_n, mut ruled_n) = (0, 0);
    for p in &paths {
        let case: Value = serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap();
        runs_n += case["images"].as_array().map_or(0, |a| {
            a.iter().map(|i| i["runs"].as_array().unwrap().len()).sum()
        });
        ruled_n += !case["rulings"].as_array().unwrap().is_empty() as usize;
        let errs = check(&case, &mut machines);
        if !errs.is_empty() {
            failed.push(format!("{}:\n  {}", case["case"], errs.join("\n  ")));
        }
    }
    println!(
        "{} cases, {runs_n} runs, {ruled_n} carry RUL.063",
        paths.len()
    );
    assert!(
        failed.is_empty(),
        "{} of {} cases differ:\n{}",
        failed.len(),
        paths.len(),
        failed.join("\n")
    );
}
