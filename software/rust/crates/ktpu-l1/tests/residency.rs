//! Bound-package residency preserves completion ordinals and commits atomically.

use ktpu_isa::unit::vector::VecCu;
use ktpu_l0::format::{Op, Step, Unit};
use ktpu_l0::Package;
use ktpu_l1::lower::{imem::Cores, residency};
use ktpu_machine::Machine;

fn fixture() -> (Cores, Package) {
    let m = Machine::named("vlt_v9t6s_1n").unwrap();
    let cores = Cores::new(&m).unwrap();
    let isa = &cores.isa;
    let pkg = Package {
        units: vec![Unit {
            type_code: 0x5643,
            x: 1,
            y: 0,
            mesh: 0,
            credit: 16,
            fetch: 0,
        }],
        payloads: vec![
            VecCu::Imem { addr: 0, word: 123 }.encode(isa).unwrap(),
            VecCu::Imem {
                addr: 1,
                word: 31 << 27,
            }
            .encode(isa)
            .unwrap(),
            VecCu::Run { pc: 0 }.encode(isa).unwrap(),
        ],
        steps: vec![
            Step::new(Op::Dispatch, 0, 2, 0, 0),
            Step::new(Op::Await, 0, 2, 0, 0),
            Step::new(Op::Dispatch, 0, 1, 2, 0),
            Step::new(Op::Await, 0, 1, 0, 0),
        ],
        ..Package::default()
    };
    (cores, pkg)
}

#[test]
fn split_installs_and_completion_ordinals() {
    let (mut cores, pkg) = fixture();
    let first = residency::package(&pkg, &mut cores).unwrap();
    assert_eq!(
        first
            .steps
            .iter()
            .map(|s| (s.op, s.count))
            .collect::<Vec<_>>(),
        vec![(Op::Dispatch, 3), (Op::Await, 3)]
    );
    let second = residency::package(&pkg, &mut cores).unwrap();
    assert_eq!(
        second
            .steps
            .iter()
            .map(|s| (s.op, s.count))
            .collect::<Vec<_>>(),
        vec![(Op::Dispatch, 1), (Op::Await, 1)]
    );
}

#[test]
fn failed_rewrite_is_atomic_and_control_flow_invalidates() {
    let (mut cores, mut pkg) = fixture();
    pkg.steps.push(Step::new(Op::Await, 0, 99, 0, 0));
    assert!(residency::package(&pkg, &mut cores).is_err());
    assert!(cores.cores.is_empty());
    pkg.steps.pop();
    residency::package(&pkg, &mut cores).unwrap();
    assert!(!cores.cores.is_empty());
    pkg.steps.push(Step::new(Op::Jump, 0, 0, 0, 0));
    let warm = residency::package(&pkg, &mut cores).unwrap();
    assert_eq!(warm.payloads.len(), 1);
    assert!(warm.steps.iter().any(|s| s.op == Op::Jump && s.unit == 0));
    assert_eq!(cores.cores.values().next().unwrap().mem.len(), 2);
}

#[test]
fn bound_words_not_source_identity_select_slots() {
    let (mut cores, mut pkg) = fixture();
    residency::package(&pkg, &mut cores).unwrap();
    pkg.payloads[0] = VecCu::Imem { addr: 0, word: 789 }
        .encode(&cores.isa)
        .unwrap();
    let changed = residency::package(&pkg, &mut cores).unwrap();
    assert_eq!(changed.steps[0].count, 3);
    let again = residency::package(&pkg, &mut cores).unwrap();
    assert_eq!(again.steps[0].count, 1);
}

#[test]
fn compact_package_is_byte_exact_without_dead_installs() {
    let (mut cores, pkg) = fixture();
    residency::package(&pkg, &mut cores).unwrap();
    let got = residency::package(&pkg, &mut cores).unwrap();
    let expected = Package {
        payloads: vec![pkg.payloads[2]],
        steps: vec![
            Step::new(Op::Dispatch, 0, 1, 0, 0),
            Step::new(Op::Await, 0, 1, 0, 0),
        ],
        ..pkg
    };
    let machine = Machine::named("vlt_v9t6s_1n").unwrap();
    let facts = ktpu_l0::Facts::of_mesh(&machine, 0).unwrap();
    assert_eq!(
        got.to_bytes(&facts).unwrap(),
        expected.to_bytes(&facts).unwrap()
    );
}

#[test]
fn image_reuse_is_independent_of_other_images_and_source_slot() {
    let (mut cores, mut a) = fixture();
    a.steps = vec![Step::new(Op::Dispatch, 0, 3, 0, 0)];
    residency::package(&a, &mut cores).unwrap();
    let mut combined = a.clone();
    let isa = &cores.isa;
    combined.payloads = vec![
        VecCu::Imem { addr: 0, word: 999 }.encode(isa).unwrap(),
        VecCu::Imem {
            addr: 1,
            word: 31 << 27,
        }
        .encode(isa)
        .unwrap(),
        VecCu::Run { pc: 0 }.encode(isa).unwrap(),
        VecCu::Imem { addr: 2, word: 123 }.encode(isa).unwrap(),
        VecCu::Imem {
            addr: 3,
            word: 31 << 27,
        }
        .encode(isa)
        .unwrap(),
        VecCu::Run { pc: 2 }.encode(isa).unwrap(),
    ];
    combined.steps[0].count = 6;
    let got = residency::package(&combined, &mut cores).unwrap();
    let sent: Vec<_> = got.payloads.iter().filter(|w| w.get(252, 4) == 1).collect();
    assert_eq!(sent.len(), 2, "only the other image installs");
    let repeat = residency::package(&a, &mut cores).unwrap();
    assert_eq!(repeat.payloads.len(), 1);
}

#[test]
fn image_includes_skipped_halt_tail() {
    let source = [(0, 0x17 << 27 | 1), (1, 31 << 27), (2, 123), (3, 31 << 27)]
        .into_iter()
        .collect();
    let image = residency::Image::at(&source, 0).unwrap();
    assert_eq!(image.words.len(), 4);
}

#[test]
fn repeated_image_is_installed_once_and_counts_follow_expansion() {
    let (mut cores, mut pkg) = fixture();
    pkg.steps = vec![
        Step::new(Op::Repeat, 0, 3, 3 << 32, 0),
        Step::new(Op::Await, 0, 9, 0, 0),
    ];
    let first = residency::package(&pkg, &mut cores).unwrap();
    assert_eq!(first.steps[0].count, 5);
    assert_eq!(first.steps[1].count, 5);
    let second = residency::package(&pkg, &mut cores).unwrap();
    assert_eq!(second.steps[0].count, 3);
    assert_eq!(second.steps[1].count, 3);
}

#[cfg(feature = "mutants")]
#[test]
fn new_residency_mutants_break_their_witnesses() {
    for (name, witness) in [
        (
            "imr/union-image",
            image_reuse_is_independent_of_other_images_and_source_slot as fn(),
        ),
        ("imr/first-halt", image_includes_skipped_halt_tail as fn()),
        (
            "imr/dead-payload",
            compact_package_is_byte_exact_without_dead_installs as fn(),
        ),
    ] {
        assert!(std::panic::catch_unwind(|| ktpu_l1::mutant::with(name, witness)).is_err());
    }
}
