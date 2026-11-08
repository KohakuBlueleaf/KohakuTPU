//! Card-wide physical accesses and the releases that order them.

use std::collections::BTreeMap;

use ktpu::{Backend, CardStack};
use ktpu_isa::unit::cluster::{ClusterInst, Drain, Fill};
use ktpu_isa::unit::vector::{VecCu, VecIsa};
use ktpu_l0::program::{BuildOpts, Program};
use ktpu_l0::{Facts, Package, Word, WordOps};
use ktpu_verify::{Finding, Kind, Nodes, Target};

use super::common::card;

fn package(p: &Program, mesh: u32) -> Package {
    p.build(
        &Facts::of_mesh(&card(), mesh).expect("facts"),
        BuildOpts::default(),
    )
    .expect("package")
    .build(false, false)
}

fn dma(base: u64, write: bool) -> Vec<Word> {
    let source = if write {
        "vdrain a0 <- l1[0]\nhalt\n"
    } else {
        "vfill a0 -> l1[0]\nhalt\n"
    };
    image_dma(base, source)
}

fn image_dma(base: u64, source: &str) -> Vec<Word> {
    let machine = card();
    let isa = VecIsa::new(&machine).expect("vector ISA");
    let core = ktpu_l1::lower::image::CoreFacts::of(&machine, machine.vc.regs);
    let reader = ktpu_l1::text::Reader::new(source, "memory access");
    let stmts = ktpu_l1::text::parse(source, "memory access").expect("image");
    let words = ktpu_l1::lower::image::Encoder::new(
        &reader,
        &isa,
        &core,
        ktpu_l1::lower::image::UNKNOWN,
        None,
    )
    .encode(&stmts)
    .expect("encoded image");
    let mut cu = Vec::new();
    for (fld, value) in [(0, base), (1, (32 << 16) | 2), (2, 0), (3, 0), (4, 0)] {
        cu.push(
            VecCu::Desc { ad: 0, fld, value }
                .encode(&isa)
                .expect("descriptor"),
        );
    }
    for (addr, word) in words.into_iter().enumerate() {
        cu.push(
            VecCu::Imem {
                addr: addr as u32,
                word,
            }
            .encode(&isa)
            .expect("IMEM"),
        );
    }
    cu.push(VecCu::Run { pc: 0 }.encode(&isa).expect("RUN"));
    cu
}

fn verify(programs: &[Program]) -> Vec<Finding> {
    verify_with_memory(programs, &ktpu_verify::MemoryContext::default())
}

fn verify_with_memory(programs: &[Program], context: &ktpu_verify::MemoryContext) -> Vec<Finding> {
    let nodes: Nodes = programs
        .iter()
        .enumerate()
        .map(|(mesh, p)| (mesh as u32, package(p, mesh as u32)))
        .collect();
    let target = Target::from_machine(&card(), Some(programs.len() as u32)).expect("target");
    ktpu_verify::check_with_memory(&[nodes.clone(), nodes], &target, context)
}

fn clean(findings: &[Finding]) {
    assert!(!findings.iter().any(|f| f.kind.refuses()), "{findings:?}");
}

fn hazard(findings: &[Finding], kind: Kind) {
    assert!(
        findings
            .iter()
            .any(|f| f.kind == kind && f.at == "shared physical memory"),
        "expected a shared-memory hazard: {findings:?}"
    );
}

