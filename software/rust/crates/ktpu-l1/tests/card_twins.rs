//! The `l1.card` vectors run on the card (tier 1), where their mutants are
//! killed by the values read back. Offline, each mutant ktpu-l1 carries for
//! such a vector must at least change what the vector's body builds to: one
//! that builds the same program cannot change a card value.

#![cfg(feature = "mutants")]

mod common;

use std::collections::BTreeMap;

use common::{machine_of, prog_steps, vectors};
use ktpu_l1::build;
use ktpu_l1::layout::buffer::Buffer;
use ktpu_l1::text::read;
use serde_json::Value;

/// The vector's body built at one address a parameter, as the spec runner
/// prints the programs.
fn built(v: &Value) -> Result<Value, String> {
    let m = machine_of(v).unwrap_or_else(common::machine);
    let module = read(v["input"]["text"].as_str().unwrap(), "<card>").map_err(|e| e.to_string())?;
    let name = v["input"]["fn"].as_str().unwrap();
    let mut binds = BTreeMap::new();
    let mut at = 0x0100_0000i128;
    for (p, t) in &module.body(name, "l1").map_err(|e| e.to_string())?.params {
        binds.insert(p.clone(), at);
        let n = Buffer::of(t)
            .and_then(|b| b.bytes(&m))
            .map_err(|e| e.to_string())?;
        at += (n.div_ceil(4096) * 4096) as i128;
    }
    let progs =
        build::programs(&module, name, &binds, &m, None, None).map_err(|e| e.to_string())?;
    let kind = |_: (u32, u32)| String::new();
    Ok(Value::Array(
        progs
            .iter()
            .map(|p| prog_steps(&kind, &p.program))
            .collect(),
    ))
}

#[test]
fn every_card_vector_mutant_changes_its_build() {
    let (mut checked, mut bad) = (0, Vec::new());
    for (name, v) in vectors("l1", "l1.card") {
        let base = built(&v).unwrap_or_else(|e| panic!("{name}: {e}"));
        for mu in v["mutants"].as_array().into_iter().flatten() {
            let mu = mu.as_str().unwrap();
            let Some(knob) = ktpu_l1::mutant::NAMES.iter().find(|n| **n == mu) else {
                continue;
            };
            checked += 1;
            let got = ktpu_l1::mutant::with(knob, || built(&v));
            if got.as_ref() == Ok(&base) {
                bad.push(format!("{name} ~ {mu}"));
            }
        }
    }
    eprintln!("{checked} l1.card mutants checked");
    assert!(checked > 0 && bad.is_empty(), "no change: {bad:?}");
}
