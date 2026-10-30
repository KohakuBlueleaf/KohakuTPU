//! The product ring and the in-kernel quant of an f16 A compile wherever the
//! product without them compiles: every recorded `matmul.mm_kernel` call of
//! the plan goldens, under each ring and quant knob, through the stack to an
//! unrefused verdict, its L1 another exactly where the kernel takes a knob.

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
    let mut taken_n = [0usize; 2];
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
                serde_json::json!({"activation_fp16": true}),
                serde_json::json!({"activation_fp16": true, "ring_depth": 2, "quant_ring": 1}),
                serde_json::json!({"activation_fp16": true, "ring_depth": 2, "quant_ring": 2}),
            ] {
                let mut a = args.clone();
                a.as_object_mut()
                    .unwrap()
                    .extend(knobs.as_object().unwrap().clone());
                let l1 =
                    compiled(&a, &m).unwrap_or_else(|e| panic!("{} {knobs}: {e}", path.display()));
                let taken = ktpu_kernels::call::mm_resolved(&a, &m).unwrap()["plain"] == false;
                assert_eq!(
                    Ok(&l1) != base.as_ref(),
                    taken,
                    "{} {knobs}",
                    path.display()
                );
                taken_n[usize::from(knobs.get("quant_ring").is_some())] += usize::from(taken);
            }
        }
    }
    assert!(taken_n.iter().all(|n| *n > 0), "{taken_n:?}");
    eprintln!(
        "{} recorded products, {taken_n:?} compiles under ring / quant knobs taken",
        seen.len()
    );
}
