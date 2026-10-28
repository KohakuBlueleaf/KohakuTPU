//! The tier-0 scheduler vectors (`l1.block`, `l1.cost`, `l1.schedule`)
//! against `golden/sched/vectors/`: each gives the RUL.063 model's output
//! the dump recorded, which is the vector's expect where the ruling leaves it.

use std::collections::BTreeMap;

use ktpu_l1::sched::{schedule_module, Ruled};
use ktpu_l1::text::{emit, read};
use ktpu_machine::Machine;
use serde_json::{json, Value};

fn lines(text: &str) -> Value {
    json!(text.lines().collect::<Vec<_>>())
}

/// The runner's output under the RUL.063 model.
pub fn run(runner: &str, inp: &Value, m: &Machine) -> Value {
    let module = read(inp["text"].as_str().unwrap(), "<ktpu>").unwrap();
    let r = module.reader();
    let ruled = Ruled::new(&r, m, module.vregs, module.vcopies).unwrap();
    match runner {
        "l1.block" => {
            let img = &module.images[inp["image"].as_str().unwrap()];
            lines(&emit(&ruled.block(&img.stmts).unwrap()))
        }
        "l1.cost" => {
            let run = &module.images[inp["image"].as_str().unwrap()].stmts;
            let all: Vec<usize> = (0..run.len()).collect();
            json!({"cost": ruled.cycles(run, &all).unwrap(), "order": ruled.order(run).unwrap()})
        }
        "l1.schedule" => {
            let got = schedule_module(&module, m).unwrap();
            let mut out: BTreeMap<String, Value> = got
                .images
                .iter()
                .map(|(n, d)| (n.clone(), lines(&emit(&d.stmts))))
                .collect();
            if inp["twice"].as_bool() == Some(true) {
                let again = schedule_module(&module, m).unwrap();
                out.insert("same".into(), json!(again.source == got.source));
            }
            json!(out)
        }
        other => panic!("no scheduler runner {other}"),
    }
}

pub fn check_all(dir: &std::path::Path) -> (usize, Vec<String>, Vec<String>) {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let mut bad = Vec::new();
    let mut ruled = Vec::new();
    let mut n = 0;
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        let case: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        n += 1;
        let got = run(case["runner"].as_str().unwrap(), &case["input"], &m);
        let want = &case["ruled"];
        if &got != want {
            bad.push(format!("{}:\n  got  {got}\n  want {want}", case["vector"]));
        }
        if case["rulings"].as_array().unwrap().is_empty() {
            if case["ruled"] != case["expect"] {
                bad.push(format!(
                    "{}: unruled but differs from its expect",
                    case["vector"]
                ));
            }
        } else {
            ruled.push(case["vector"].as_str().unwrap().to_string());
        }
    }
    (n, bad, ruled)
}