#[test]
fn physical_aliases_and_partial_lines_are_card_wide() {
    let machine = card();
    let address = machine.die.address.runtime();
    let route = 1u64 << address.mesh_shift;
    let special = 1u64 << address.special_bit;
    let uncached = 1u64 << address.reserved_bit;
    for (a, b, race) in [
        (4096, 4096 | route, Some(Kind::Race)),
        (4096, 4096 | uncached, Some(Kind::Race)),
        (4096, 4104 | route, None),
        (4096, 4160 | route, None),
        (special | 4096, special | route | 4096, None),
    ] {
        let mut p = Program::new();
        let mut q = Program::new();
        p.store(vec![(a, 1)], false);
        q.store(vec![(b, 2)], false);
        let findings = verify(&[p, q]);
        match race {
            Some(kind) => assert!(
                findings
                    .iter()
                    .any(|f| f.kind == kind && f.at == "shared physical memory"),
                "CPU stores at {a:#x} and {b:#x}: expected {kind:?}, got {findings:?}"
            ),
            None => assert!(
                !findings.iter().any(|f| f.kind.refuses()),
                "CPU stores at {a:#x} and {b:#x}: expected no refusal, got {findings:?}"
            ),
        }
    }
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let movers: Vec<_> = [4096, 4128 | route]
        .into_iter()
        .map(|at| {
            let mut p = Program::new();
            p.move_(
                vec![
                    (0x10, (at << 4) | (1 << 44) | 1),
                    (0x18, (1 << 4) | (32 << 20) | 1),
                    (0x20, 0),
                    (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
                ],
                vec![],
            );
            p
        })
        .collect();
    let findings = verify(&movers);
    hazard(&findings, Kind::Line);
    assert!(
        !findings.iter().any(|f| f.kind == Kind::Race),
        "{findings:?}"
    );
}

#[test]
fn a_release_covers_the_union_of_independent_producers() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let a = facts.units["VC"][0];
    let b = facts.units["VC"][1];
    for union in [false, true] {
        let mut producer = Program::new();
        producer.send(a, dma(4096, true), vec![]);
        producer.send(b, dma(4224, true), vec![]);
        if union {
            producer.wait(a, None).expect("A completion");
        }
        producer.wait(b, None).expect("B completion");
        producer.ring(1, 9, 1, true);
        let mut consumer = Program::new();
        consumer.wait_slot(9, 1, true);
        let alias = 1u64 << machine.die.address.runtime().reserved_bit;
        consumer.load(0, 4096 | alias, None, false);
        consumer.load(1, 4224 | alias, None, false);
        let findings = verify(&[producer, consumer]);
        if union {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
            assert!(
                findings.iter().any(|f| f.text.contains("VDRAIN")),
                "{findings:?}"
            );
        }
    }
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let mg = facts.units["MG"][0];
    for union in [false, true] {
        let mut producer = Program::new();
        let private = ClusterInst::Fill(Fill {
            addr: 4096,
            n: 1,
            ..Fill::default()
        });
        producer.send(
            mg,
            vec![private.encode(&target.isa.cluster).expect("private fill")],
            vec![],
        );
        producer.send(b, dma(4224, true), vec![]);
        if union {
            producer.wait(mg, None).expect("private read completion");
        }
        producer
            .wait(b, None)
            .expect("independent output completion");
        producer.ring(1, 9, 1, true);
        let receive = ClusterInst::Fill(Fill {
            sel: 1,
            n: 1,
            recv: true,
            ..Fill::default()
        });
        producer.send(
            mg,
            vec![receive.encode(&target.isa.cluster).expect("peer marker")],
            vec![],
        );
        let mut consumer = Program::new();
        let local = 4096 | (1u64 << machine.die.address.runtime().mesh_shift);
        consumer
            .wait_slot(9, 1, true)
            .store(vec![(local, 1)], false);
        consumer.load(0, 4224, None, false);
        let peer = ClusterInst::Drain(Drain {
            n: 4,
            dst: Some(machine.die.sys_ports[0]),
            fin: Some(machine.die.units.mg[0]),
            mesh: 0,
            buf: ktpu_isa::unit::cluster::BUF_L1B,
            dflags: ktpu_isa::unit::cluster::DFLAG_COUNT,
            ..Drain::default()
        });
        consumer.send(
            mg,
            vec![peer.encode(&target.isa.cluster).expect("peer stream")],
            vec![],
        );
        let findings = verify(&[producer, consumer]);
        if union {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
            assert!(
                findings
                    .iter()
                    .any(|f| f.kind == Kind::Port && f.text.contains("responses arrive")),
                "{findings:?}"
            );
        }
    }
}

#[test]
fn submission_and_release_without_acquire_do_not_order_a_consumer() {
    for acquire in [false, true] {
        let mut producer = Program::new();
        producer.store(vec![(4096, 1)], false).ring(1, 9, 1, true);
        let mut consumer = Program::new();
        if acquire {
            consumer.wait_slot(9, 1, true);
        }
        consumer.load(0, 4096, None, false);
        let findings = verify(&[producer, consumer]);
        if acquire {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
        }
    }
}

#[test]
fn credit_reuse_waits_for_the_consuming_read_not_its_send() {
    let machine = card();
    let unit = Facts::of_mesh(&machine, 1).expect("facts").units["VC"][0];
    for completed in [false, true] {
        let mut producer = Program::new();
        producer.store(vec![(4096, 1)], false).ring(1, 9, 1, true);
        producer
            .wait_slot(10, 1, true)
            .store(vec![(4096, 2)], false);
        let mut consumer = Program::new();
        consumer
            .wait_slot(9, 1, true)
            .send(unit, dma(4096, false), vec![]);
        if completed {
            consumer.wait(unit, None).expect("consumer completion");
        }
        consumer.ring(0, 10, 1, true);
        let findings = verify(&[producer, consumer]);
        if completed {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
        }
    }
}

#[test]
fn a_late_ring_cannot_publish_a_new_slot_owner() {
    for separate in [false, true] {
        let mut old = Program::new();
        old.ring(2, 9, 1, true)
            .wait_slot(10, 1, true)
            .ring(2, 9, 1, true);
        let mut new = Program::new();
        let slot = if separate { 12 } else { 9 };
        let local = 4096 | (1u64 << card().die.address.runtime().mesh_shift);
        new.wait_slot(11, 1, true)
            .store(vec![(local, 1)], false)
            .ring(2, slot, 1, true);
        let mut consumer = Program::new();
        consumer
            .wait_slot(9, 1, true)
            .ring(0, 10, 1, true)
            .ring(1, 11, 1, true);
        consumer.wait_slot(slot, 1, true).load(0, 4096, None, false);
        consumer.wait_slot(9, 1, true);
        let findings = verify(&[old, new, consumer]);
        if separate {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
        }
    }
}

#[test]
fn unresolved_indexed_cpu_access_is_not_a_certificate() {
    let mut p = Program::new();
    p.load(0, 4096, None, false);
    p.load(1, 8192, Some(0), false);
    let nodes = BTreeMap::from([(0, package(&p, 0))]);
    let target = Target::from_machine(&card(), Some(1)).expect("target");
    let result = ktpu_verify::gate(&[nodes], &target, ktpu_verify::Mode::Refuse, None);
    assert!(
        matches!(result, Err(ktpu_verify::GateError::Refused(ref fs))
            if fs.iter().any(|f| f.kind == Kind::Model && f.text.contains("unknown LOAD footprint"))),
        "{result:?}"
    );
}

