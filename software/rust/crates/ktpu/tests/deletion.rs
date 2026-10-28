//! Cross-stage deletion witnesses: hand L1 bytes and memory-path image timing.
#![cfg(feature = "stack")]

use std::collections::BTreeMap;
use std::path::Path;

use ktpu_l1::{build, text};
use ktpu_l2::{lower, Program, Search, Strategy, Target};
use ktpu_machine::Machine;

fn example(level: &str, name: &str) -> String {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../language/kohakutpu/language/examples")
        .join(level);
    for sub in ["matmul", "vector", "multinode", "mover", "fused"] {
        let path = root.join(sub).join(format!("{name}.ktpu"));
        if path.exists() {
            return std::fs::read_to_string(path).unwrap();
        }
    }
    panic!("no {level} example {name}")
}

fn lowered(name: &str, machine: &Machine) -> text::Module {
    let p = Program::parse(&example("l2", name), "<example>", Some(name)).unwrap();
    let source = lower::lower(&p, &Target::of(machine), &Search::new(Strategy::Band)).unwrap();
    text::read(&source, "<lowered>").unwrap()
}

fn packages(module: &text::Module, name: &str, machine: &Machine) -> Vec<Vec<u8>> {
    let binds: BTreeMap<_, _> = module
        .body(name, "l1")
        .unwrap()
        .params
        .iter()
        .enumerate()
        .map(|(i, (p, _))| (p.clone(), 0x100000 * (i as i128 + 1)))
        .collect();
    let programs =
        build::programs(module, name, &binds, machine, Some(machine.nodes()), None).unwrap();
    assert!(!programs.is_empty());
    programs
        .iter()
        .map(|p| {
            build::package(p, machine, build::Opts::default())
                .unwrap()
                .0
                .to_bytes(&ktpu_l0::Facts::of_mesh(machine, p.mesh).unwrap())
                .unwrap()
        })
        .collect()
}

#[test]
fn six_kernels_compile_to_the_hand_packages() {
    let m = Machine::named("multimesh_v9").unwrap();
    for name in ["matmul", "longk", "gather", "scatter", "fill", "generate"] {
        let hand = text::read(&example("l1", name), "<hand>").unwrap();
        assert_eq!(
            packages(&lowered(name, &m), name, &m),
            packages(&hand, name, &m),
            "{name}"
        );
    }
}

#[test]
fn shared_fills_and_band_loops_build_packages() {
    for (name, nodes) in [
        ("share2", 2),
        ("silu", 4),
        ("add", 4),
        ("softmax", 4),
        ("layernorm", 4),
    ] {
        let m = Machine::named("multimesh_v9")
            .unwrap()
            .with_nodes(nodes)
            .unwrap();
        let l1 = lowered(name, &m);
        let bytes = packages(&l1, name, &m);
        assert!(bytes.iter().all(|b| b.len() > 96), "{name}");
    }
}

fn fill_deltas() -> (i64, i64) {
    let m = Machine::named("multimesh_v9").unwrap();
    let source = "target ktpu.v9\nimage k()\n    desc a0 = 0 walk=(32, 64)\n    vfill a0 -> l1[0]\n    vsync unpack on=fill\n    vunpk v0 <- l1[0] f16\n    halt\n";
    let cycles = |fill| ktpu_l1::sched::timing::cycles(source, "k", 32, 4, fill, &m).unwrap();
    let near = cycles((16, 1));
    (cycles((400, 1)) - near, cycles((16, 5)) - near)
}

#[test]
fn a_fill_lands_the_memory_latency_after_its_last_word() {
    assert_eq!(fill_deltas(), (384, 4 * 64));
}

#[cfg(feature = "mutants")]
#[test]
fn timing_and_hand_package_witnesses_kill_mutants() {
    assert_ne!(
        ktpu_l1::mutant::with("l1.sched/timing-flat", fill_deltas),
        (384, 4 * 64)
    );
    let m = Machine::named("multimesh_v9").unwrap();
    let hand = text::read(&example("l1", "matmul"), "<hand>").unwrap();
    let want = packages(&hand, "matmul", &m);
    let got = ktpu_l1::mutant::with("l1.lower/stream-reversed", || {
        packages(&lowered("matmul", &m), "matmul", &m)
    });
    assert_ne!(got, want);
}
