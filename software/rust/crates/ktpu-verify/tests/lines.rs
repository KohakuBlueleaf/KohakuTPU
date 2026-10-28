//! L1.VER.017: partial memory lines belong to one mover/xform stream.

use std::collections::BTreeMap;

use ktpu_isa::{Move, MoveMode, MoverIsa, Walk};
use ktpu_l0::{Facts, Package, PackageBuilder};
use ktpu_machine::Machine;
use ktpu_verify::{check, Nodes, Target};

fn machine() -> Machine {
    Machine::named("multimesh_v9t6").unwrap()
}

fn package(m: &Machine, streams: &[&[(i64, i64)]]) -> Package {
    let isa = MoverIsa::of(m);
    let f = Facts::of(m);
    let mut b = PackageBuilder::new(&f, BTreeMap::new(), 32);
    for dims in streams {
        let mv = Move {
            mode: MoveMode::Fill,
            src: None,
            dst: Some(Walk::new(0x1000, dims)),
            ewidth: 2,
            flags: 8,
            xform: (0, 0),
            imm: Some(0),
            seed: None,
            index: None,
            row: None,
            fp: false,
        };
        let writes: Vec<_> = isa
            .move_writes(&mv)
            .unwrap()
            .into_iter()
            .map(|(r, v)| (u64::from(r), v))
            .collect();
        b.mover(&writes, None, false);
    }
    b.build(false, false)
}

fn conflicts(m: &Machine, p: Package) -> usize {
    let t = Target::from_machine(m, Some(1)).unwrap();
    check(&[Nodes::from([(0, p)])], &t)
        .iter()
        .filter(|f| f.clause() == "L1.VER.017")
        .count()
}

#[test]
fn two_partial_streams_on_one_line_are_refused_even_when_ordered() {
    let m = machine();
    let mut p = package(&m, &[&[(1, 32)], &[(1, 32)]]);
    // Second stream writes the other half, not the same word.
    let at = p
        .steps
        .iter()
        .filter(|s| s.op == ktpu_l0::Op::Mover)
        .nth(1)
        .unwrap()
        .arg as usize;
    for w in &mut p.payloads[at..] {
        for j in [0, 2] {
            if w.0[j] == 0x10 && w.0[j + 1] & 1 != 0 {
                w.0[j + 1] += 32 << 4;
            }
        }
    }
    assert!(conflicts(&m, p) > 0);
}

#[test]
fn full_line_coverage_is_unioned_per_stream_not_per_word() {
    let m = machine();
    assert_eq!(conflicts(&m, package(&m, &[&[(2, 32)], &[(2, 32)]])), 0);
    assert_eq!(conflicts(&m, package(&m, &[&[(2, 32)], &[(1, 32)]])), 0);
}

#[test]
fn the_line_fact_not_the_noc_word_controls_partial_coverage() {
    let m = machine();
    let narrow = m.with_fact("card.dram.line_bytes", 32.into()).unwrap();
    let wide = m.with_fact("card.dram.line_bytes", 128.into()).unwrap();
    assert_eq!(
        conflicts(&narrow, package(&narrow, &[&[(1, 32)], &[(1, 32)]])),
        0
    );
    assert!(conflicts(&wide, package(&wide, &[&[(2, 32)], &[(2, 32)]])) > 0);
}

#[test]
fn holes_in_a_stream_are_not_full_coverage_and_engine_lowering_agrees() {
    let m = machine();
    let p = package(&m, &[&[(3, 64)], &[(3, 64)]]);
    assert!(conflicts(&m, p.clone()) > 0);
    let low = ktpu_l0::engine::lower(&p, &Facts::of(&m)).unwrap();
    assert!(conflicts(&m, low) > 0);
}