#[test]
fn mg_reads_and_drains_conflict_with_remote_cpu_protocol_accesses() {
    let machine = card();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let unit = Facts::of_mesh(&machine, 0).expect("facts").units["MG"][0];
    for write in [false, true] {
        for ordered in [false, true] {
            let instruction = if write {
                ClusterInst::Drain(Drain {
                    addr: 4096,
                    n: 2,
                    ..Drain::default()
                })
            } else {
                ClusterInst::Fill(Fill {
                    addr: 4096,
                    n: 1,
                    ..Fill::default()
                })
            };
            let mut mg = Program::new();
            mg.send(
                unit,
                vec![instruction.encode(&target.isa.cluster).expect("MG word")],
                vec![],
            );
            if ordered {
                mg.wait(unit, None).expect("MG completion");
            }
            mg.ring(1, 9, 1, true);
            let mut cpu = Program::new();
            cpu.wait_slot(9, 1, true);
            if write {
                cpu.load(0, 4096, None, false);
            } else {
                let local = 4096 | (1u64 << machine.die.address.runtime().mesh_shift);
                cpu.store(vec![(local, 1)], false);
            }
            let findings = verify(&[mg, cpu]);
            if ordered {
                clean(&findings);
            } else {
                hazard(&findings, Kind::Race);
            }
        }
    }
}

