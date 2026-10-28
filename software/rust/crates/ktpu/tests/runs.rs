//! One compile builds and orders the first run once: `order` and `check`
//! share it (`stack::count`). Under `ktpu.stack/check_rebuilds` (`check`
//! building and ordering it again) the counts break and the output stays.

#![cfg(feature = "stack")]

use ktpu::stack::count;
use ktpu::{compile, Cache, Compiled, Input, Stack};
use ktpu_machine::Machine;
use serde_json::Value;
use std::path::Path;

fn case(name: &str) -> (String, Machine) {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../golden/e2e")
        .join(format!("{name}.json"));
    let c: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let m = Machine::named(c["machine"].as_str().unwrap()).unwrap();
    let nodes = c["nodes"].as_u64().unwrap() as u32;
    let m = match m.nodes() == nodes {
        true => m,
        false => m.with_nodes(nodes).unwrap(),
    };
    (c["l2"].as_str().unwrap().to_string(), m)
}

/// The compile and `(built, simulated)` it took, on this thread.
fn counted(name: &str) -> (Compiled, (usize, usize)) {
    let (l2, m) = case(name);
    count::reset();
    let got = compile(&Stack, &Input::L2(l2), None, &m, &Cache::off()).unwrap();
    (got, count::get())
}

#[test]
fn order_and_check_share_the_first_run() {
    // Built: the first run, the shifted build (STACK.RELOC), the rerun.
    // Simulated: the first run's order, the rerun's.
    for name in ["examples-dsl-activation", "examples-l2-softmax"] {
        let (_, counts) = counted(name);
        assert_eq!(counts, (3, 2), "{name}");
    }
}

/// Nothing a kernel compiled before leaves in the order key: kernel A, then
/// B, then A again through one on-disk cache, and the third compile builds
/// and simulates nothing.
#[test]
fn a_warm_order_cache_builds_nothing() {
    let dir = std::env::temp_dir().join(format!("ktpu-runs-{}", std::process::id()));
    let cache = Cache::new(&dir);
    let go = |name: &str| {
        let (l2, m) = case(name);
        count::reset();
        let got = compile(&Stack, &Input::L2(l2), None, &m, &cache).unwrap();
        (got, count::get())
    };
    let (a, cold) = go("examples-dsl-activation");
    go("examples-l2-softmax");
    let (again, warm) = go("examples-dsl-activation");
    let _ = std::fs::remove_dir_all(&dir);
    assert_eq!(cold, (3, 2));
    assert_eq!(warm, (0, 0));
    assert_eq!(again, a);
}

#[cfg(feature = "mutants")]
#[test]
fn check_rebuilding_the_first_run_is_seen_and_changes_nothing_else() {
    for name in ["examples-dsl-activation", "examples-l2-softmax"] {
        let (want, counts) = counted(name);
        let (got, mutated) =
            ktpu::mutant::with(ktpu::stack::CHECK_REBUILDS, || counted(name)).unwrap();
        assert_ne!(mutated, counts, "{name}: the counts see the mutant");
        assert_eq!(got, want, "{name}: the same packages and verdict");
    }
}
