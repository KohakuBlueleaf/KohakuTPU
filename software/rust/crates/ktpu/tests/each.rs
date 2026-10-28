//! `compile_each`: inputs on different machines in one parallel call, each
//! as `compile` makes it on its own machine, whatever the thread count.

#![cfg(feature = "stack")]

use ktpu::{compile, compile_each, Cache, Input, Stack};
use ktpu_machine::Machine;
use serde_json::Value;
use std::path::Path;

/// An e2e golden's L2 text and its machine.
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

#[test]
fn each_input_compiles_on_its_own_machine() {
    let (a, ma) = case("examples-dsl-activation");
    let (b, mb) = case("examples-l2-softmax");
    assert_ne!(ma.digest_bytes(), mb.digest_bytes(), "two machines");
    let cache = Cache::off();
    let inputs = vec![
        (Input::L2(a.clone()), None, &ma),
        (Input::L2(b.clone()), None, &mb),
        (Input::L2(a.clone()), None, &ma),
    ];
    let want = [
        compile(&Stack, &Input::L2(a.clone()), None, &ma, &cache).unwrap(),
        compile(&Stack, &Input::L2(b), None, &mb, &cache).unwrap(),
    ];
    for threads in [1, 4] {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .build()
            .unwrap();
        let got = pool.install(|| compile_each(&Stack, &inputs, &cache));
        let got: Vec<_> = got.into_iter().map(Result::unwrap).collect();
        assert_eq!(got[0], want[0], "{threads} threads");
        assert_eq!(got[1], want[1], "{threads} threads");
        assert_eq!(got[2], want[0], "{threads} threads");
    }
    let elsewhere = compile(&Stack, &Input::L2(a), None, &mb, &cache);
    assert!(
        elsewhere.as_ref() != Ok(&want[0]),
        "the first input compiles otherwise on the second machine"
    );
}
