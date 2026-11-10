//! Counter publication prefixes must survive cross-sysnode retained joins.
//! One package per node: a host/package END must not repair the device join.

use super::super::{clean, hazard};
use ktpu_l0::program::{BuildOpts, Program};
use ktpu_l0::Facts;
use ktpu_machine::Machine;
use ktpu_verify::model::{clock::Clock, counter::Counter};
use ktpu_verify::{composition, Kind, Nodes, Target};

const ROWS: u64 = 4096;
const JOIN: u64 = 12;

fn stripe(node: u32) -> u64 {
    4096 + u64::from(node) * 0x10_0000
}

/// Each node writes a different physical stripe, routed through its successor.
/// A fenced then plain ring pair tests prefix publication, not the last mark's
/// incidental clock. Per-source slots avoid choosing one order of arrivals.
/// The following gather/release acquires all destination observations.
fn remote_launches(machine: &Machine, nodes: u32, count: usize, missing: bool, engine: bool) -> Nodes {
    let target = Target::from_machine(machine, Some(nodes)).expect("target");
    let mut programs: Vec<_> = (0..nodes).map(|_| Program::new()).collect();
    for launch in 0..count {
        for node in 0..nodes {
            let p = &mut programs[node as usize];
            // This is the *next* launch's read of its previous remote output.
            // The incoming publication alone cannot order its own outgoing
            // response. Omit the preceding release and this becomes a RAW.
            if node == 1 {
                p.load(0, stripe(node), None, false);
            }
            let destination = (node + 1) % nodes;
            let base = stripe(node) | (u64::from(destination) << machine.die.address.runtime().mesh_shift);
            p.post(vec![
                (0x10, (base << 4) | (2 << 44) | 1),
                (0x18, (2 << 4) | (32 << 20) | 1),
                (0x20, 0),
                (0x18, (ROWS << 4) | (128 << 20) | 3),
                (0x20, 0),
                (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
            ], vec![]);
            p.barrier();
            for peer in 0..nodes {
                if peer != node {
                    p.ring(u64::from(peer), 4 + u64::from(node), 1, true);
                    p.ring(u64::from(peer), 4 + u64::from(node), 1, false);
                }
            }
            for peer in 0..nodes {
                if peer != node {
                    p.wait_slot(4 + u64::from(peer), 2, true);
                }
            }
            if node == 0 {
                p.wait_slot(JOIN, u64::from(nodes - 1), true);
                for peer in 1..nodes {
                    if !(missing && launch == 0 && peer == 1) {
                        p.ring(u64::from(peer), JOIN, 1, true);
                    }
                }
            } else {
                p.ring(0, JOIN, 1, true);
                if !(missing && launch == 0 && node == 1) {
                    p.wait_slot(JOIN, 1, true);
                }
            }
        }
    }
    programs.into_iter().enumerate().map(|(node, p)| {
        let facts = Facts::of_mesh(machine, node as u32).expect("facts");
        let raw = p.build(&facts, BuildOpts::default()).expect("program").build(false, false);
        let package = if engine { ktpu_l0::engine::lower(&raw, &facts).expect("engine") } else { raw };
        (node as u32, package)
    }).collect()
}

#[test]
fn remote_joins_scale_to_four_nodes_and_preserve_missing_release_refusal() {
    for board in ["multimesh_v9t6", "multimesh_v9t8"] {
        let machine = Machine::named(board).expect("board");
        for nodes in [2, 4] {
            let target = Target::from_machine(&machine, Some(nodes)).expect("target");
            for engine in [false, true] {
                let single = remote_launches(&machine, nodes, 1, false, engine);
                let (found, largest) = composition::prove(|| ktpu_verify::check(&[single], &target));
                clean(&found);
                let joined = remote_launches(&machine, nodes, 3, false, engine);
                let (found, peak) = composition::prove(|| ktpu_verify::check(&[joined.clone(), joined], &target));
                clean(&found);
                assert!(peak <= largest, "{board} P={nodes} engine={engine}: {peak} > {largest}");
                let broken = remote_launches(&machine, nodes, 2, true, engine);
                let (found, _) = composition::prove(|| ktpu_verify::check(&[broken], &target));
                hazard(&found, Kind::Race);
            }
        }
    }
}

fn clock(owner: usize, value: u32) -> Clock {
    let mut clock = Clock::new();
    clock.tick(owner, value);
    clock
}

#[test]
fn publication_prefixes_do_not_acquire_future_or_optional_contributors() {
    let build = || {
        let mut c = Counter::new("slot".into(), Some(vec![("a".into(), 4), ("b".into(), 4)]));
        let mut published = clock(0, 1);
        published.join(&clock(2, 1)); // independent remote write-response event
        c.add(2, &published, 0);
        c.add(2, &clock(0, 2), 0); // later plain ring omits that response
        c.add(4, &clock(1, 1), 1);
        c
    };
    let name = |owner| if owner == 0 { "a" } else { "b" };
    let legacy = build();
    assert_eq!(legacy.clock_to(7, &[], name).at(2), 0);
    let ((), _) = composition::prove(|| {
        let c = build();
        // Four tokens may all come from b; visiting a first proves no edge.
        assert_eq!(c.clock_to(4, &[], name).at(2), 0);
        // Seven require at least three from a, including its fenced arrival.
        assert_eq!(c.clock_to(7, &[], name).at(2), 1);
        // A proved cap tightens participation, exactly as before this patch.
        assert_eq!(c.clock_to(4, &[(1, 1)], name).at(2), 1);
        let mut future = Counter::new("bell".into(), Some(vec![("a".into(), 2)]));
        future.add(1, &clock(0, 1), 0);
        let mut later = clock(0, 2);
        later.join(&clock(2, 1));
        future.add(1, &later, 0);
        assert_eq!(future.clock_to(1, &[], name).at(2), 0);
        assert_eq!(future.clock_to(2, &[], name).at(2), 1);
    });
    // The opt-in proof must not affect a subsequent ordinary single launch.
    assert_eq!(build(), legacy);
}