#[test]
fn bypass_transform_emits_four_words_not_one() {
    let m = machine();
    let mut b = PackageBuilder::new(&Facts::of(&m), BTreeMap::new(), 32);
    let mv = Move {
        mode: MoveMode::Xform,
        src: Some(Walk::new(0x8000, &[(4, 32)])),
        dst: Some(Walk::new(0x1000, &[(1, 128)])),
        ewidth: 2,
        flags: 8,
        xform: (0, 0),
        imm: None,
        seed: None,
        index: None,
        row: None,
        fp: false,
    };
    let writes: Vec<_> = MoverIsa::of(&m)
        .move_writes(&mv)
        .unwrap()
        .into_iter()
        .map(|(r, v)| (u64::from(r), v))
        .collect();
    b.mover(&writes, None, false);
    b.mover(&writes, None, false);
    let mut p = b.build(false, false);
    assert_eq!(conflicts(&m, p.clone()), 0);
    for w in &mut p.payloads {
        for j in [0, 2] {
            if w.0[j] == 0x18 && w.0[j + 1] & 1 == 0 {
                w.0[j + 1] += 4 << 4;
            }
        }
    }
    assert!(conflicts(&m, p) > 0);
}

#[test]
fn receive_does_not_waive_partial_mover_line_ownership() {
    use ktpu_l0::{Op, Step, Word};
    let m = machine();
    let mut p = package(&m, &[&[(1, 32)], &[(1, 32)]]);
    let address = p.payloads.len() as u64;
    p.payloads.push(Word([0x8000, 0, 0, 0]));
    p.steps.insert(
        0,
        Step::new(Op::Recv, ktpu_l0::format::ANY_UNIT as u64, 1, address, 0),
    );
    let t = Target::from_machine(&m, Some(1)).unwrap();
    assert!(ktpu_verify::lines::check(&p, 0, &t)
        .iter()
        .any(|f| f.text.contains("partially written")));
}

#[test]
fn an_unknown_engine_opcode_cannot_hide_a_write_stream() {
    use ktpu_l0::{Op, Step};
    let m = machine();
    let p = package(&m, &[&[(1, 32)]]);
    let mut low = ktpu_l0::engine::lower(&p, &Facts::of(&m)).unwrap();
    // LOAD is unknown; PATCH changes a retained non-mover opcode into any opcode.
    let at = low.steps.iter().find(|s| s.op == Op::Engine).unwrap().arg;
    low.payloads[at as usize].0[0] &= !255;
    low.steps.insert(
        0,
        Step::new(
            Op::Patch,
            0,
            at,
            ktpu_l0::format::patch_arg(0, 8, 0, false, false).unwrap(),
            0,
        ),
    );
    low.steps.insert(0, Step::new(Op::Load, 0, 0, 0, 0));
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let f = ktpu_verify::lines::check(&low, 0, &t);
    assert!(f.iter().any(|f| f.text.contains("ENGINE opcode")), "{f:?}");
}

#[test]
fn a_single_subline_scatter_has_no_competitor() {
    let m = machine();
    let mut b = PackageBuilder::new(&Facts::of(&m), BTreeMap::new(), 32);
    let mv = Move {
        mode: MoveMode::Scatter,
        src: Some(Walk::new(0x8000, &[(2, 32)])),
        dst: Some(Walk::new(0x1000, &[(2, 32)])),
        ewidth: 2,
        flags: 8,
        xform: (0, 0),
        imm: None,
        seed: None,
        index: Some((0x9000, 2)),
        row: Some((32, 1)),
        fp: false,
    };
    let writes: Vec<_> = MoverIsa::of(&m)
        .move_writes(&mv)
        .unwrap()
        .into_iter()
        .map(|(r, v)| (u64::from(r), v))
        .collect();
    b.mover(&writes, None, false);
    assert_eq!(conflicts(&m, b.build(false, false)), 0);
    b.mover(&writes, None, false);
    assert!(conflicts(&m, b.build(false, false)) > 0);
}

