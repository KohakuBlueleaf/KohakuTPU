//! Certified single-owner walks and incremental exact mover re-execution.

use ktpu::templates::{proof, proof_mover};
use ktpu_isa::unit::mover::{R_AXIS, R_CTRL, R_DIM, R_HDR};
use ktpu_l0::format::template::{Bounds, ParameterTable, Params, Template, ValuePatch};
use ktpu_l0::{Op, Package, Step, Word};
use ktpu_machine::Machine;
use ktpu_verify::{check, Target};

fn machine() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

fn target() -> Target {
    Target::from_machine(&machine(), None).unwrap()
}

fn put(reg: u32, value: u64) -> Word {
    Word([u64::from(reg), value, u64::MAX, 0])
}

fn fixture(hi: i64) -> Template {
    let t = target();
    let word = t.facts.word_bytes as u64;
    let payloads = vec![
        put(R_HDR, 1 | (1 << 44)),
        put(R_DIM, 1 | (2 << 4) | (word << 20)),
        put(R_AXIS, 0),
        put(R_CTRL, (1 << 16) | u64::from(t.isa.mover.modes.copy)),
    ];
    Template {
        package: Package {
            steps: vec![Step::new(Op::Mover, 0, payloads.len() as u64, 0, 0)],
            payloads,
            ..Default::default()
        },
        table: ParameterTable {
            parameters: [("n".into(), Bounds { lo: 0, hi })].into(),
            sites: vec![ValuePatch {
                name: "destination.count".into(),
                payload: 1,
                step: None,
                bit: 68,
                width: 16,
                shift: 0,
                constant: 2,
                coefficients: [("n".into(), 1)].into(),
                buffer: None,
            }],
            ..Default::default()
        },
    }
}

fn prove(t: &Template, cap: usize) -> Result<proof::ProvenBox, proof::Refusal> {
    proof::prove(
        &[(0, t.clone())].into(),
        &[(0, vec![])].into(),
        &machine(),
        cap,
    )
}

fn check_at(t: &Template, n: i64) -> bool {
    let params: Params = [("n".into(), n)].into();
    let nodes: ktpu_verify::Nodes = [(0, t.bind(&[], &params).unwrap())].into();
    check(&[nodes.clone(), nodes], &target()).is_empty()
}

#[test]
fn single_stream_summary_proves_large_box_without_enumeration() {
    let t = fixture(3000);
    let p = prove(&t, 1).unwrap();
    assert_eq!(p.certificate().version, 2);
    assert_eq!(p.certificate().checker_bindings, 1);
    assert_eq!(p.certificate().eliminated, vec!["n"]);
    let summary = p.certificate().mover_arguments[0]
        .single_stream
        .as_ref()
        .unwrap();
    assert_eq!(summary.words_max, 3002);
    assert_eq!(summary.address_min, 0);
    assert_eq!(
        summary.address_max,
        3001 * target().facts.word_bytes as i128
    );
    for n in [0, 1, 71, 2999, 3000] {
        assert!(check_at(&t, n));
    }
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with(proof_mover::NO_SUMMARY, || {
        assert!(prove(&t, 1).is_err());
    });
}

#[test]
fn every_small_two_dimensional_binding_agrees_with_summary() {
    let mut t = fixture(4);
    let word = target().facts.word_bytes as u64;
    t.package.payloads[0] = put(R_HDR, 1 | (2 << 44));
    t.package
        .payloads
        .insert(3, put(R_DIM, 3 | (3 << 4) | ((word * 16) << 20)));
    t.package.payloads.insert(4, put(R_AXIS, 0));
    t.package.steps[0].count = t.package.payloads.len() as u64;
    t.table
        .parameters
        .insert("m".into(), Bounds { lo: 0, hi: 4 });
    let mut p = t.table.sites[0].clone();
    p.payload = 3;
    p.constant = 3;
    p.coefficients = [("m".into(), 1)].into();
    t.table.sites.push(p);
    let proven = prove(&t, 1).unwrap();
    for n in 0..=4 {
        for m in 0..=4 {
            let nodes = proven
                .bind(&[("n".into(), n), ("m".into(), m)].into())
                .unwrap();
            assert!(check(&[nodes.clone(), nodes], &target()).is_empty());
        }
    }
}

#[test]
fn interior_misalignment_refuses_and_guard_mutant_is_killed() {
    let mut t = fixture(2);
    let word = target().facts.word_bytes;
    let p = &mut t.table.sites[0];
    p.bit = 84;
    p.width = 32;
    p.constant = word;
    p.coefficients = [("n".into(), word / 2)].into();
    assert!(check_at(&t, 0));
    assert!(check_at(&t, 2));
    assert!(!check_at(&t, 1));
    let refused = prove(&t, 3).unwrap_err();
    assert_eq!(refused.counterexample.unwrap()["n"], 1);
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with(proof_mover::NO_GUARDS, || {
        assert!(prove(&t, 1).is_ok());
    });
}

