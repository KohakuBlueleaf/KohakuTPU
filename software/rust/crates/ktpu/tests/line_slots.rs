//! Indexed lanes use replicated line slots, without changing caller tables.
#![cfg(feature = "stack")]

use ktpu::{compile, Cache, Input, Stack};
use ktpu_graph as g;
use ktpu_machine::Machine;

fn graph(m: &Machine, keep: bool) -> g::Graph {
    let p = ktpu_kernels::gen::lanes::plan(
        4096,
        &[(128, 17), (7, 1)],
        3,
        &[(128, 16), (7, 1)],
        5,
        4096,
        keep,
        m,
    )
    .unwrap();
    ktpu_kernels::relayout_kernel(&p, m).unwrap().graph
}

fn compiled(g: &g::Graph, m: &Machine) -> Result<ktpu::Compiled, ktpu::Refused> {
    compile(
        &Stack,
        &Input::Graph(serde_json::to_string(g).unwrap()),
        None,
        m,
        &Cache::off(),
    )
}

#[test]
fn indexed_lanes_own_full_lines_with_unchanged_tables_and_compact_once() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    for keep in [false, true] {
        let g = graph(&m, keep);
        let out = compiled(&g, &m).unwrap();
        assert!(!out.verdict.refused, "{:?}", out.verdict);
        let slots = ktpu::stack::lines::isolate(g.clone(), &m);
        assert_eq!(slots.ports, g.ports);
        let movers = |g: &g::Graph| {
            g.body
                .iter()
                .filter_map(|s| match &s.node {
                    g::Node::Mover {
                        op: g::MoverOp::Gather | g::MoverOp::Scatter,
                        kw,
                        flags,
                        ..
                    } => Some((kw.clone(), flags.clone())),
                    _ => None,
                })
                .collect::<Vec<_>>()
        };
        assert_eq!(
            movers(&slots),
            movers(&g),
            "index tables are logical row numbers, pitch follows the expanded row"
        );
        assert_eq!(
            slots
                .body
                .iter()
                .filter(|s| matches!(&s.node,
            g::Node::Mover { op: g::MoverOp::Copy, dst, .. } if dst.buffer == "dst"))
                .count(),
            1
        );
        for scratch in &g.scratch {
            let new = slots
                .scratch
                .iter()
                .find(|s| s.name == scratch.name)
                .unwrap();
            assert_eq!(
                new.spec.shape,
                [scratch.spec.shape[0], scratch.spec.shape[1] * 2]
            );
        }
        // Independent byte-address witness of the emitted prefix walk.
        let prefix = slots
            .body
            .iter()
            .find(|s| {
                matches!(&s.node,
            g::Node::Mover { op: g::MoverOp::Copy, src: Some(src), .. } if src.buffer == "src")
            })
            .unwrap();
        let g::Node::Mover { kw, .. } = &prefix.node else {
            unreachable!()
        };
        assert_eq!(kw[0].1, g::Attr::Walk(vec![(256, 32), (2, 0)]));
        assert_eq!(kw[1].1, g::Attr::Walk(vec![(256, 64), (2, 32)]));
        let source: Vec<u8> = (0..8192)
            .map(|i| ((i / 32 * 37 + i % 32) % 251) as u8)
            .collect();
        let mut expanded = vec![0; 16384];
        for q in 0..256 {
            for k in 0..2 {
                expanded[64 * q + 32 * k..64 * q + 32 * (k + 1)]
                    .copy_from_slice(&source[32 * q..32 * (q + 1)]);
            }
        }
        for q in [0, 1, 15, 17, 255] {
            for k in 0..64 {
                assert_eq!(expanded[q * 64 + k], ((q * 37 + k % 32) % 251) as u8);
            }
        }
    }
}

#[cfg(feature = "mutants")]
#[test]
fn word_slots_and_vector_count_mutants_are_killed() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let g = graph(&m, true);
    let broken = ktpu::mutant::with("stack.layout/no-line-slots", || compiled(&g, &m))
        .unwrap()
        .unwrap();
    assert!(broken.verdict.refused);
    assert!(format!("{:?}", broken.verdict).contains("L1.VER.017"));
    for mutant in ["l2.vector/word-row-count", "l2.vector/no-line-mask-cycle"] {
        let result = ktpu::mutant::with(mutant, || compiled(&g, &m)).unwrap();
        assert!(result.is_err(), "{mutant} survived");
    }
}

#[test]
fn unsupported_graphs_and_names_are_left_for_the_verifier() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let mut g = graph(&m, false);
    g.ports[2].name = "line_slot_src".into();
    assert_eq!(ktpu::stack::lines::isolate(g.clone(), &m), g);
    g.ports[2].name = "index".into();
    if let g::Node::Mover { kw, .. } = &mut g.body[1].node {
        kw.push(("pitch".into(), g::Attr::Int(32)));
    }
    assert_eq!(ktpu::stack::lines::isolate(g.clone(), &m), g);
}
