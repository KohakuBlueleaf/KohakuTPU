//! The product ring compiles wherever the product without it compiles:
//! every recorded `matmul.mm_kernel` call of the plan goldens, under each
//! ring knob, through the stack to an unrefused verdict.

use std::path::Path;

use ktpu::{compile, Cache, Input, Stack};
use ktpu_machine::Machine;
use serde_json::Value;

/// Each `matmul.mm_kernel` call's arguments under `v`.
fn calls(v: &Value, out: &mut Vec<Value>) {
    match v {
        Value::Object(o) => {
            if o.get("gen").and_then(Value::as_str) == Some("matmul.mm_kernel") {
                out.push(o["args"].clone());
            }
            o.values().for_each(|x| calls(x, out));
        }
        Value::Array(a) => a.iter().for_each(|x| calls(x, out)),
        _ => {}
    }
}

/// `args`' kernel, compiled on `m`: its L1, or the refusal.
fn compiled(args: &Value, m: &Machine) -> Result<String, String> {
    let k = ktpu_kernels::call::call("matmul.mm_kernel", args, m).map_err(|e| e.to_string())?;
    let graph = serde_json::to_string(&k.graph).unwrap();
    let c =
        compile(&Stack, &Input::Graph(graph), None, m, &Cache::off()).map_err(|e| e.to_string())?;
    if c.verdict.refused {
        return Err(format!("{:?}", c.verdict.findings));
    }
    Ok(c.l1)
}

#[test]
fn every_recorded_product_compiles_through_the_ring() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/plan");
    let mut seen = std::collections::BTreeSet::new();
    let mut rung = 0;
    for f in std::fs::read_dir(dir).unwrap() {
        let path = f.unwrap().path();
        let text = std::fs::read_to_string(&path).unwrap();
        let case: ktpu_plan::Case = serde_json::from_str(&text).unwrap();
        let mut found = vec![];
        calls(&serde_json::from_str(&text).unwrap(), &mut found);
        let m = case.machine();
        for args in found {
            if !seen.insert(args.to_string()) {
                continue;
            }
            let base = compiled(&args, &m);
            assert!(base.is_ok(), "{}: {base:?}", path.display());
            for knobs in [
                serde_json::json!({"ring_depth": 1}),
                serde_json::json!({"ring_depth": 2, "split_vc": true}),
                serde_json::json!({"ring_depth": 2, "tail_bands": true}),
            ] {
                let mut a = args.clone();
                a.as_object_mut()
                    .unwrap()
                    .extend(knobs.as_object().unwrap().clone());
                let l1 =
                    compiled(&a, &m).unwrap_or_else(|e| panic!("{} {knobs}: {e}", path.display()));
                assert_ne!(
                    Ok(&l1),
                    base.as_ref(),
                    "{}: the ring changes the kernel",
                    path.display()
                );
                rung += 1;
            }
        }
    }
    assert!(rung > 0);
    eprintln!("{} recorded products, {rung} ringed compiles", seen.len());
}
