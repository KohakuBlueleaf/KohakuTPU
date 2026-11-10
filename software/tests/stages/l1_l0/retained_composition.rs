//! Retained proof regression: production budget, real joins, real physical reuse.
//! Selected by tests/run.py's stages/l1_l0 (ktpu-sim integration target).

use super::{clean, dma, hazard, package};
use crate::common::card;
use ktpu_l0::program::Program;
use ktpu_l0::Facts;
use ktpu_verify::{composition, Kind, Nodes, Target};

// Two adjacent 32-byte words on each of 4096 nonadjacent 64-byte lines.
// One GO costs at least 4096 byte intervals + 4096 line intervals. Thus
// 256 joined launches exceed the legacy 2,000,000 cumulative budget even
// if every old interval has already retired. No test-only budget override.
fn launches(count: usize, first_owner: usize, missing_release: bool) -> Nodes {
    let machine = card();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let mut programs = [Program::new(), Program::new()];
    for launch in 0..count {
        let owner = (first_owner + launch) % 2;
        let base = 4096 | ((owner as u64) << machine.die.address.runtime().mesh_shift);
        programs[owner].post(
            vec![
                (0x10, (base << 4) | (2 << 44) | 1),
                (0x18, (2 << 4) | (32 << 20) | 1),
                (0x20, 0),
                (0x18, (4096 << 4) | (128 << 20) | 3),
                (0x20, 0),
                (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
            ],
            vec![],
        );
        for program in &mut programs {
            program.barrier();
        }
        if launch + 1 < count {
            // The composer's gather/release, not a host-side join. All event
            // tokens balance even in the mutant: omit both ends of one edge.
            programs[1].ring(0, 9, 1, true);
            programs[0].wait_slot(9, 1, true);
            if !(missing_release && launch == 0) {
                programs[0].ring(1, 9, 1, true);
                programs[1].wait_slot(9, 1, true);
            }
        }
    }
    programs.iter().enumerate().map(|(n, p)| (n as u32, package(p, n as u32))).collect()
}

#[test]
fn joined_launch_proofs_use_the_largest_launch_budget_not_the_sum() {
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    let mut largest = 0;
    for owner in 0..2 {
        let single = launches(1, owner, false);
        let legacy = ktpu_verify::check(&[single.clone()], &target);
        clean(&legacy);
        let (findings, work) = composition::prove(|| ktpu_verify::check(&[single], &target));
        assert_eq!(findings, legacy, "single launch semantic verdict");
        largest = largest.max(work);
    }
    assert!(largest > 0 && largest < 2_000_000);
    let graph = launches(256, 0, false);
    let legacy = ktpu_verify::check(&[graph.clone()], &target);
    assert!(legacy.iter().any(|f| f.kind == Kind::Model
        && f.text.contains("global DRAM proof budget exhausted")), "{legacy:?}");
    // The cold scheduling pass, final check and retained rerun all use the
    // same cap; no sum over launches or over replay epochs is admitted.
    let (findings, work) = composition::prove(|| {
        ktpu_verify::check(&[graph.clone(), graph.clone()], &target)
    });
    clean(&findings);
    assert!(work <= largest, "composed {work}, largest launch {largest}");
    // Scope restoration matters: composition must not change a later ordinary
    // single-package budget verdict on the same checker thread.
    assert_eq!(ktpu_verify::check(&[graph], &target), legacy);
}

#[test]
fn missing_cross_launch_release_is_still_a_physical_race() {
    let machine = card();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    for lowered in [false, true] {
        for missing in [false, true] {
            let mut graph = launches(2, 0, missing);
            if lowered {
                for (&mesh, package) in &mut graph {
                    let facts = Facts::of_mesh(&machine, mesh).expect("facts");
                    *package = ktpu_l0::engine::lower(package, &facts).expect("engine stream");
                }
            }
            let (findings, _) = composition::prove(|| ktpu_verify::check(&[graph], &target));
            if missing {
                hazard(&findings, Kind::Race);
            } else {
                clean(&findings);
            }
        }
    }
}

#[test]
fn a_local_barrier_does_not_publish_an_unjoined_remote_producer() {
    let machine = card();
    let unit = Facts::of_mesh(&machine, 0).expect("facts").units["VC"][0];
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    for acquire in [false, true] {
        let mut producer = Program::new();
        producer.send(unit, dma(4096, true), vec![]);
        producer.barrier();
        let mut consumer = Program::new();
        // An unrelated, ordered component must not cause early deletion of
        // the producer's access, even if the latter is visited later.
        consumer.store(vec![(8192 | (1 << machine.die.address.runtime().mesh_shift), 0)], false);
        consumer.barrier();
        if acquire {
            producer.ring(1, 9, 1, true);
            consumer.wait_slot(9, 1, true);
        }
        consumer.load(0, 4096, None, false);
        let nodes = Nodes::from([(0, package(&producer, 0)), (1, package(&consumer, 1))]);
        let (findings, _) = composition::prove(|| ktpu_verify::check(&[nodes], &target));
        if acquire { clean(&findings); } else { hazard(&findings, Kind::Race); }
    }
}

#[test]
fn intrinsic_footprint_limits_are_not_waived_by_composition() {
    let target = Target::from_machine(&card(), Some(1)).expect("target");
    let mut p = Program::new();
    let token = p.post(vec![
        (0x10, (4096 << 4) | (2 << 44) | 1),
        (0x18, (65535 << 4) | (32 << 20) | 1),
        (0x20, 0),
        (0x18, (65535 << 4) | (2097120 << 20) | 3),
        (0x20, 0),
        (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
    ], vec![]);
    p.wait_moves(token).expect("completion");
    let nodes = Nodes::from([(0, package(&p, 0))]);
    let legacy = ktpu_verify::check(&[nodes.clone()], &target);
    let (composed, _) = composition::prove(|| ktpu_verify::check(&[nodes], &target));
    for findings in [&legacy, &composed] {
        assert!(findings.iter().any(|f| f.kind == Kind::Model
            && f.text.contains("proof budget")), "{findings:?}");
    }
}

#[test]
fn cached_verdicts_do_not_cross_proof_decompositions() {
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    let graph = launches(1, 0, false);
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH).expect("clock").as_nanos();
    let directory = std::env::temp_dir().join(format!("ktpu-composition-{}-{stamp}", std::process::id()));
    std::fs::create_dir_all(&directory).expect("proof cache");
    let run = || ktpu_verify::gate(
        std::slice::from_ref(&graph), &target, ktpu_verify::Mode::Refuse,
        Some((&directory, "same-bytes")),
    );
    run().expect("legacy proof");
    let (result, work) = composition::prove(run);
    result.expect("composed proof");
    assert!(work > 0, "must not reuse the legacy cache entry");
    assert_eq!(std::fs::read_dir(&directory).expect("cache").count(), 2);
    std::fs::remove_dir_all(directory).expect("remove proof cache");
}

#[path = "multi_sysnode_composition.rs"]
mod multi_sysnode_composition;

#[path = "component_diagnostic.rs"]
mod component_diagnostic;

#[path = "retained_part_transitions.rs"]
mod retained_part_transitions;
