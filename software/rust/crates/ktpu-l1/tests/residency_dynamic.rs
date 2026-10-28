//! Dynamic instruction streams retain patch aliases and immutable image reuse.
use ktpu_isa::unit::vector::VecCu;
use ktpu_l0::format::{patch_arg, Op, Step, Unit};
use ktpu_l0::{Facts, Package, Word, WordOps};
use ktpu_l1::lower::{engine, imem::Cores, manifest, residency};
use ktpu_machine::Machine;

fn fixture() -> (Cores, Package, Facts) {
    let m = Machine::named("vlt_v9t6s_1n").unwrap();
    let c = Cores::new(&m).unwrap();
    let isa = &c.isa;
    let payloads = vec![
        VecCu::Imem { addr: 0, word: 123 }.encode(isa).unwrap(),
        VecCu::Imem {
            addr: 1,
            word: 31 << 27,
        }
        .encode(isa)
        .unwrap(),
        VecCu::Run { pc: 0 }.encode(isa).unwrap(),
        Word::placed(17, 0),
    ];
    let p = Package {
        units: vec![Unit {
            x: 1,
            y: 0,
            mesh: 0,
            type_code: 0x5643,
            credit: 16,
            fetch: 0,
        }],
        steps: vec![
            Step::new(Op::Dispatch, 0, 3, 0, 0),
            Step::new(Op::Await, 0, 3, 0, 0),
        ],
        payloads,
        ..Package::default()
    };
    (c, p, Facts::of_mesh(&m, 0).unwrap())
}
fn installs(p: &Package) -> usize {
    p.steps
        .iter()
        .filter(|s| matches!(s.op, Op::Dispatch | Op::Send | Op::Repeat))
        .map(|s| {
            let start = if s.op == Op::Dispatch {
                s.arg
            } else {
                s.arg & 0xffff_ffff
            };
            p.payloads[start as usize..(start + s.count) as usize]
                .iter()
                .filter(|w| w.get(252, 4) == 1)
                .count()
        })
        .sum()
}

#[test]
fn patch_outside_image_and_jump_reuse() {
    let (mut c, mut p, _) = fixture();
    p.steps.insert(
        0,
        Step::new(
            Op::Patch,
            0,
            3,
            patch_arg(0, 16, 0, false, false).unwrap(),
            0,
        ),
    );
    p.steps.push(Step::new(Op::Jump, 1, 0, 0, 0));
    let cold = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&cold), 2);
    let warm = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&warm), 0);
    assert!(warm.steps.iter().any(|s| s.op == Op::Jump && s.unit == 1));
}

#[test]
fn image_patch_stays_inline_and_shared_words_keep_aliases() {
    let (mut c, mut p, _) = fixture();
    p.steps.insert(
        0,
        Step::new(
            Op::Patch,
            0,
            0,
            patch_arg(0, 16, 0, false, false).unwrap(),
            0,
        ),
    );
    p.steps.push(Step::new(Op::Dispatch, 0, 3, 0, 0));
    p.steps.push(Step::new(Op::Await, 0, 3, 0, 0));
    residency::package(&p, &mut c).unwrap();
    let warm = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&warm), 2, "mutable instruction sent for both runs");
    let patch = warm.steps.iter().find(|s| s.op == Op::Patch).unwrap();
    let aliases = warm
        .steps
        .iter()
        .filter(|s| s.op == Op::Dispatch && s.arg == patch.count)
        .count();
    assert_eq!(aliases, 2);
    assert!(!c.cores.values().next().unwrap().mem.contains_key(&0));
}

#[test]
fn send_keeps_type_and_reuses_inline_image() {
    let (mut c, mut p, _) = fixture();
    p.steps = vec![Step::new(Op::Send, 0, 3, 5 << 32, 0)];
    let cold = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&cold), 2);
    let warm = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&warm), 0);
    let send = warm.steps.iter().find(|s| s.op == Op::Send).unwrap();
    assert_eq!(send.arg >> 32, 5);
    assert_eq!(send.count, 1);
    assert_eq!(
        warm.payloads[(send.arg & 0xffff_ffff) as usize],
        p.payloads[2]
    );
}

