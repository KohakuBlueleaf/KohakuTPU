//! The scratch arena end to end on TG.RUN.012's lanes relayout
//! (`golden/e2e/kernels-lanes-TG.RUN.012.relayout`): 2177 L2 temporaries in
//! one arena keep the package relocatable (STACK.RELOC passes, no bound
//! build); with `l1.layout/no-arena` each temporary is its own buffer, past
//! L0.BIND.005's 256: STACK.RELOC fires and the compile is a bound build
//! (`Compiled::bound`, no packages).

#![cfg(feature = "stack")]

use ktpu::{compile, Cache, Compiled, Input, Refused, Stack};
use ktpu_machine::Machine;
use serde_json::Value;
use std::path::Path;

fn lanes() -> Result<Compiled, Refused> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden/e2e/kernels-lanes-TG.RUN.012.relayout.json");
    let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
    let input = Input::L2(case["l2"].as_str().unwrap().to_string());
    compile(&Stack, &input, case["name"].as_str(), &m, &Cache::off())
}

#[test]
fn the_lanes_temporaries_share_one_arena_and_stay_relocatable() {
    let c = lanes().unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(c.bound, None, "STACK.RELOC passes: no bound build");
    assert!(!c.packages.is_empty());
    assert_eq!(c.scratch.len(), 1, "one dram arena");
    assert!(
        c.scratch[0].members.len() > 256,
        "{}",
        c.scratch[0].members.len()
    );
    for (mesh, p) in &c.packages {
        assert!(
            p.params.len() <= 256,
            "mesh {mesh}: {} buffers",
            p.params.len()
        );
        assert!(p.params.iter().any(|n| n == "scratch:dram"), "mesh {mesh}");
    }
}

#[cfg(feature = "mutants")]
#[test]
fn without_the_arena_stack_reloc_refuses_the_lanes_kernel() {
    let c = ktpu::mutant::with("l1.layout/no-arena", lanes)
        .unwrap()
        .unwrap_or_else(|e| panic!("{e}"));
    let why = c.bound.expect("relocatable without the arena");
    // STACK.RELOC's message: the refused word, outside the program's spans.
    assert!(why.contains("is not relocatable"), "{why}");
    assert!(c.packages.is_empty() && c.scratch.is_empty());
}
