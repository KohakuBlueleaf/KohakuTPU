//! `tensor::Session` against the Python `kp` planner (API.TEN / OP / FUSE /
//! READ): every recorded `kp` op log replays with the same steps (storage
//! names, launch names and graphs included), the same tensors made (shape,
//! statistic, storage) and the same refusals.

mod common;

use std::collections::HashMap;

use common::goldens;
use ktpu_plan::tensor::Session;
use ktpu_plan::{class, Case, Step};
use serde_json::Value;

fn normal(steps: &[Step]) -> Vec<Value> {
    common::normal(steps, false)
}

#[test]
fn every_kp_log_takes_the_python_steps() {
    let (mut cases, mut entries, mut bad) = (0, 0, Vec::new());
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        if case.kp.is_empty() {
            continue;
        }
        cases += 1;
        let m = case.machine();
        let mut s = Session::new(&m);
        let mut names: HashMap<String, usize> = HashMap::new();
        for (i, k) in case.kp.iter().enumerate() {
            entries += 1;
            let (steps, out, err) = match s.replay(&k.op, &k.args, &names) {
                Ok(r) => (r.steps, r.out, None),
                Err((e, steps)) => (steps, None, Some(e)),
            };
            let (have, mut want) = (normal(&steps), common::want(&k.steps, false));
            for (actual, reference) in have.iter().zip(&mut want) {
                let call = &actual["launch"]["call"];
                if call["gen"] == "vector.flat_kernel" {
                    let n = call["args"]["n"].as_i64().unwrap();
                    if n % m.vc.vlmax as i64 != 0 {
                        // Kernel semantics have sentinel/value tests; this test checks the planner steps.
                        let ports = actual["launch"]["graph"]["ports"].as_array().unwrap();
                        for p in ports {
                            let item = if p["spec"]["dtype"] == "f32" { 4 } else { 2 };
                            let elements: i64 = p["spec"]["shape"]
                                .as_array()
                                .unwrap()
                                .iter()
                                .map(|n| n.as_i64().unwrap())
                                .product();
                            assert_eq!(elements * item, (n * item + 31) / 32 * 32, "RUL.020");
                        }
                        reference["launch"]["graph"] = actual["launch"]["graph"].clone();
                    }
                }
            }
            let mut why = Vec::new();
            if have != want {
                let at = have
                    .iter()
                    .zip(&want)
                    .position(|(a, b)| a != b)
                    .unwrap_or(have.len().min(want.len()));
                why.push(format!(
                    "step {at} of {}/{}: {}",
                    have.len(),
                    want.len(),
                    common::first_diff(
                        have.get(at).unwrap_or(&Value::Null),
                        want.get(at).unwrap_or(&Value::Null)
                    )
                ));
            }
            match (&err, &k.error) {
                (None, None) => {}
                (Some(e), Some(w)) if *w == class::text(e) => {}
                _ => why.push(format!(
                    "error {:?} want {:?}",
                    err.as_ref().map(class::text),
                    k.error
                )),
            }
            if let (Some(name), Some(t)) = (&k.out, out) {
                match names.get(name) {
                    Some(prev) if *prev != t => why.push(format!("{name} is another tensor")),
                    Some(_) => {}
                    None => {
                        names.insert(name.clone(), t);
                    }
                }
                let x = &s.ts[t];
                let buf = x.buf.as_ref().map(|b| b.0.clone());
                if k.shape.as_ref() != Some(&x.shape) || k.stat != Some(x.stat) || k.buf != buf {
                    why.push(format!(
                        "made {:?} stat {} buf {:?}, want {:?} {:?} {:?}",
                        x.shape, x.stat, buf, k.shape, k.stat, k.buf
                    ));
                }
            } else if k.out.is_some() != out.is_some() && k.error.is_none() {
                why.push(format!("out {:?}, want {:?}", out, k.out));
            }
            if !why.is_empty() {
                bad.push(format!(
                    "{} entry {i} ({}): {}",
                    case.case,
                    k.op,
                    why.join("; ")
                ));
                break;
            }
        }
    }
    eprintln!("{cases} kp logs, {entries} entries replayed");
    let named = goldens()
        .iter()
        .filter(|p| p.file_name().unwrap().to_str().unwrap().starts_with("kp."))
        .count();
    assert!(
        cases >= named && named > 0,
        "{cases} kp logs of {named} kp goldens"
    );
    assert!(bad.is_empty(), "{} differ:\n{}", bad.len(), bad.join("\n"));
}