#[test]
fn lowered_engine_lifts_to_reusable_images() {
    let (mut c, p, f) = fixture();
    for port in [0, 0x10021] {
        let mut p = p.clone();
        p.units[0].fetch = port;
        let low = ktpu_l0::engine::lower(&p, &f).unwrap();
        assert!(low.steps.iter().any(|s| s.op == Op::Engine));
        let compact = engine::compact(&low).unwrap();
        assert!(compact.payloads.len() < low.payloads.len());
        ktpu_l0::replay::check(&p, &low, &f).unwrap();
        let sent = |q: &Package| -> Vec<Word> {
            q.steps
                .iter()
                .filter(|s| s.op == Op::Dispatch)
                .flat_map(|s| {
                    q.payloads[s.arg as usize..(s.arg + s.count) as usize]
                        .iter()
                        .copied()
                })
                .collect()
        };
        assert_eq!(sent(&engine::lift(&compact, &f).unwrap()), sent(&p));
        if port == 0 {
            let mut expected = low.clone();
            expected.payloads.drain(..p.payloads.len());
            for step in &mut expected.steps {
                step.arg -= p.payloads.len() as u64;
            }
            assert_eq!(
                compact.to_bytes(&f).unwrap(),
                expected.to_bytes(&f).unwrap()
            );
        }
        let lifted = engine::lift(&compact, &f).unwrap();
        let first = residency::package(&lifted, &mut c).unwrap();
        let second = residency::package(&lifted, &mut c).unwrap();
        assert!(installs(&first) <= 2);
        assert_eq!(installs(&second), 0);
    }
}

#[test]
fn repeat_with_runtime_descriptor_patch_reuses_image() {
    let (mut c, mut p, _) = fixture();
    p.payloads[3] = p.payloads[2];
    p.payloads[2] = Word::placed(2, 252);
    p.steps = vec![
        Step::new(
            Op::Patch,
            0,
            2,
            patch_arg(0, 16, 0, false, false).unwrap(),
            0,
        ),
        Step::new(Op::Repeat, 0, 4, 3 << 32, 0),
        Step::new(Op::Await, 0, 12, 0, 0),
    ];
    residency::package(&p, &mut c).unwrap();
    let warm = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&warm), 0);
    let repeat = warm.steps.iter().find(|s| s.op == Op::Repeat).unwrap();
    assert_eq!(repeat.count, 2);
    assert!(warm
        .steps
        .iter()
        .any(|s| s.op == Op::Patch && s.count == repeat.arg & 0xffff_ffff));
    assert!(warm.steps.iter().any(|s| s.op == Op::Await && s.count == 6));
}

#[test]
fn repeat_increment_delta_patch_tracks_compacted_record() {
    let (mut c, mut p, _) = fixture();
    p.payloads[3] = p.payloads[2];
    p.payloads[2] = Word::placed(2, 252);
    p.payloads
        .push(Word::placed(2 | (16 << 40), 0) | Word::placed(3, 64));
    p.steps = vec![
        Step::new(
            Op::Patch,
            0,
            4,
            patch_arg(64, 64, 0, false, false).unwrap(),
            0,
        ),
        Step::new(Op::Repeat, 0, 4, (3 << 32) | (1 << 48), 0),
        Step::new(Op::Await, 0, 12, 0, 0),
    ];
    residency::package(&p, &mut c).unwrap();
    let warm = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&warm), 0);
    let repeat = warm.steps.iter().find(|s| s.op == Op::Repeat).unwrap();
    let record = (repeat.arg & 0xffff_ffff) + repeat.count;
    assert_eq!(warm.payloads[record as usize].get(0, 32), 0);
    assert!(warm
        .steps
        .iter()
        .any(|s| s.op == Op::Patch && s.count == record && s.arg & 255 == 64));
    assert_eq!(
        warm.payloads.len(),
        3,
        "descriptor, RUN, increment record only"
    );
}

#[test]
fn engine_expansion_retargets_static_control_references() {
    let (_, p, f) = fixture();
    let mut low = ktpu_l0::engine::lower(&p, &f).unwrap();
    let end = low.steps.len() as u64;
    low.steps.push(Step::new(Op::Jump, end + 1, 0, 0, 0));
    low.steps.push(Step::bare(Op::End));
    let lifted = engine::lift(&low, &f).unwrap();
    let jump = lifted.steps.iter().find(|s| s.op == Op::Jump).unwrap();
    assert_eq!(lifted.steps[jump.unit as usize].op, Op::End);
    assert_eq!(jump.unit as usize, lifted.steps.len() - 1);
}