#[test]
fn posted_mover_completion_is_required_before_publication() {
    let machine = card();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    for (waited, fence) in [(false, false), (true, false), (false, true)] {
        let mut producer = Program::new();
        let token = producer.post(
            vec![
                (0x10, (4096 << 4) | (1 << 44) | 1),
                (0x18, (2 << 4) | (32 << 20) | 1),
                (0x20, 0),
                (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
            ],
            vec![],
        );
        if waited {
            producer.wait_moves(token).expect("mover completion");
        }
        producer.ring(1, 9, 1, fence);
        let mut consumer = Program::new();
        consumer.wait_slot(9, 1, true).load(0, 4096, None, false);
        let findings = verify(&[producer, consumer]);
        if waited || fence {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
        }
    }
}

#[test]
fn card_path_checks_all_sysnodes_before_certifying_retained_packages() {
    let machine = card();
    for (destination, collision) in [(4096, true), (4160, false)] {
        let source = format!(
            "fn shared.l1()\n    node 0\n        send mg[0]\n            drain 4096 n=2\n    node 1\n        send mg[0]\n            drain {destination} n=2\n"
        );
        let backend = CardStack::default();
        let module = backend.parse_l1(&source).expect("card L1");
        let programs = backend
            .programs(&module, "shared", None, &machine)
            .expect("programs");
        let findings = backend
            .check(&programs, &machine)
            .expect("cold and retained check");
        if collision {
            assert!(findings.iter().any(|f| f.kind == "race"), "{findings:?}");
        } else {
            assert!(!findings.iter().any(|f| backend.refuses(f)), "{findings:?}");
        }
    }
}

#[test]
fn full_line_indexed_mover_still_requires_a_global_domain_proof() {
    let target = Target::from_machine(&card(), Some(1)).expect("target");
    let mut p = Program::new();
    let token = p.post(
        vec![
            (0x10, (8192 << 4) | (1 << 44)),
            (0x18, (2 << 4) | (32 << 20)),
            (0x20, 0),
            (0x10, (4096 << 4) | 1),
            (0x50, 64 | (2 << 32)),
            (0x30, 12288 | (1 << 40)),
            (0, (1 << 16) | u64::from(target.isa.mover.modes.scatter)),
        ],
        vec![],
    );
    p.wait_moves(token).expect("indexed completion");
    let nodes = BTreeMap::from([(0, package(&p, 0))]);
    let scheduled = ktpu_verify::schedule(&nodes, &ktpu_verify::Held::new(), &target)
        .expect("dispatch progress is independent of the index bytes");
    let findings = ktpu_verify::check_scheduled(&scheduled, &[], &target);
    assert!(
        !findings.iter().any(|f| f.kind == Kind::Deadlock),
        "{findings:?}"
    );
    let mut context = ktpu_verify::MemoryContext::default();
    context.initial.insert(12288, 0u32.to_le_bytes().to_vec());
    clean(&ktpu_verify::check_with_memory(
        std::slice::from_ref(&nodes),
        &target,
        &context,
    ));
    assert!(
        findings.iter().any(|f| f.kind == Kind::Model
            && f.kind.refuses()
            && f.text.contains("index-domain disjointness proof")),
        "{findings:?}"
    );
    let mut rejected = Program::new();
    rejected.alu(0, ktpu_l0::Alu::Mov, 0, 1, true);
    rejected
        .steps
        .push(ktpu_l0::program::Stmt::AssertZero { reg: 0, code: 1 });
    let findings = ktpu_verify::check(
        &[nodes, BTreeMap::from([(0, package(&rejected, 0))])],
        &target,
    );
    for reason in [
        "index-domain disjointness proof",
        "contract 1 cannot succeed",
    ] {
        assert!(
            findings
                .iter()
                .any(|f| f.kind == Kind::Model && f.text.contains(reason)),
            "{findings:?}"
        );
    }
}

#[test]
fn self_modification_requires_package_storage_isolation() {
    for (steps, alias) in [
        (false, 0),
        (true, 0),
        (false, 1u64 << card().die.address.runtime().reserved_bit),
    ] {
        let mut program = Program::new();
        program.store(vec![(4096, 1)], false);
        let mut patched = package(&program, 0);
        let flags = if steps { 1 << 24 } else { 0 };
        patched.steps.insert(
            0,
            ktpu_l0::Step::new(
                ktpu_l0::Op::Patch,
                0,
                u64::from(steps),
                64 | (64 << 8) | flags,
                0,
            ),
        );
        let mut peer = Program::new();
        peer.load(0, if steps { 32768 + 24 } else { 16384 + 8 }, None, false);
        let target = Target::from_machine(&card(), Some(2)).expect("target");
        let nodes = BTreeMap::from([(0, patched), (1, package(&peer, 1))]);
        clean(&ktpu_verify::check(std::slice::from_ref(&nodes), &target));
        let mut context = ktpu_verify::MemoryContext::default();
        context.packages.insert(
            (0, 0),
            ktpu_verify::PackageStorage {
                steps: 32768 | alias,
                payloads: 16384 | alias,
            },
        );
        let findings = ktpu_verify::check_with_memory(&[nodes], &target, &context);
        hazard(&findings, Kind::Race);
    }
}

#[test]
fn local_drain_retirement_does_not_order_an_external_read() {
    let machine = card();
    let unit = Facts::of_mesh(&machine, 0).expect("facts").units["VC"][0];
    let mut unfenced = Program::new();
    unfenced.send(
        unit,
        image_dma(
            4096,
            "vdrain a0 <- l1[0]\nvsync fill on=drain\nvfill a0 -> l1[0]\nhalt\n",
        ),
        vec![],
    );
    hazard(&verify(&[unfenced]), Kind::Race);
    let mut fenced = Program::new();
    fenced.send(unit, dma(4096, true), vec![]);
    fenced
        .wait(unit, None)
        .expect("endpoint acknowledges writes");
    fenced.send(unit, dma(4096, false), vec![]);
    clean(&verify(&[fenced]));
}

#[test]
fn remote_store_is_published_by_a_fenced_ring_not_its_posted_reply() {
    let machine = card();
    let a = machine.die.address.runtime();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    for aperture in [0, 1u64 << a.special_bit] {
        let remote = aperture | (1u64 << a.mesh_shift) | 4096;
        for mover in [false, true] {
            for fence in [false, true] {
                let mut producer = Program::new();
                if mover {
                    producer.move_(
                        vec![
                            (0x10, (remote << 4) | (1 << 44) | 1),
                            (0x18, (2 << 4) | (32 << 20) | 1),
                            (0x20, 0),
                            (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
                        ],
                        vec![],
                    );
                } else {
                    producer.store(vec![(remote, 64)], false);
                }
                producer.ring(1, 9, 1, fence);
                let mut consumer = Program::new();
                consumer.wait_slot(9, 1, true).load(0, remote, None, false);
                if !mover {
                    // The remote STORE must publish its known bytes with the
                    // same response event as the memory access, not an older
                    // local response. Unresolved values cannot prove this read.
                    consumer.load(1, 8192, Some(0), false);
                }
                let findings = verify(&[producer, consumer]);
                if fence {
                    clean(&findings);
                } else {
                    hazard(&findings, Kind::Race);
                }
            }
        }
    }
}

#[test]
fn an_ordered_store_into_a_placed_descriptor_changes_the_executed_footprint() {
    let mut program = Program::new();
    program.load(0, 4096, None, false);
    let p = package(&program, 0);
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    let mut writer = Program::new();
    writer.store(vec![(16384, 8192)], false).ring(0, 9, 1, true);
    let mut p = p;
    p.steps
        .insert(0, ktpu_l0::Step::new(ktpu_l0::Op::WaitSlot, 9, 1, 0, 32));
    let mut other = Program::new();
    other.store(vec![(8192, 0)], false);
    let q = package(&writer, 1);
    let nodes = BTreeMap::from([(0, p), (1, q), (2, package(&other, 2))]);
    let target = Target::from_machine(&target.machine, Some(3)).expect("three participants");
    let mut context = ktpu_verify::MemoryContext::default();
    context.packages.insert(
        (0, 0),
        ktpu_verify::PackageStorage {
            steps: 32768,
            payloads: 16384,
        },
    );
    let findings = ktpu_verify::check_with_memory(&[nodes], &target, &context);
    hazard(&findings, Kind::Race);
    assert!(
        findings.iter().any(|f| f.text.contains("0x2000")),
        "{findings:?}"
    );
}

#[test]
fn cached_certificates_do_not_cross_physical_alias_environments() {
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    let directory = std::env::temp_dir().join(format!("ktpu-dram-{}-{stamp}", std::process::id()));
    std::fs::create_dir_all(&directory).expect("proof cache");
    for (other, collision) in [(8192, false), (4096, true)] {
        let mut a = Program::new();
        let mut b = Program::new();
        a.store(vec![(4096, 1)], false);
        b.store(vec![(other, 2)], false);
        let nodes = BTreeMap::from([(0, package(&a, 0)), (1, package(&b, 1))]);
        let result = ktpu_verify::gate(
            &[nodes],
            &target,
            ktpu_verify::Mode::Refuse,
            Some((&directory, "retained")),
        );
        if collision {
            assert!(
                matches!(result, Err(ktpu_verify::GateError::Refused(ref fs))
                if fs.iter().any(|f| f.kind == Kind::Race)),
                "{result:?}"
            );
        } else {
            result.expect("disjoint writers certify");
        }
    }
    std::fs::remove_dir_all(directory).expect("remove proof cache");
}

#[test]
fn footprint_budget_exhaustion_cannot_report_clean() {
    let target = Target::from_machine(&card(), Some(1)).expect("target");
    let mut p = Program::new();
    let token = p.post(
        vec![
            (0x10, 4096 << 4 | 2 << 44 | 1),
            (0x18, 65535 << 4 | 32 << 20 | 1),
            (0x20, 0),
            (0x18, 65535 << 4 | 2097120 << 20 | 3),
            (0x20, 0),
            (0, 1 << 16 | u64::from(target.isa.mover.modes.fill)),
        ],
        vec![],
    );
    p.wait_moves(token).expect("bounded command completion");
    let nodes = BTreeMap::from([(0, package(&p, 0))]);
    let scheduled = ktpu_verify::schedule(&nodes, &ktpu_verify::Held::new(), &target)
        .expect("a proof budget is not a dispatch deadlock");
    let findings = ktpu_verify::check_scheduled(&scheduled, &[], &target);
    assert!(
        findings
            .iter()
            .any(|f| f.kind == Kind::Model && f.text.contains("proof budget")),
        "{findings:?}"
    );
    assert!(
        !findings.iter().any(|f| f.kind == Kind::Deadlock),
        "{findings:?}"
    );
    let result = ktpu_verify::gate(&[nodes], &target, ktpu_verify::Mode::Refuse, None);
    assert!(
        matches!(result, Err(ktpu_verify::GateError::Refused(ref fs))
        if fs.iter().any(|f| f.kind == Kind::Model && f.text.contains("proof budget"))),
        "{result:?}"
    );
}

#[test]
fn retained_index_data_uses_the_previous_epoch_publication() {
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    let mut first = Program::new();
    first.store(vec![(12288, 1)], false);
    let mut scatter = Program::new();
    scatter.move_(
        vec![
            (0x10, 24576 << 4 | 1 << 44),
            (0x18, 2 << 4 | 32 << 20),
            (0x20, 0),
            (0x10, 4096 << 4 | 1),
            (0x50, 4096 | 2 << 32),
            (0x30, 12288 | 1 << 40),
            (0, 1 << 16 | u64::from(target.isa.mover.modes.scatter)),
        ],
        vec![],
    );
    let mut context = ktpu_verify::MemoryContext::default();
    context.initial.insert(12288, 0u64.to_le_bytes().to_vec());
    for (address, collision) in [(8192, true), (16384, false)] {
        let mut peer = Program::new();
        peer.store(vec![(address, 2)], false);
        let runs = [
            BTreeMap::from([(0, package(&first, 0)), (1, package(&Program::new(), 1))]),
            BTreeMap::from([(0, package(&scatter, 0)), (1, package(&peer, 1))]),
        ];
        let findings = ktpu_verify::check_with_memory(&runs, &target, &context);
        if collision {
            hazard(&findings, Kind::Race);
        } else {
            clean(&findings);
        }
    }
}

#[test]
fn a_runtime_contract_guard_constrains_the_access_without_publishing_it() {
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    for acquired in [false, true] {
        let mut producer = Program::new();
        producer.store(vec![(4096, 0)], false).ring(1, 9, 1, true);
        let mut consumer = Program::new();
        if acquired {
            consumer.wait_slot(9, 1, true);
        }
        // Control status is unknown at preparation. The successful guard
        // constrains the subsequent address, but does not acquire a producer.
        consumer.load(0, 0x100, None, true);
        consumer
            .steps
            .push(ktpu_l0::program::Stmt::AssertZero { reg: 0, code: 1 });
        consumer.load(1, 4096, Some(0), false);
        let nodes = BTreeMap::from([(0, package(&producer, 0)), (1, package(&consumer, 1))]);
        let findings = ktpu_verify::check(&[nodes.clone(), nodes], &target);
        if acquired {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
            assert!(
                !findings.iter().any(|f| f.kind == Kind::Model),
                "{findings:?}"
            );
        }
    }
    let mut rejected = Program::new();
    rejected.alu(0, ktpu_l0::Alu::Mov, 0, 1, true);
    rejected
        .steps
        .push(ktpu_l0::program::Stmt::AssertZero { reg: 0, code: 1 });
    let findings = verify(&[rejected]);
    assert!(
        findings
            .iter()
            .any(|f| f.kind == Kind::Model && f.text.contains("contract 1 cannot succeed")),
        "{findings:?}"
    );
}

#[test]
fn a_burst_ack_cannot_replace_the_retirement_wait_that_releases_memory() {
    let machine = card();
    let target = Target::from_machine(&machine, Some(1)).expect("target");
    let mg = Facts::of_mesh(&machine, 0).expect("facts").units["MG"][0];
    let mut descriptor = Word::default();
    descriptor.set(224, 8, 3);
    descriptor.set(216, 8, 1);
    let mut p = Program::new();
    p.push(mg, descriptor, 8192, 4, true);
    p.send(
        mg,
        vec![ClusterInst::Fill(Fill {
            addr: 4096,
            sel: 1,
            n: 1,
            ..Fill::default()
        })
        .encode(&target.isa.cluster)
        .expect("memory FILL")],
        vec![],
    );
    p.wait(mg, None).expect("instruction retirement");
    p.store(vec![(4096, 0)], false);
    clean(&verify(&[p]));
}

#[test]
fn one_source_orders_memory_writes_without_joining_other_sources() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let target = Target::from_machine(&machine, Some(1)).expect("target");
    for vector in [false, true] {
        let mut p = Program::new();
        if vector {
            let mut words = image_dma(
                4096,
                "ainc a0 += 32\nvdrain a0 <- l1[0] rel\n\
                 vdrain a0 <- l1[0] rel\nvdrain a0 <- l1[0]\n\
                 vdrain a0 <- l1[0]\nhalt\n",
            );
            words[1] = VecCu::Desc {
                ad: 0,
                fld: 1,
                value: (32 << 16) | 1,
            }
            .encode(&target.isa.vector)
            .expect("one-word walk");
            p.send(facts.units["VC"][0], words, vec![]);
        } else {
            let words = [4096, 4128, 4096]
                .into_iter()
                .map(|addr| {
                    ClusterInst::Drain(Drain {
                        addr,
                        n: 1,
                        ..Drain::default()
                    })
                    .encode(&target.isa.cluster)
                    .expect("memory drain")
                })
                .collect();
            p.send(facts.units["MG"][0], words, vec![]);
        }
        clean(&verify(&[p.clone()]));
        // A second source still needs a completion edge, even at the same home.
        p.send(facts.units["VC"][1], dma(4096, true), vec![]);
        hazard(&verify(&[p]), Kind::Race);
    }
}

#[test]
fn prepared_staging_does_not_alias_a_tensor_at_the_same_offset() {
    let source = "\
fn placement.l1(x: mx7a[4, 32] @dram(gm=1, nk=1), scratch: f16[32] @stage(node=1))
    node 0
        send mg[0]
            fill A0 <- x n=1
    node 1
        send mg[0]
            drain scratch n=2
";
    super::common::verify_card_l1(
        source,
        "placement",
        &card(),
        BTreeMap::from([("x".into(), 4096)]),
        &ktpu_verify::MemoryContext::default(),
    );
}

#[test]
#[should_panic(expected = "generated staging extent")]
fn generated_staging_cannot_certify_an_out_of_aperture_access() {
    let machine = card();
    let topology = ktpu_machine::card::Card::of(&machine, None, None).expect("card topology");
    let bytes = topology.stage_bytes;
    // The f16 allocation is twice the SRAM capacity; the drain addresses its
    // second half. A unique aperture prefix alone cannot certify this access.
    let source = format!(
        "fn oversized.l1(scratch: f16[{bytes}] @stage(node=1))\n    node 1\n        send mg[0]\n            drain scratch+{bytes} n=2\n"
    );
    super::common::verify_card_l1(
        &source,
        "oversized",
        &machine,
        BTreeMap::new(),
        &ktpu_verify::MemoryContext::default(),
    );
}

#[test]
fn destination_windows_suppress_copy_and_gather_source_traffic() {
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    for mode in [ktpu_isa::MoveMode::Copy, ktpu_isa::MoveMode::Gather] {
        let mut destination = ktpu_isa::Walk::new(8192, &[(2, 64), (2, 32)]);
        destination.dims[0].axis = 1;
        destination.dims[0].astep = 1;
        destination.windows[0] = (-1, 1);
        let command = ktpu_isa::Move {
            mode,
            src: Some(ktpu_isa::Walk::new(4096, &[(4, 32)])),
            dst: Some(destination),
            ewidth: ktpu_isa::unit::mover::W32,
            flags: ktpu_isa::unit::mover::FLAG_WCOAL,
            xform: (0, 0),
            imm: None,
            seed: None,
            index: (mode == ktpu_isa::MoveMode::Gather).then_some((12288, 2)),
            row: (mode == ktpu_isa::MoveMode::Gather).then_some((64, 2)),
            fp: false,
        };
        let writes = target
            .isa
            .mover
            .move_writes(&command)
            .expect("masked move")
            .into_iter()
            .map(|(reg, value)| (u64::from(reg), value))
            .collect();
        let mut reader = Program::new();
        let token = reader.post(writes, vec![]);
        reader.wait_moves(token).expect("move completion");
        let mut context = ktpu_verify::MemoryContext::default();
        // ISA.MV.029/.033: only the enabled gather row needs a source address.
        // The index-cache read still includes both lanes (ISA.MV.050).
        context.initial.insert(12292, 1u32.to_le_bytes().to_vec());
        for (address, enabled) in [(4096, false), (4160, true)] {
            let mut writer = Program::new();
            writer.store(vec![(address, 7)], false);
            let findings = verify_with_memory(&[reader.clone(), writer], &context);
            if enabled {
                hazard(&findings, Kind::Race);
            } else {
                clean(&findings);
            }
        }
    }
}

#[test]
fn index_cache_reads_conflict_with_writes_to_unused_lanes() {
    let target = Target::from_machine(&card(), Some(2)).expect("target");
    for operation in [
        ktpu_isa::MoverOp::Gather {
            src_base: 4096,
            index: (12288, 1),
            dst: ktpu_isa::Walk::new(8192, &[(2, 32)]),
            row: (64, 2),
            fp: false,
        },
        ktpu_isa::MoverOp::Scatter {
            src: ktpu_isa::Walk::new(4096, &[(2, 32)]),
            dst_base: 8192,
            index: (12288, 1),
            row: (64, 2),
            fp: false,
        },
    ] {
        let writes = target
            .isa
            .mover
            .writes(&operation)
            .expect("indexed move")
            .into_iter()
            .map(|(reg, value)| (u64::from(reg), value))
            .collect();
        let mut reader = Program::new();
        let token = reader.post(writes, vec![]);
        reader.wait_moves(token).expect("move completion");
        let mut context = ktpu_verify::MemoryContext::default();
        context.initial.insert(12288, 0u32.to_le_bytes().to_vec());
        for (address, fetched) in [(12304, true), (12320, false)] {
            let mut writer = Program::new();
            writer.store(vec![(address, 7)], false);
            let findings = verify_with_memory(&[reader.clone(), writer], &context);
            if fetched {
                hazard(&findings, Kind::Race);
            } else {
                clean(&findings);
            }
        }
    }
}

#[test]
fn posted_write_history_certifies_reuse_without_spending_a_quadratic_budget() {
    let machine = card();
    let vc = Facts::of_mesh(&machine, 0).expect("facts").units["VC"][0];
    let mut producer = Program::new();
    producer.send(
        vc,
        image_dma(
            4096,
            "seti s5 = 2048\nloop s5\n    vdrain a0 <- l1[0]\nhalt\n",
        ),
        vec![],
    );
    producer.wait(vc, None).expect("endpoint publication");
    producer.ring(1, 9, 1, true);
    let mut consumer = Program::new();
    consumer.wait_slot(9, 1, true).load(0, 4096, None, false);
    clean(&verify(&[producer, consumer]));
}

#[test]
fn engine_waits_publish_mover_writes_without_implicit_dispatch_acquires() {
    let machine = card();
    let facts = Facts::of(&machine);
    let target = Target::from_machine(&machine, Some(1)).expect("target");
    for fetch in [None, facts.fetch_port] {
        for acquire in [false, true] {
            let mut p = Program::new();
            let token = p.post(
                vec![
                    (0x10, (4096 << 4) | (1 << 44) | 1),
                    (0x18, (2 << 4) | (32 << 20) | 1),
                    (0x20, 0),
                    (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
                ],
                vec![],
            );
            if acquire {
                p.wait_moves(token).expect("publication");
            }
            p.send(facts.units["VC"][0], dma(4096, false), vec![]);
            let raw = p
                .build(
                    &facts,
                    BuildOpts {
                        fetch,
                        ..BuildOpts::default()
                    },
                )
                .expect("program")
                .build(false, false);
            let engine = ktpu_l0::engine::lower(&raw, &facts).expect("engine lowering");
            let nodes = BTreeMap::from([(0, engine)]);
            let findings = ktpu_verify::check_with_memory(
                &[nodes.clone(), nodes],
                &target,
                &ktpu_verify::MemoryContext::default(),
            );
            if acquire {
                clean(&findings);
            } else {
                hazard(&findings, Kind::Race);
            }
        }
    }
}

#[test]
fn engine_wait_targets_wrap_without_losing_the_publication_frontier() {
    let machine = card()
        .with_fact("node.engine.counter_bits", serde_json::json!(4))
        .expect("short counter machine");
    let mut facts = Facts::of(&machine);
    facts.wait_window = false;
    let target = Target::from_machine(&machine, Some(1)).expect("target");
    for acquire_each in [false, true] {
        let mut p = Program::new();
        let mut first = None;
        for _ in 0..18 {
            let token = p.post(
                vec![
                    (0x10, (4096 << 4) | (1 << 44) | 1),
                    (0x18, (2 << 4) | (32 << 20) | 1),
                    (0x20, 0),
                    (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
                ],
                vec![],
            );
            first.get_or_insert(token);
            if acquire_each {
                p.wait_moves(token)
                    .expect("publication before counter wrap");
            }
        }
        if !acquire_each {
            p.wait_moves(first.expect("first move"))
                .expect("early modular target");
        }
        p.load(0, 4096, None, false);
        let raw = p
            .build(&facts, BuildOpts::default())
            .expect("program")
            .build(false, false);
        let engine = ktpu_l0::engine::lower(&raw, &facts).expect("engine lowering");
        let nodes = BTreeMap::from([(0, engine)]);
        let findings = ktpu_verify::check_with_memory(
            &[nodes.clone(), nodes],
            &target,
            &ktpu_verify::MemoryContext::default(),
        );
        if acquire_each {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
        }
    }
}

#[test]
fn local_mover_rebinding_keeps_publication_and_does_not_touch_the_old_allocation() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 1).expect("facts");
    let original = Facts::of(&machine);
    let address = machine.die.address.runtime();
    let target = Target::from_machine(&machine, Some(2)).expect("target");
    let addresses = ktpu_l0::IsaAddresses::new(&machine).expect("address fields");
    for (dynamic, relocated) in [(false, true), (true, true), (false, false)] {
        let mut p = Program::new();
        let base = if relocated { 4096 } else { 8192 };
        if relocated {
            p.spans.insert(base, 128);
        }
        p.alu(0, ktpu_l0::Alu::Mov, 0, 32, true);
        let patches = if dynamic {
            vec![ktpu_l0::Patch {
                word: 0,
                segments: vec![(4, address.mesh_shift, 0)],
                reg: 0,
                konst: base,
            }]
        } else {
            vec![]
        };
        let offset = if dynamic { 0 } else { 32 };
        let token = p.post(
            vec![
                (0x10, ((base + offset) << 4) | (1 << 44) | 1),
                (0x18, (2 << 4) | (32 << 20) | 1),
                (0x20, 0),
                (0, (1 << 16) | u64::from(target.isa.mover.modes.fill)),
            ],
            patches,
        );
        if let Some(ktpu_l0::Stmt::Post { local, .. }) = p.steps.last_mut() {
            *local = Some(ktpu_l0::build::LocalRoute {
                shift: address.mesh_shift,
                bits: address.mesh_bits,
                special: address.special_bit,
                mesh: 0,
            });
        }
        p.wait_moves(token).expect("publication");
        p.send(facts.units["VC"][0], dma(8192 + 32, false), vec![]);
        let built = p
            .build(
                &original,
                BuildOpts {
                    addresses: Some(&addresses),
                    spans: Some(&p.spans),
                    ..BuildOpts::default()
                },
            )
            .expect("relocatable program");
        let raw = built
            .build(false, false)
            .to_bytes(&original)
            .expect("wire package");
        let moved = Package::rehomed(&raw, &facts).expect("rehome local routing");
        let raw = Package::from_bytes(&moved, &facts).expect("rehomed package");
        let bound = raw
            .bind(&vec![
                8192 | (2u64 << address.mesh_shift);
                raw.buffers.len()
            ])
            .expect("routed binding");
        let mut other = Program::new();
        other.store(vec![(4096 + 32, 0)], false);
        let nodes = BTreeMap::from([(0, package(&other, 0)), (1, bound)]);
        clean(&ktpu_verify::check_with_memory(
            &[nodes.clone(), nodes],
            &target,
            &ktpu_verify::MemoryContext::default(),
        ));
    }
}

#[test]
fn engine_wait_does_not_publish_to_a_cpu_patch_without_a_drain() {
    let machine = card();
    let facts = Facts::of(&machine);
    let target = Target::from_machine(&machine, Some(1)).expect("target");
    let mut program = Program::new();
    program.send(facts.units["VC"][0], dma(16384, false), vec![]);
    let raw = package(&program, 0);
    let engine = ktpu_l0::engine::lower(&raw, &facts).expect("engine lowering");
    for drain in [false, true] {
        let mut p = engine.clone();
        if drain {
            p.steps
                .push(ktpu_l0::Step::new(ktpu_l0::Op::Barrier, 0, 0, 0, 0));
        }
        let field = ktpu_l0::format::patch_arg(0, 64, 0, false, false).expect("patch field");
        p.steps
            .push(ktpu_l0::Step::new(ktpu_l0::Op::Patch, 0, 0, field, 0));
        let mut memory = ktpu_verify::MemoryContext::default();
        memory.packages.insert(
            (0, 0),
            ktpu_verify::PackageStorage {
                steps: 32768,
                payloads: 16384,
            },
        );
        let findings =
            ktpu_verify::check_with_memory(&[BTreeMap::from([(0, p)])], &target, &memory);
        if drain {
            clean(&findings);
        } else {
            hazard(&findings, Kind::Race);
        }
    }
}

#[test]
fn local_register_spills_publish_after_rebinding_and_rehoming() {
    let machine = card();
    let address = machine.die.address.runtime();
    let original = Facts::of_mesh(&machine, 0).expect("original facts");
    let moved = Facts::of_mesh(&machine, 3).expect("destination facts");
    let target = Target::from_machine(&machine, None).expect("target");
    let route = ktpu_l0::build::LocalRoute {
        shift: address.mesh_shift,
        bits: address.mesh_bits,
        special: address.special_bit,
        mesh: 0,
    };
    for relocated in [false, true] {
        let mut p = Program::new();
        if relocated {
            p.spans.insert(4096, 64);
        }
        p.alu(0, ktpu_l0::Alu::Mov, 0, 32, true);
        p.store(vec![(4096, 0)], true);
        p.load(0, 4096, None, false);
        for step in &mut p.steps {
            if let ktpu_l0::Stmt::Store { local, .. } | ktpu_l0::Stmt::Load { local, .. } = step {
                *local = Some(route);
            }
        }
        // The restored register determines the next access's physical footprint.
        p.load(1, 16384, Some(0), false);
        let raw = p
            .build(
                &original,
                BuildOpts {
                    spans: Some(&p.spans),
                    ..BuildOpts::default()
                },
            )
            .expect("spill package")
            .build(false, false);
        let bytes = raw.to_bytes(&original).expect("wire package");
        let bytes = Package::rehomed(&bytes, &moved).expect("local spill rehome");
        let raw = Package::from_bytes(&bytes, &moved).expect("rehomed package");
        let bindings = vec![8192 | (1u64 << address.mesh_shift); raw.buffers.len()];
        let bound = raw.bind(&bindings).expect("spill binding");
        let mut other = Program::new();
        if relocated {
            other.store(vec![(4096, 0)], false);
        }
        let nodes = BTreeMap::from([(0, package(&other, 0)), (3, bound)]);
        clean(&ktpu_verify::check_with_memory(
            &[nodes.clone(), nodes],
            &target,
            &ktpu_verify::MemoryContext::default(),
        ));
    }
}

#[test]
fn independent_unit_drains_share_a_line_but_not_written_bytes() {
    let machine = card();
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let a = facts.units["VC"][0];
    let b = facts.units["VC"][1];
    for (second, overlapping) in [(4192, false), (4160, true)] {
        let mut p = Program::new();
        p.send(a, dma(4128, true), vec![]);
        p.send(b, dma(second, true), vec![]);
        p.wait(a, None).expect("first drain completion");
        p.wait(b, None).expect("second drain completion");
        let findings = verify(&[p]);
        if overlapping {
            hazard(&findings, Kind::Race);
        } else {
            clean(&findings);
        }
    }
}
