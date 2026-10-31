//! The `l0.pairs.package` vectors through `spec::run`: each gives its expect,
//! and with the `mutants` feature each mutant it names changes the answer.
//! `KTPU_L0_FROZEN=FILE` (the Python runner's answers, plain and under each
//! mutant, as `scratchpad/rsl2/l0_freeze.py` writes them) also requires
//! every answer to equal the Python one.

use std::path::Path;

use ktpu_l0::spec;
use ktpu_machine::Machine;
use serde_json::Value;

const VECTORS: [&str; 5] = [
    "boot/L0.BOOT.004.pairs",
    "de/L0.DE.002.wait_code",
    "de/L0.DE.006.pairs",
    "pkg/L0.PKG.003.pairs",
    "step/L0.STEP.004.registers",
];

fn load(rel: &str) -> Value {
    let p = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/spec/vectors/l0")
        .join(format!("{rel}.json"));
    serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap()
}

/// RUL.042 through `spec::named::run`: the format names two UNBOUND values
/// the header does not define yet. At queue v4's landing (the define in
/// format.h) this gives the ruling's expect, and `l0.named` joins `ports()`.
#[test]
fn unbound_named_waits_for_the_header_define() {
    let v = load("rul/RUL.042.unbounded_named");
    let m = Machine::named(spec::DEFAULT_MACHINE).unwrap();
    let got = ktpu_l0::spec::named::run(&v["input"], Some(&m)).unwrap();
    assert_eq!(got["host"], true);
    if got["header"] == true {
        assert_eq!(got, v["expect"], "format.h defines it: switch l0.named on");
    }
}

#[test]
fn package_pairs_give_their_expect_and_their_mutants_change_it() {
    let frozen: Option<Value> = std::env::var("KTPU_L0_FROZEN")
        .ok()
        .map(|f| serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap());
    for rel in VECTORS {
        let v = load(rel);
        let m = Machine::named(v["machine"].as_str().unwrap_or(spec::DEFAULT_MACHINE)).unwrap();
        let run = |mu| spec::run("l0.pairs.package", &v["input"], Some(&m), mu).unwrap();
        let plain = run(None);
        assert_eq!(plain, v["expect"], "{rel}");
        let py = frozen.as_ref().map(|f| &f[format!("l0/{rel}")]);
        if let Some(py) = py {
            assert_eq!(plain, py["plain"]["answer"], "{rel}: Python");
        }
        if !cfg!(feature = "mutants") {
            continue;
        }
        for mu in v["mutants"].as_array().unwrap() {
            let mu = mu.as_str().unwrap();
            let got = run(Some(mu));
            assert_ne!(got, v["expect"], "{rel} ~ {mu} survives");
            if let Some(py) = py {
                assert_eq!(got, py["mutants"][mu]["answer"], "{rel} ~ {mu}: Python");
            }
        }
    }
}
