//! Literal builder steps retain positions for host template patch sites.

#[path = "common/mod.rs"]
mod common;

use ktpu_l0::spec::build::{builder, call, named};
use ktpu_l0::{Op, Step};
use serde_json::{json, Map};

#[test]
fn literal_steps_and_end_preserve_indices_and_wire_words() {
    let f = common::v9t6();
    let mut b = builder(&json!({"credit": 512}), &f).unwrap();
    for (name, args) in [
        ("step", vec![json!(3)]),
        ("step", vec![json!(3)]),
        ("end", vec![]),
    ] {
        call(&mut b, name, &named(name, &args, &Map::new()).unwrap())
            .unwrap()
            .unwrap();
    }
    assert_eq!(
        b.steps,
        vec![
            Step::new(Op::Barrier, 0, 0, 0, 0),
            Step::new(Op::Barrier, 0, 0, 0, 0),
            Step::new(Op::End, 0, 0, 0, 0)
        ]
    );
    let raw = b.build(true, false).to_bytes(&f).unwrap();
    let offset = u64::from_le_bytes(raw[56..64].try_into().unwrap()) as usize;
    assert_eq!(
        &raw[offset..offset + 48],
        &[
            3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
        ]
    );
    let mut mutant = b.clone();
    mutant.steps.remove(0);
    assert_ne!(mutant.build(true, false).to_bytes(&f).unwrap(), raw);
}

#[test]
fn literal_step_rejects_unknown_opcode_and_wide_flags() {
    let f = common::v9t6();
    let mut b = builder(&json!({"credit": 512}), &f).unwrap();
    for args in [
        vec![json!(255)],
        vec![json!(3), json!(0), json!(0), json!(0), json!(256)],
    ] {
        assert!(call(&mut b, "step", &named("step", &args, &Map::new()).unwrap()).is_err());
        assert!(b.steps.is_empty());
    }
}