#[test]
fn a_dim_write_does_not_commit_without_axis() {
    let m = machine();
    let mut b = PackageBuilder::new(&Facts::of(&m), BTreeMap::new(), 32);
    // Commit one word; a staged two-word DIM must not make it a full line.
    let writes = [
        (0x10, 1 | (0x1000 << 4) | (1 << 44)),
        (0x18, 1 | (1 << 4) | (32 << 20)),
        (0x20, 0),
        (0x18, 1 | (2 << 4) | (32 << 20)),
        (0, (1 << 16) | 4 | (1 << 3)),
    ];
    b.mover(&writes, None, false);
    b.mover(&[(0, (1 << 16) | 4 | (1 << 3))], None, false);
    assert!(conflicts(&m, b.build(false, false)) > 0);
}

#[test]
fn relative_engine_mover_codes_cannot_hide_a_stream() {
    let m = machine();
    let p = package(&m, &[&[(1, 32)], &[(1, 32)]]);
    let mut low = ktpu_l0::engine::lower(&p, &Facts::of(&m)).unwrap();
    for s in low.steps.iter().filter(|s| s.op == ktpu_l0::Op::Engine) {
        for k in 0..s.count {
            let w = &mut low.payloads[(s.arg + k / 3) as usize];
            let shift = (k % 3 * 8) as u32;
            if (w.0[0] >> shift) & 255 == 8 {
                w.0[0] |= 64 << shift;
            }
        }
    }
    assert!(conflicts(&m, low) > 0);
}

#[test]
fn full_scatter_rows_require_complete_source_rows() {
    let m = machine();
    for words in [1, 2, 3, 4] {
        let mut b = PackageBuilder::new(&Facts::of(&m), BTreeMap::new(), 32);
        let mv = Move {
            mode: MoveMode::Scatter,
            src: Some(Walk::new(0x8000, &[(words, 32)])),
            dst: Some(Walk::new(0x1000, &[(words, 32)])),
            ewidth: 2,
            flags: 8,
            xform: (0, 0),
            imm: None,
            seed: None,
            index: Some((0x9000, 2)),
            row: Some((64, 2)),
            fp: false,
        };
        let writes: Vec<_> = MoverIsa::of(&m)
            .move_writes(&mv)
            .unwrap()
            .into_iter()
            .map(|(r, v)| (u64::from(r), v))
            .collect();
        b.mover(&writes, None, false);
        b.mover(&writes, None, false);
        assert_eq!(
            conflicts(&m, b.build(false, false)) > 0,
            words % 2 != 0,
            "source words {words}"
        );
    }
}

#[test]
fn header_and_window_select_the_walker_that_axis_commits() {
    let m = machine();
    for select in [(0x10, (0x8000 << 4) | (1 << 44)), (0x28, 0)] {
        let mut b = PackageBuilder::new(&Facts::of(&m), BTreeMap::new(), 32);
        b.mover(
            &[
                (0x10, 1 | (0x1000 << 4) | (1 << 44)),
                (0x18, 1 | (1 << 4) | (32 << 20)),
                (0x20, 0),
                (0x18, 1 | (2 << 4) | (32 << 20)),
                select,
                (0x20, 0),
                (0, (1 << 16) | 4 | (1 << 3)),
                (0, (1 << 16) | 4 | (1 << 3)),
            ],
            None,
            false,
        );
        assert!(
            conflicts(&m, b.build(false, false)) > 0,
            "selector {select:?}"
        );
    }
}

#[test]
fn step_patches_can_create_all_the_mover_streams() {
    use ktpu_l0::{Op, Step};
    let m = machine();
    let mut p = package(&m, &[&[(1, 32)], &[(1, 32)]]);
    for s in &mut p.steps {
        if s.op == Op::Mover {
            s.op = Op::Settle;
        }
    }
    let mut prefix = vec![Step::new(
        Op::Alu,
        0,
        0,
        Op::Mover as u64,
        ktpu_l0::format::F_IMM,
    )];
    for (i, s) in p.steps.iter().enumerate() {
        if s.op == Op::Settle {
            prefix.push(Step::new(
                Op::Patch,
                0,
                (i + 3) as u64,
                (8 << 8) | (1 << 24),
                0,
            ));
        }
    }
    assert_eq!(prefix.len(), 3);
    prefix.append(&mut p.steps);
    p.steps = prefix;
    let t = Target::from_machine(&m, Some(1)).unwrap();
    assert!(ktpu_verify::lines::check(&p, 0, &t)
        .iter()
        .any(|f| f.text.contains("partially written")));
}