#[test]
fn retained_repeat_meets_its_implicit_backedge() {
    let (mut c, mut p, _) = fixture();
    residency::package(&p, &mut c).unwrap();
    p.payloads = vec![
        p.payloads[0],
        p.payloads[2],
        VecCu::Imem { addr: 0, word: 456 }.encode(&c.isa).unwrap(),
        p.payloads[2],
        Word::placed(2, 252),
    ];
    p.steps = vec![
        Step::new(
            Op::Patch,
            0,
            4,
            patch_arg(0, 32, 0, false, false).unwrap(),
            0,
        ),
        Step::new(Op::Repeat, 0, 5, 2 << 32, 0),
        Step::new(Op::Await, 0, 10, 0, 0),
    ];
    let got = residency::package(&p, &mut c).unwrap();
    let repeat = got.steps.iter().find(|s| s.op == Op::Repeat).unwrap();
    assert_eq!(
        repeat.count, 5,
        "A must install again after B on the backedge"
    );
}

#[test]
fn dynamic_fetch_forgets_destination_then_relearns_known_installs() {
    let (mut c, mut p, _) = fixture();
    residency::package(&p, &mut c).unwrap();
    p.steps.insert(0, Step::new(Op::Fetch, 0, 1, 0x100000, 0));
    p.steps.insert(1, Step::new(Op::Await, 0, 1, 0, 0));
    p.steps.push(Step::new(
        Op::Patch,
        0,
        3,
        patch_arg(0, 16, 0, false, false).unwrap(),
        0,
    ));
    let got = residency::package(&p, &mut c).unwrap();
    assert_eq!(installs(&got), 2);
    assert_eq!(got.steps[0].op, Op::Fetch);
    assert_eq!(c.cores.values().next().unwrap().mem[&0], 123);
}

#[test]
fn prefix_patches_observe_previously_patched_alu_value() {
    let (_, mut p, _) = fixture();
    p.steps = vec![
        Step::new(Op::Alu, 0, 0, 2, ktpu_l0::format::F_IMM),
        Step::new(
            Op::Patch,
            0,
            2,
            patch_arg(64, 64, 0, true, false).unwrap(),
            0,
        ),
        Step::new(Op::Alu, 1, 0, 1, ktpu_l0::format::F_IMM),
        Step::new(
            Op::Patch,
            1,
            4,
            patch_arg(32, 32, 0, true, false).unwrap(),
            0,
        ),
        Step::new(Op::Dispatch, 0, 0, 0, 0),
    ];
    let got = ktpu_l1::lower::counts::specialize(&p).unwrap();
    assert_eq!(got.steps[4].count, 2);
}

#[test]
fn manifest_is_host_only_and_rejects_wrong_sites() {
    let (c, p, _) = fixture();
    let mut images = manifest::describe(&p, &c.isa);
    assert_eq!(images["images"][0]["sites"], serde_json::json!([0, 1]));
    manifest::validate(&p, &c.isa, &images).unwrap();
    images["images"][0]["sites"] = serde_json::json!([3, 1]);
    assert!(manifest::validate(&p, &c.isa, &images).is_err());
}

#[cfg(feature = "mutants")]
#[test]
fn runtime_patch_mutants_are_killed() {
    for (name, witness) in [
        (
            "imr/dead-payload",
            lowered_engine_lifts_to_reusable_images as fn(),
        ),
        (
            "imr/first-iteration-only",
            retained_repeat_meets_its_implicit_backedge as fn(),
        ),
        (
            "imr/engine-stale-target",
            engine_expansion_retargets_static_control_references as fn(),
        ),
        (
            "imr/prefix-stale-alu",
            prefix_patches_observe_previously_patched_alu_value as fn(),
        ),
        (
            "imr/patch-is-constant",
            image_patch_stays_inline_and_shared_words_keep_aliases as fn(),
        ),
        (
            "imr/forget-patch-alias",
            repeat_with_runtime_descriptor_patch_reuses_image as fn(),
        ),
    ] {
        assert!(std::panic::catch_unwind(|| ktpu_l1::mutant::with(name, witness)).is_err());
    }
}
