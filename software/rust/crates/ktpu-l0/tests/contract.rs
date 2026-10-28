//! Software contract completion and relocated affine-address literals.
use std::collections::BTreeMap;

use ktpu_l0::build::AddrField;
use ktpu_l0::format::{Alu, F_IMM};
use ktpu_l0::program::{BuildOpts, Patch};
use ktpu_l0::{Facts, Op, Package, PackageBuilder, Program, Step, Word};
use ktpu_machine::Machine;

fn facts() -> Facts {
    Facts::of(&Machine::named("multimesh_v9t6").unwrap())
}

#[test]
fn contract_wire_rejects_reserved_fields_and_bad_registers() {
    let f = facts();
    let mut b = PackageBuilder::new(&f, BTreeMap::new(), 16);
    b.assert_zero(14, 1).unwrap();
    let p = b.build(true, false);
    let raw = p.to_bytes(&f).unwrap();
    assert_eq!(
        Package::from_bytes(&raw, &f).unwrap().steps[0],
        Step::new(Op::AssertZero, 14, 1, 0, 0)
    );
    for bad in [
        Step::new(Op::AssertZero, 16, 1, 0, 0),
        Step::new(Op::AssertZero, 14, 1, 1, 0),
        Step::new(Op::AssertZero, 14, 1, 0, F_IMM),
    ] {
        let mut mutated = p.clone();
        mutated.steps[0] = bad;
        assert!(mutated.to_bytes(&f).is_err());
    }
    assert!(b.assert_zero(16, 1).is_err());
    assert!(b.assert_zero(0, 1 << 32).is_err());
    b.address(0, 0x1000);
    let literal = b.build(true, false);
    for flags in [F_IMM | 8, F_IMM | 16, F_IMM | 32] {
        let mut bad = literal.clone();
        bad.steps[1].flags = flags;
        assert!(bad.to_bytes(&f).is_err());
    }
}

#[test]
fn affine_literal_relocates_without_displacing_patch_targets() {
    for base in [0, 0x1000] {
        affine_literal_at(base);
    }
}

fn affine_literal_at(base: u64) {
    let f = facts();
    let mut p = Program::new();
    let coord = f.units["VC"][0];
    p.alu(3, Alu::Mov, 0, 32, true);
    let words = vec![Word::from_u64(base), Word::from_u64(base + 128)];
    let patches = (0..2)
        .map(|word| Patch {
            word,
            segments: vec![(0, 40, 0)],
            reg: 3,
            konst: base + word as u64 * 128,
        })
        .collect();
    p.send(coord, words, patches);
    let addresses = |w: &Word, _: &str| {
        vec![AddrField {
            segments: vec![(0, 40, 0)],
            value: w.0[0],
        }]
    };
    let spans = BTreeMap::from([(base, 512)]);
    let plain = p
        .build(
            &f,
            BuildOpts {
                addresses: Some(&addresses),
                ..BuildOpts::default()
            },
        )
        .unwrap();
    let reloc = p
        .build(
            &f,
            BuildOpts {
                addresses: Some(&addresses),
                spans: Some(&spans),
                ..BuildOpts::default()
            },
        )
        .unwrap();
    assert_eq!(
        reloc
            .build(true, false)
            .bind(&reloc.bindings())
            .unwrap()
            .to_bytes(&f)
            .unwrap(),
        plain
            .build(true, false)
            .bind(&[])
            .unwrap()
            .to_bytes(&f)
            .unwrap()
    );
    let bound = reloc.build(true, false).bind(&[0x8000]).unwrap();
    let loads: Vec<_> = bound
        .steps
        .iter()
        .filter(|s| s.op == Op::Load && s.flags == F_IMM)
        .collect();
    assert_eq!(loads.len(), 2);
    assert_eq!(bound.payloads[loads[0].arg as usize].0[0], 0x8000);
    assert_eq!(bound.payloads[loads[1].arg as usize].0[0], 0x8080);
    let patch: Vec<_> = bound
        .steps
        .iter()
        .filter(|s| s.op == Op::Patch)
        .map(|s| s.count)
        .collect();
    assert_eq!(patch, vec![0, 1]);
    assert!(loads.iter().all(|s| s.arg >= 2));
    #[cfg(feature = "mutants")]
    {
        let broken = ktpu_l0::mutant::with("l0.program/unrelocated-literal", || {
            p.build(
                &f,
                BuildOpts {
                    addresses: Some(&addresses),
                    spans: Some(&spans),
                    ..BuildOpts::default()
                },
            )
            .unwrap()
        });
        assert!(!broken
            .steps
            .iter()
            .any(|s| s.op == Op::Load && s.flags == F_IMM));
        assert!(broken
            .steps
            .iter()
            .any(|s| s.op == Op::Alu && s.flags == F_IMM && s.arg == base));
    }
}

#[test]
fn loop_waits_use_dynamic_sent_counts() {
    let f = facts();
    let mut p = Program::new();
    p.label("top").unwrap();
    p.send(f.units["VC"][0], vec![Word::from_u64(0)], vec![]);
    let good = p.build(&f, BuildOpts::default()).unwrap();
    assert!(!good.steps.iter().any(|s| s.op == Op::Await && s.flags == 0));
    assert!(good
        .steps
        .iter()
        .any(|s| s.op == Op::Await && s.flags == F_IMM));
    assert!(good.steps.iter().any(|s| s.op == Op::Barrier));
    #[cfg(feature = "mutants")]
    {
        let bad = ktpu_l0::mutant::with("l0.program/static-loop-wait", || {
            p.build(&f, BuildOpts::default()).unwrap()
        });
        assert!(bad.steps.iter().any(|s| s.op == Op::Await && s.flags == 0));
    }
}
