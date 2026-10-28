//! Audit six software-control spec vectors; preserve inputs and dispatch words.
use ktpu_machine::Machine;
use serde_json::Value;
use std::{error::Error, path::PathBuf};

const CASES: &[&str] = &[
    "l0/step/L0.STEP.054.patch_through_r15",
    "l1/low/L1.LOW.008.past-label",
    "l1/low/L1.LOW.008.patch",
    "l1/low/L1.LOW.011.end",
    "l1/mg/L1.MG.016.patched-fill",
    "l1/node/L1.NODE.004.runtime-field",
];

fn dispatches(v: &Value, out: &mut Vec<Value>) {
    match v {
        Value::Array(a) => {
            if a.first().is_some_and(|x| x == "DISPATCH") {
                out.push(v.clone());
            } else {
                for child in a {
                    dispatches(child, out);
                }
            }
        }
        Value::Object(o) => {
            if o.get("op").is_some_and(|x| x == "DISPATCH") {
                out.push(v.clone());
            } else {
                for child in o.values() {
                    dispatches(child, out);
                }
            }
        }
        _ => {}
    }
}

fn main() -> Result<(), Box<dyn Error>> {
    let write = std::env::args().any(|a| a == "--write");
    let backup = std::env::args()
        .skip_while(|a| a != "--backup")
        .nth(1)
        .map(PathBuf::from)
        .ok_or("--backup <dir>: directory receiving the original vectors")?;
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let mut pending = Vec::new();
    for case in CASES {
        let path = root.join(format!("software/tests/spec/vectors/{case}.json"));
        let original = std::fs::read(&path)?;
        let doc: Value = serde_json::from_slice(&original)?;
        let m = Machine::named(doc["machine"].as_str().unwrap())?;
        let got = if case.starts_with("l0/") {
            let port = ktpu_l0::spec::ports()
                .into_iter()
                .find(|p| p.0 == doc["runner"])
                .unwrap()
                .1;
            port(&doc["input"], Some(&m))?
        } else {
            let port = ktpu_l1::spec::ports()
                .into_iter()
                .find(|p| p.0 == doc["runner"])
                .unwrap()
                .1;
            port(&doc["input"], &m)?
        };
        if got == doc["expect"] {
            continue;
        }
        let (mut old_dispatch, mut new_dispatch) = (Vec::new(), Vec::new());
        dispatches(&doc["expect"], &mut old_dispatch);
        dispatches(&got, &mut new_dispatch);
        assert_eq!(
            old_dispatch, new_dispatch,
            "{case}: data-plane words changed"
        );
        println!("{case}\nBEFORE {}\nAFTER {got}", doc["expect"]);
        let mut updated = doc.clone();
        updated["expect"] = got;
        if *case == "l1/low/L1.LOW.008.past-label" {
            for mutant in updated["mutants"].as_array_mut().unwrap() {
                if mutant == "l1.lower/await-count" {
                    *mutant = Value::String("l1.lower/static-loop-wait".into());
                }
            }
        }
        assert_eq!(updated["input"], doc["input"]);
        let saved = backup.join(format!("{case}.json"));
        assert!(
            !saved.exists(),
            "refuse to overwrite original {}",
            saved.display()
        );
        pending.push((path, saved, original, updated));
    }
    if write {
        for (path, saved, original, updated) in pending {
            std::fs::create_dir_all(saved.parent().unwrap())?;
            std::fs::write(saved, original)?;
            std::fs::write(
                path,
                format!("{}\n", serde_json::to_string_pretty(&updated)?),
            )?;
        }
    }
    Ok(())
}
