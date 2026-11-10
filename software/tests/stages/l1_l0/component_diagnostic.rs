//! A real production-cap refusal, not a test-only lower budget. Exercise both
//! private and placed package fetches; this is a diagnostic witness, not a claim
//! that physical-package component growth has been repaired.

use super::super::package;
use crate::common::card;
use ktpu_l0::program::Program;
use ktpu_verify::{composition, Kind, MemoryContext, Nodes, PackageStorage, Target};

fn remote_unacquired(count: usize) -> Nodes {
    let machine = card();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let slot = u64::from(machine.il.slots - 1);
    let mut writer = Program::new();
    let mut peer = Program::new();
    // A real completed join first: its last gather/release marks must appear.
    peer.ring(0, slot, 1, true);
    writer.wait_slot(slot, 1, true);
    writer.ring(1, slot, 1, true);
    peer.wait_slot(slot, 1, true);
    let base = 4096 | (1u64 << machine.die.address.runtime().mesh_shift);
    for _ in 0..count {
        writer.post(vec![
            (0x10, (base << 4) | (2 << 44) | 1),
            (0x18, (2 << 4) | (32 << 20) | 1),
            (0x20, 0),
            (0x18, (4096 << 4) | (128 << 20) | 3),
            (0x20, 0),
            (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
        ], vec![]);
        writer.barrier();
    }
    // Local mover completion does not acquire its remote response. All writes
    // are FIFO WAW on the same response stream, but their unordered envelopes
    // deliberately remain connected until this final publication.
    writer.ring(1, 9, 1, true);
    peer.wait_slot(9, 1, true);
    Nodes::from([(0, package(&writer, 0)), (1, package(&peer, 1))])
}

#[test]
fn component_refusal_is_bounded_deterministic_and_self_describing() {
    let machine = card();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let graph = remote_unacquired(256);
    for placed in [false, true] {
        let mut memory = MemoryContext::default();
        if placed {
            // Disjoint from each other and from the stripe, with ample space.
            for mesh in 0..2 {
                memory.packages.insert((mesh, 0), PackageStorage {
                    steps: 0x1000_0000 + u64::from(mesh) * 0x100_0000,
                    payloads: 0x1080_0000 + u64::from(mesh) * 0x100_0000,
                });
            }
        }
        let run = || {
            let (found, _) = composition::prove(|| {
                ktpu_verify::check_with_memory(std::slice::from_ref(&graph), &target, &memory)
            });
            let budget: Vec<_> = found.iter().filter(|f| f.kind == Kind::Model
                && f.text.starts_with("global DRAM proof budget exhausted")).collect();
            assert!(!budget.is_empty(), "expected unchanged production cap: {found:?}");
            for f in &budget {
                assert!(f.text.is_ascii() && f.text.len() <= 768, "{}", f.text);
                let gap_owner = f.text.split(" gap_owner=").nth(1)
                    .expect("refusal identifies the missing causal process");
                assert!(gap_owner.split(" J").next().unwrap().contains("responses"),
                    "the unacquired remote response must be named: {}", f.text);
            }
            budget.iter().map(|f| f.text.clone()).collect::<Vec<_>>()
        };
        assert_eq!(run(), run(), "first-refusal evidence must be deterministic");
    }
}