fn data_loop_package(m: &Machine, text: &str, name: &str) -> Package {
    let module = ktpu_l1::text::read(text, "<data-loop>").unwrap();
    let binds = BTreeMap::from([
        ("cnt".into(), 0x10000),
        ("x".into(), 0x100000),
        ("y".into(), 0x300000),
    ]);
    let programs = ktpu_l1::build::programs(&module, name, &binds, m, Some(1), None).unwrap();
    ktpu_l1::build::Builder::new(m)
        .unwrap()
        .build(&programs[0], ktpu_l1::build::Opts::default())
        .unwrap()
        .build(false, false)
}

#[test]
fn data_dependent_copy_loop_owns_full_lines_for_every_trip_count() {
    let m = machine();
    let text = include_str!("../../../../language/kohakutpu/language/examples/l1/node/loop.ktpu");
    let p = data_loop_package(&m, text, "loop");
    let t = Target::from_machine(&m, Some(1)).unwrap();
    for package in [
        p.clone(),
        ktpu_l0::engine::lower(&p, &Facts::of(&m)).unwrap(),
    ] {
        let findings = ktpu_verify::lines::check(&package, 0, &t);
        assert!(findings.is_empty(), "{findings:?}");
        #[cfg(feature = "mutants")]
        assert!(
            !ktpu_verify::mutant::with("verify.lines/no-dynamic-full-lines", || {
                ktpu_verify::lines::check(&package, 0, &t)
            })
            .is_empty()
        );
    }
    // Half-line pitch destroys base alignment on alternate iterations.
    let bad = data_loop_package(&m, &text.replace("mul r2, 512", "mul r2, 32"), "loop");
    assert!(!ktpu_verify::lines::check(&bad, 0, &t).is_empty());
    #[cfg(feature = "mutants")]
    assert!(ktpu_verify::mutant::with("verify.lines/off", || {
        ktpu_verify::lines::check(&bad, 0, &t)
    })
    .is_empty());
}

#[test]
fn unknown_branch_checks_both_the_exit_and_the_backedge() {
    let m = machine();
    let full = unknown_loop(package(&m, &[&[(2, 32)]]));
    assert_eq!(conflicts(&m, full.clone()), 0);
    let mut bad = full;
    let partial = package(&m, &[&[(1, 32)]]);
    let offset = bad.payloads.len() as u64;
    bad.payloads.extend(partial.payloads);
    for mut step in partial.steps {
        if step.op == ktpu_l0::Op::Mover {
            step.arg += offset;
        }
        bad.steps.push(step);
    }
    assert!(conflicts(&m, bad) > 0);
}

#[test]
fn dynamic_proof_cannot_assume_unwritten_mover_configuration_is_zero() {
    let m = machine();
    let mut p = package(&m, &[&[(2, 32)]]);
    for word in &mut p.payloads {
        for half in [0, 2] {
            if word.0[half] == 0x28 {
                word.0[half] = ktpu_l0::format::MOVER_SKIP;
            }
        }
    }
    assert!(conflicts(&m, unknown_loop(p)) > 0);
}

fn tiles_package(m: &Machine) -> Package {
    let text =
        include_str!("../../../../language/kohakutpu/language/examples/l1/matmul/tiles.ktpu");
    let module = ktpu_l1::text::read(text, "<tiles>").unwrap();
    let binds = BTreeMap::from([
        ("cnt".into(), 0x10000),
        ("x".into(), 0x100000),
        ("w".into(), 0x200000),
        ("y".into(), 0x300000),
    ]);
    let programs = ktpu_l1::build::programs(&module, "tiles", &binds, m, Some(1), None).unwrap();
    ktpu_l1::build::Builder::new(m)
        .unwrap()
        .build(&programs[0], ktpu_l1::build::Opts::default())
        .unwrap()
        .build(false, false)
}

