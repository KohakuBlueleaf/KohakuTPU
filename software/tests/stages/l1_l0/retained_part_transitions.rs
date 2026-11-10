//! Real package transitions, two epochs, and balanced missing-join mutants.
//! The event assertion meters execution only, not total compiler/copy work.
use super::super::{clean, dma, package};
use crate::common::card;
use ktpu_l0::{Facts, Op};
use ktpu_l0::program::Program;
use ktpu_verify::{composition::parts, Kind, MemoryContext, Nodes, Target};

fn graph(count: usize, missing: bool, engine: bool) -> Vec<Nodes> {
    let machine = card();
    (0..count).map(|part| {
        let mut ps = [Program::new(), Program::new()];
        // Repeatedly share one physical cell. The protocol, not visitation
        // order or a synthetic host fence, must carry its visibility.
        if part % 2 == 0 {
            ps[0].store(vec![(4096, 7)], false);
        } else {
            ps[1].load(0, 4096, None, false);
        }
        ps[0].barrier();
        ps[1].barrier();
        if part + 1 < count {
            ps[1].ring(0, 9, 1, true);
            ps[0].wait_slot(9, 1, true);
            if !(missing && part == 0) {
                ps[0].ring(1, 9, 1, true);
                ps[1].wait_slot(9, 1, true);
            }
        }
        ps.iter().enumerate().map(|(mesh, p)| {
            let mut pkg = package(p, mesh as u32);
            if engine {
                pkg = ktpu_l0::engine::lower(&pkg, &Facts::of_mesh(&machine, mesh as u32).unwrap()).unwrap();
            }
            (mesh as u32, pkg)
        }).collect()
    }).collect()
}

fn check(mut runs: Vec<Nodes>) -> parts::Report {
    let target = Target::from_machine(&card(), Some(2)).unwrap();
    let period = runs.len();
    runs.extend(runs.clone());
    parts::check(&runs, &target, &MemoryContext {
        independent_packages: true, host_join_every: period, ..Default::default()
    })
}

#[test]
fn exact_parts_prove_without_concatenated_replay() {
    for engine in [false, true] {
        let small = check(graph(8, false, engine));
        let large = check(graph(32, false, engine));
        clean(&small.findings);
        clean(&large.findings);
        assert_eq!(small.checked_parts, 16);
        assert_eq!(large.checked_parts, 64);
        assert_eq!(small.peak_package_steps, large.peak_package_steps);
        assert!(small.events > 0);
        assert!(2 * large.events <= 9 * small.events);
    }
}

#[test]
fn balanced_missing_join_cannot_be_repaired_by_part_installation() {
    for engine in [false, true] {
        let found = check(graph(2, true, engine));
        assert!(found.findings.iter().any(|f| f.kind.refuses()));
        assert_eq!(found.checked_parts, 0, "contract must reject before execution");
    }
}

#[test]
fn extra_body_tokens_cannot_impersonate_join_generation() {
    let mut runs = graph(2, false, false);
    // A numerically available release from the body must not let a follower
    // start its next package while the coordinator is still accessing data.
    let extra = runs[0][&0].steps.iter().find(|s| s.op == Op::Ring).unwrap().clone();
    runs[0].get_mut(&0).unwrap().steps.insert(0, extra);
    let report = check(runs);
    assert!(report.findings.iter().any(|f| f.kind == Kind::Model));
    assert_eq!(report.checked_parts, 0);
}

#[test]
fn replay_identity_is_not_just_a_part_number() {
    let runs = graph(2, false, false);
    let mut twice = [runs.clone(), runs].concat();
    twice[2].get_mut(&0).unwrap().steps[0].arg ^= 1;
    let target = Target::from_machine(&card(), Some(2)).unwrap();
    let report = parts::check(&twice, &target, &MemoryContext {
        independent_packages: true, host_join_every: 2, ..Default::default()
    });
    assert!(report.findings.iter().any(|f| f.text.contains("identity")));
}


#[test]
fn engine_mailbox_ring_cannot_hide_a_join_channel_token() {
    let mut runs = graph(2, false, false);
    let p = runs[0].get_mut(&0).unwrap();
    let at = p.payloads.len() as u64;
    // ENGINE interlink doorbell code 26; destination 1, join slot 9, add 1.
    p.payloads.push(ktpu_l0::Word([26, 1 | (9 << 8) | (1 << 16), 0, 0]));
    p.steps.insert(0, ktpu_l0::Step::new(Op::Engine, 0, 1, at, 0));
    let report = check(runs);
    assert!(report.findings.iter().any(|f| f.kind == Kind::Model
        && f.text.contains("ENGINE ring aliases")), "{:?}", report.findings);
}

#[test]
fn a_numerically_retired_unit_is_not_a_causally_acquired_drain() {
    let machine = card();
    let unit = Facts::of_mesh(&machine, 0).unwrap().units["VC"][0];
    for engine in [false, true] {
        let mut runs = graph(2, false, engine);
        let mut p = Program::new();
        p.send(unit, dma(4096, true), vec![]);
        // Deliberately no local barrier before gather. END after the release
        // would be too late to certify the package boundary.
        p.wait_slot(9, 1, true);
        p.ring(1, 9, 1, true);
        let mut raw = package(&p, 0);
        if engine { raw = ktpu_l0::engine::lower(&raw, &Facts::of_mesh(&machine, 0).unwrap()).unwrap(); }
        runs[0].insert(0, raw);
        let report = check(runs);
        assert!(report.findings.iter().any(|f| f.kind == Kind::Model
            && f.text.contains("local unit drain")), "{:?}", report.findings);
    }
}


#[test]
fn counter_prefix_copy_work_scales_with_parts_not_their_accumulated_history() {
    for engine in [false, true] {
        let small = check(graph(8, false, engine));
        let large = check(graph(32, false, engine));
        clean(&small.findings);
        clean(&large.findings);
        assert!(small.history_visits > 0);
        // This is the actual copy/freeze/query census, independent of events.
        // Exact logarithmic prefix lookup is allowed; quadratic prefix copies
        // at every transition are not. No proof budget is increased.
        assert!(large.history_visits <= 8 * small.history_visits,
            "history work: 8={} 32={}", small.history_visits, large.history_visits);
        let total = |r: &parts::Report| r.events + r.history_visits + r.retirement_visits;
        assert!(total(&large) <= 8 * total(&small));
    }
}
