//! Work, not wall time: repeated operands must not rescan their use history.
use ktpu_l1::text::Term;
use ktpu_l2::lower::vector::banks::typed::{choose, choose_profiled};
use ktpu_l2::lower::vector::gen::typed::{math, Event, Symbol};
use ktpu_l2::lower::vector::modulo::Core;
use ktpu_l2::target::Target;

#[test]
fn incidence_keeps_exact_rotation_choices_with_linear_membership_work() {
    let core = Core::of(&Target::of(&super::common::card()));
    for copies in [2, 3] {
        for repeated in [false, true] {
            let mut work = Vec::new();
            for n in [16, 64] {
                let second = if repeated { 1 } else { 4 };
                let code: Vec<_> = (0..n)
                    .map(|k| Event::Op {
                        stmt: math(
                            "vfma",
                            Term::name("%0"),
                            // Repeated operands and three distinct words;
                            // alternate the last symbol's nonadjacent uses.
                            vec![
                                Term::name("%1"),
                                Term::name(if repeated { "%1" } else { "%4" }),
                                Term::name(if k % 2 == 0 { "%2" } else { "%3" }),
                            ],
                            vec![],
                        ),
                        syms: vec![
                            Symbol(0), Symbol(1), Symbol(second),
                            Symbol(if k % 2 == 0 { 2 } else { 3 }),
                        ],
                    })
                    .collect();
                let (indexed, checks) = choose_profiled(&code, copies, &core, false);
                let (reference, scans) = choose_profiled(&code, copies, &core, true);
                assert_eq!(indexed, reference, "coordinate descent and tie order");
                assert_eq!(choose(&code, copies, &core), reference);
                if !repeated {
                    assert!(!indexed.is_empty(), "exercise a real bank rotation");
                }
                assert_eq!(checks, 3 * n as u64);
                assert!(scans > checks * 4, "exercise a growing history");
                work.push(checks);
            }
            assert_eq!(work[1], 4 * work[0]);
        }
    }
}

#[test]
fn rebinding_at_zero_preserves_the_immutable_footprint() {
    use ktpu_l2::lower::sync::Access;
    use std::collections::BTreeMap;
    use std::sync::Arc;
    let original = Access::strided("vc[0]", true, "x", -64, &[(128, 32)], 64);
    let shifted = original.shifted(0).expect("zero shift");
    assert_eq!(original, shifted);
    assert!(Arc::ptr_eq(
        original.footprint.as_ref().unwrap(), shifted.footprint.as_ref().unwrap()
    ));
    let bound = original.bound(&BTreeMap::from([("x".into(), 0)])).unwrap();
    assert_eq!(bound.key.0, "bound_dram");
    assert!(Arc::ptr_eq(
        original.footprint.as_ref().unwrap(), bound.footprint.as_ref().unwrap()
    ));
    let moved = original.shifted(64).unwrap();
    assert_eq!(moved.lo, original.lo + 64);
    assert_eq!(moved.hi, original.hi + 64);
    assert_eq!(moved.footprint.as_ref().unwrap().geometry().0, 0);
}