fn loop_ownership_witness(m: &Machine) -> bool {
    let t = Target::from_machine(m, Some(1)).unwrap();
    ktpu_verify::lines::check(&tiles_package(m), 0, &t).is_empty()
        && conflicts(m, unknown_loop(package(m, &[&[(1, 32)]]))) > 0
}

#[test]
fn data_dependent_tiles_loop_has_no_mover_write_footprint() {
    let m = machine();
    let p = tiles_package(&m);
    let t = Target::from_machine(&m, Some(1)).unwrap();
    assert!(p.steps.iter().any(|s| s.op == ktpu_l0::Op::Jump));
    for p in [
        p.clone(),
        ktpu_l0::engine::lower(&p, &Facts::of(&m)).unwrap(),
    ] {
        assert!(!p.steps.iter().any(|s| s.op == ktpu_l0::Op::Mover));
        assert!(p.steps.iter().any(|s| s.op == ktpu_l0::Op::Patch));
        let findings = ktpu_verify::lines::check(&p, 0, &t);
        assert!(findings.is_empty(), "{findings:?}");
    }
    assert!(loop_ownership_witness(&m));
    #[cfg(feature = "mutants")]
    assert!(!ktpu_verify::mutant::with("verify.lines/off", || {
        loop_ownership_witness(&m)
    }));
}

fn unknown_loop(mut p: Package) -> Package {
    use ktpu_l0::{Op, Step};
    p.steps.insert(0, Step::new(Op::Load, 1, 0, 0, 0));
    p.steps.push(Step::new(
        Op::Jump,
        1,
        3 | (1 << 8),
        1,
        ktpu_l0::format::F_IMM,
    ));
    p
}

#[test]
fn data_dependent_loops_with_unproven_partial_writes_still_refuse() {
    let m = machine();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let p = package(&m, &[&[(1, 32)]]);
    for p in [
        unknown_loop(p.clone()),
        unknown_loop(ktpu_l0::engine::lower(&p, &Facts::of(&m)).unwrap()),
    ] {
        let findings = ktpu_verify::lines::check(&p, 0, &t);
        assert!(
            findings
                .iter()
                .any(|f| f.text.contains("data-dependent node loop")),
            "{findings:?}"
        );
        #[cfg(feature = "mutants")]
        assert!(ktpu_verify::mutant::with("verify.lines/off", || {
            ktpu_verify::lines::check(&p, 0, &t)
        })
        .is_empty());
    }
}

fn masked_engine_streams(m: &Machine) -> (Package, Vec<(u64, u32, u64)>) {
    let mut p =
        ktpu_l0::engine::lower(&package(m, &[&[(1, 32)], &[(1, 32)]]), &Facts::of(m)).unwrap();
    let mut codes = Vec::new();
    for s in p.steps.iter().filter(|s| s.op == ktpu_l0::Op::Engine) {
        for k in 0..s.count {
            let i = s.arg + k / 3;
            let bit = (k % 3 * 8) as u32;
            let w = &mut p.payloads[i as usize];
            let code = w.get(bit, 8);
            if (8..24).contains(&code) {
                codes.push((i, bit, code));
                w.0[0] &= !(255 << bit);
            }
        }
    }
    assert!(!codes.is_empty());
    (p, codes)
}

#[test]
fn data_dependent_engine_value_patches_preserve_zero_write_footprint() {
    use ktpu_l0::{Op, Step};
    let m = machine();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let (mut p, codes) = masked_engine_streams(&m);
    assert!(p.steps.iter().any(|s| s.op == Op::Engine));
    p.steps.insert(
        0,
        Step::new(
            Op::Patch,
            1,
            codes[0].0,
            ktpu_l0::format::patch_arg(64, 64, 0, false, false).unwrap(),
            0,
        ),
    );
    let findings = ktpu_verify::lines::check(&unknown_loop(p), 0, &t);
    assert!(findings.is_empty(), "{findings:?}");
}