fn two_streams(overlap: bool) -> Template {
    let mut t = fixture(1);
    let target = target();
    let word = target.facts.word_bytes as u64;
    let per_line = target.facts.line_bytes / target.facts.word_bytes;
    assert!(per_line > 1);
    t.table.sites[0].constant = per_line;
    t.table.sites[0].coefficients = [("n".into(), -1)].into();
    t.package.payloads.extend([
        put(
            R_HDR,
            1 | ((if overlap { word } else { 1 << 20 }) << 4) | (1 << 44),
        ),
        put(R_DIM, 1 | (1 << 4) | (word << 20)),
        put(R_AXIS, 0),
        put(R_CTRL, (1 << 16) | u64::from(target.isa.mover.modes.copy)),
    ]);
    t.package.steps[0].count = t.package.payloads.len() as u64;
    t
}

#[test]
fn second_partial_owner_refuses_even_when_lower_binding_is_safe() {
    let t = two_streams(true);
    assert!(check_at(&t, 0));
    assert!(!check_at(&t, 1));
    let refused = prove(&t, 2).unwrap_err();
    assert_eq!(refused.counterexample.unwrap()["n"], 1);
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with(proof_mover::NO_GUARDS, || {
        assert!(prove(&t, 1).is_ok());
    });
}

#[test]
fn incremental_projection_reexecutes_only_lines_not_invariant_simulation() {
    let t = two_streams(false);
    let fast = prove(&t, 2).unwrap();
    assert_eq!(fast.certificate().checker_bindings, 2);
    assert_eq!(fast.certificate().simulation_bindings, 1);
    assert!(fast.certificate().mover_arguments[0]
        .single_stream
        .is_none());
    #[cfg(feature = "mutants")]
    ktpu_verify::mutant::with(proof_mover::NO_PROJECTION, || {
        let full = prove(&t, 2).unwrap();
        assert_eq!(full.certificate().simulation_bindings, 2);
        for n in 0..=1 {
            let params = [("n".into(), n)].into();
            assert_eq!(fast.bind(&params).unwrap(), full.bind(&params).unwrap());
        }
    });
    for n in 0..=1 {
        assert!(check_at(&t, n));
    }
}

#[test]
fn zero_counts_negative_addresses_and_exhaustion_have_no_summary() {
    let mut zero = fixture(1);
    zero.table.sites[0].constant = 1;
    zero.table.sites[0].coefficients = [("n".into(), -1)].into();
    assert!(check_at(&zero, 0));
    assert!(!check_at(&zero, 1));
    assert!(prove(&zero, 1).is_err());

    let mut negative = fixture(1);
    let word = target().facts.word_bytes as u64;
    negative.package.payloads[1] = put(R_DIM, 1 | (1 << 4) | (((1u64 << 32) - word) << 20));
    negative.table.sites[0].constant = 1;
    assert!(check_at(&negative, 0));
    assert!(!check_at(&negative, 1));
    assert!(prove(&negative, 1).is_err());

    let mut exhausted = fixture(1);
    exhausted.table.sites[0].constant = 1;
    exhausted.table.sites[0].coefficients = [("n".into(), 65534)].into();
    exhausted.package.payloads[0] = put(R_HDR, 1 | (2 << 44));
    exhausted
        .package
        .payloads
        .insert(3, put(R_DIM, 3 | (32 << 4) | (word << 20)));
    exhausted.package.payloads.insert(4, put(R_AXIS, 0));
    exhausted.package.steps[0].count = exhausted.package.payloads.len() as u64;
    assert!(check_at(&exhausted, 0));
    assert!(!check_at(&exhausted, 1));
    assert!(prove(&exhausted, 1).is_err());
    #[cfg(feature = "mutants")]
    for t in [&zero, &negative, &exhausted] {
        ktpu_verify::mutant::with(proof_mover::NO_GUARDS, || assert!(prove(t, 1).is_ok()));
    }
}

#[test]
fn unsupported_consumers_and_signed_discontinuity_fall_back() {
    for op in [Op::Patch, Op::Engine, Op::Repeat, Op::Load, Op::Jump] {
        let mut t = fixture(4);
        t.package.steps.push(Step::new(op, 0, 0, 0, 0));
        assert!(prove(&t, 1).is_err(), "{op:?}");
    }
    let mut t = fixture(1);
    let p = &mut t.table.sites[0];
    p.bit = 84;
    p.width = 32;
    p.constant = (1 << 31) - 32;
    p.coefficients = [("n".into(), 32)].into();
    assert!(prove(&t, 1).is_err());
}
