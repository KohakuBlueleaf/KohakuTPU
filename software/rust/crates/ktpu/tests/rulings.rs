//! Owner rulings exercised through the same adapters as the Python extension.
#![cfg(feature = "stack")]

use serde_json::Value;
use std::path::Path;

fn check(path: &str, mutant: Option<&str>) {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/spec/vectors");
    let v: Value =
        serde_json::from_str(&std::fs::read_to_string(root.join(path)).unwrap()).unwrap();
    let id = path.trim_end_matches(".json");
    let run = |mu| {
        ktpu::spec::run(
            v["runner"].as_str().unwrap(),
            &v["input"],
            v["machine"].as_str(),
            mu,
            Some(id),
            &v["expect"],
        )
        .unwrap()
    };
    let got = run(None);
    assert_eq!(got["result"], got["expect"], "{path}");
    if cfg!(feature = "mutants") {
        if let Some(mu) = mutant {
            assert_ne!(
                run(Some(mu))["result"],
                got["expect"],
                "{path} survives {mu}"
            );
        }
    }
}

#[test]
fn rul_001() {
    for path in [
        "isa/rulings/RUL.001.regs.json",
        "l1/rul/RUL.001.refuse-unbuilt.json",
        "l1/rul/RUL.001.v9t6-core.json",
        "l2/rulings/RUL.001.board_registers.json",
        "l2/rulings/RUL.001.board_without_core.json",
    ] {
        let mu = if path.contains("refuse-unbuilt") {
            "rul.001/unbuilt"
        } else if path.contains("without_core") {
            "rul.001/missing-core"
        } else {
            "rul.001/registers"
        };
        check(path, Some(mu));
    }
}
#[test]
fn rul_003() {
    check("isa/rulings/RUL.003.q1_refused.json", Some("rul.003/q1"));
}
#[test]
fn rul_014() {
    check(
        "isa/rulings/RUL.014.no_defaults.json",
        Some("rul.014/profile-reloaded"),
    );
}
#[test]
fn rul_048() {
    check(
        "isa/rulings/RUL.048.eoff_past_bank.json",
        Some("rul.048/fill-wraps"),
    );
}
#[test]
fn rul_049() {
    check(
        "isa/rulings/RUL.049.mode7.json",
        Some("rul.049/reserved-copy"),
    );
}
#[test]
fn rul_050() {
    check("isa/rulings/RUL.050.pitch.json", Some("rul.050/unaligned"));
}
#[test]
fn rul_051() {
    check(
        "isa/rulings/RUL.051.loop_zero.json",
        Some("rul.051/zero-skips"),
    );
}
#[test]
fn rul_052() {
    check(
        "isa/rulings/RUL.052.node_bit.json",
        Some("rul.052/dst-is-peer"),
    );
}
#[test]
fn rul_053() {
    check(
        "isa/rulings/RUL.053.faults.json",
        Some("rul.053/fault-dropped"),
    );
}