#[test]
fn payload_patches_can_introduce_every_engine_mover_opcode() {
    use ktpu_l0::{Op, Step};
    let m = machine();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    for unknown in [false, true] {
        let (mut p, codes) = masked_engine_streams(&m);
        let mut prefix = Vec::new();
        for (i, bit, code) in codes {
            prefix.push(if unknown {
                Step::new(Op::Load, 0, 0, 0, 0)
            } else {
                Step::new(Op::Alu, 0, 0, code, ktpu_l0::format::F_IMM)
            });
            prefix.push(Step::new(
                Op::Patch,
                0,
                i,
                ktpu_l0::format::patch_arg(u64::from(bit), 8, 0, false, false).unwrap(),
                0,
            ));
        }
        prefix.append(&mut p.steps);
        p.steps = prefix;
        let findings = ktpu_verify::lines::check(&p, 0, &t);
        let expected = if unknown {
            "ENGINE opcode"
        } else {
            "partially written"
        };
        assert!(
            findings.iter().any(|f| f.text.contains(expected)),
            "{findings:?}"
        );
    }
}

#[test]
fn receive_stores_to_dram_not_engine_payload() {
    use ktpu_l0::{Op, Step, Word};
    let m = machine();
    let t = Target::from_machine(&m, Some(1)).unwrap();
    let (empty, _) = masked_engine_streams(&m);
    let full = package(&m, &[&[(2, 32)]]);
    for mut p in [
        empty,
        full.clone(),
        ktpu_l0::engine::lower(&full, &Facts::of(&m)).unwrap(),
    ] {
        let address = p.payloads.len() as u64;
        p.payloads.push(Word([0x8000, 0, 0, 0]));
        p.steps.insert(
            0,
            Step::new(Op::Recv, ktpu_l0::format::ANY_UNIT as u64, 1, address, 0),
        );
        for p in [p.clone(), unknown_loop(p)] {
            assert!(ktpu_verify::lines::check(&p, 0, &t).is_empty());
            assert!(ktpu_verify::lines::admit(&p, 0, &t).is_ok());
        }
    }
    let mut p = package(&m, &[&[(2, 32)]]);
    let address = p.payloads.len() as u64;
    p.payloads.push(Word([0x8000, 0, 0, 0]));
    p.steps.insert(
        0,
        Step::new(Op::Recv, ktpu_l0::format::ANY_UNIT as u64, 1, address, 0),
    );
    #[cfg(feature = "mutants")]
    assert!(
        ktpu_verify::mutant::with("verify.lines/recv-is-payload", || {
            ktpu_verify::lines::admit(&p, 0, &t)
        })
        .is_err()
    );
    // RX bytes read from DRAM do not become known just because RECV itself
    // leaves the command payload alone: an unknown destination is unprovable.
    let (payload, bit) = p
        .payloads
        .iter()
        .enumerate()
        .find_map(|(i, w)| {
            [0, 2]
                .into_iter()
                .find(|&j| w.0[j] == 0x10 && w.0[j + 1] & 1 != 0)
                .map(|j| (i as u64, (j + 1) * 64))
        })
        .unwrap();
    p.steps.insert(1, Step::new(Op::Load, 1, 0, address, 0));
    p.steps.insert(
        2,
        Step::new(
            Op::Patch,
            1,
            payload,
            ktpu_l0::format::patch_arg(bit as u64, 64, 0, false, false).unwrap(),
            0,
        ),
    );
    assert!(ktpu_verify::lines::admit(&p, 0, &t).is_err());
    assert!(!ktpu_verify::lines::check(&p, 0, &t).is_empty());
}

#[cfg(feature = "mutants")]
#[test]
fn disabling_the_line_rule_is_killed() {
    let m = machine();
    assert_eq!(
        ktpu_verify::mutant::with("verify.lines/off", || {
            conflicts(&m, package(&m, &[&[(1, 32)], &[(1, 32)]]))
        }),
        0
    );
}
