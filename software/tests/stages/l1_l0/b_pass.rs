//! Packed operand-B transport across sysnodes, including an entry split at
//! the interlink packet boundary (ISA.VC.134, ISA.MG.016, ISA.MG.040).

use std::collections::BTreeMap;
use std::rc::Rc;

use ktpu_isa::{Isa, UnitKind};
use ktpu_l0::{Facts, Package};
use ktpu_l1::build::{programs, Builder, Opts};
use ktpu_l1::text::read;
use ktpu_sim::l0::{card, Units};
use ktpu_sim::machine::Machine;
use ktpu_sim::num::mx7::Entry;
use ktpu_sim::units::link::Place;
use ktpu_sim::Dram;

use super::common;

fn packages(source: &str, machine: &ktpu_machine::Machine) -> BTreeMap<u32, Package> {
    let module = read(source, "B delivery").expect("L1 motif");
    let binds = BTreeMap::from([
        ("a".into(), 4096),
        ("b".into(), 8192),
        ("out".into(), 12288),
    ]);
    let programs =
        programs(&module, "bpass", &binds, machine, Some(2), None).expect("bound node programs");
    let builder = Builder::new(machine).expect("builder");
    programs
        .iter()
        .map(|p| {
            (
                p.mesh,
                builder
                    .build(p, Opts::default())
                    .expect("pre-engine package")
                    .build(false, false),
            )
        })
        .collect()
}

fn source(machine: &ktpu_machine::Machine, peer: bool) -> String {
    let mg = machine.unit(UnitKind::Mg, 0).expect("MG");
    let port = machine.die.fetch_port;
    let sender = if peer {
        "    node 0\n        wait slot 9 n=1 consume\n        send vc[0]\n            run push(b)\n        wait vc[0]\n"
    } else {
        ""
    };
    let delivery = if peer {
        "        ring mesh=0 slot=9\n        send mg[0]\n            fill B0 recv n=8\n"
    } else {
        "        send mg[0]\n            fill B0 <- b n=8\n"
    };
    format!(
        "target ktpu.v9
image push(b)
    desc a0 = b walk=(32, 32)
    desc a1 = peer(0, count=1, mesh=1, fin=({}, {})) walk=(1, 31)
    desc a2 = peer(31, count=1, mesh=1, fin=({}, {})) walk=(1, 1)
    vfill a0 -> l1[256]
    vsync drain on=fill
    vdrain a1 <- l1[256] to=({}, {}) buf=1
    vdrain a2 <- l1[287] to=({}, {}) buf=1
    halt
fn bpass.l1(a: mx7a[4, 32] @dram(gm=1, nk=1), b: mx7b[32, 32] @dram(gn=8, nk=1), out: f16[4, 32] @dram(tile=1x8))
{sender}    node 1
        send mg[0]
            fill A0 <- a n=1
        wait mg[0]
{delivery}        send mg[0]
            gemm 1x8x1 a=A0 b=B0 acc=0
            drain out n=8
        wait mg[0]
",
        mg.x(),
        mg.y(),
        mg.x(),
        mg.y(),
        port.x(),
        port.y(),
        port.x(),
        port.y(),
    )
}

#[test]
fn packed_b_peer_bytes_and_gemm_follow_private_a_and_operand_count() {
    let machine = common::card();
    let coord = |kind| {
        machine
            .units(kind)
            .iter()
            .map(|c| (c.x() as u64, c.y() as u64))
            .collect()
    };
    let facts = Facts::of_mesh(&machine, 0).expect("facts");
    let units = Units {
        vc: coord(UnitKind::Vc),
        mg: coord(UnitKind::Mg),
        src_mover: facts.src_mover(),
        counter_bits: facts.counter_bits,
    };
    let target = ktpu_verify::Target::from_machine(&machine, Some(2)).expect("target");
    let mut input = Dram::new(16384);
    for (side, count, base) in [(false, 1, 4096), (true, 8, 8192)] {
        for entry in 0..count {
            let e = Entry {
                q: std::array::from_fn(|lane| {
                    std::array::from_fn(|k| ((entry * 11 + lane * 7 + k * 3) % 125) as i8 - 62)
                }),
                field: std::array::from_fn(|lane| 0x90 + (entry * 3 + lane) as u8),
            };
            for (word, bytes) in e.write(side).iter().enumerate() {
                input
                    .set_word((base + entry * 128 + word * 32) as i64, bytes)
                    .expect("packed source");
            }
        }
    }
    let mut outputs = Vec::new();
    for peer in [false, true] {
        let text = source(&machine, peer);
        let packages = packages(&text, &machine);
        let findings = ktpu_verify::check(std::slice::from_ref(&packages), &target);
        assert!(
            !findings.iter().any(|f| f.kind.refuses()),
            "{text}\n{findings:?}"
        );
        let mut mem = Dram::new(input.bytes.len());
        mem.bytes.copy_from_slice(&input.bytes);
        let mut sim = Machine::of_meshes(
            mem,
            2,
            units.vc.len(),
            units.mg.len(),
            machine.vc.regs as usize,
            7,
        );
        sim.isa = Some(Rc::new(Isa::reference(&machine).expect("ISA")));
        card::run(&packages, &units, &mut sim).expect("encoded two-node execution");
        let resident: Vec<_> = sim.mg[units.mg.len()]
            .cluster
            .operand_words(true, 0, 0, 8)
            .into_iter()
            .flatten()
            .collect();
        assert_eq!(resident, input.bytes[8192..8192 + 1024]);
        outputs.push(sim.mem.bytes[12288..12288 + 256].to_vec());
    }
    assert_eq!(outputs[0], outputs[1], "same packed operands and K order");

    let text = source(&machine, true);
    let mut mem = Dram::new(input.bytes.len());
    mem.bytes.copy_from_slice(&input.bytes);
    let mut sim = Machine::of_meshes(
        mem,
        2,
        units.vc.len(),
        units.mg.len(),
        machine.vc.regs as usize,
        7,
    );
    for (kind, vc) in [(UnitKind::Vc, true), (UnitKind::Mg, false)] {
        for (index, coord) in machine.units(kind).iter().enumerate() {
            sim.places.insert(
                (coord.x(), coord.y()),
                if vc {
                    Place::Vc(index)
                } else {
                    Place::Mg(index)
                },
            );
        }
    }
    let port = machine.die.fetch_port;
    let mg = machine.unit(UnitKind::Mg, 0).expect("MG");
    let interpreted = text
        .replace(
            "image push(b)",
            "image push(b, dx, dy)\n    desc a3 = peer(0, count=1) walk=(1, 32)",
        )
        .replace(
            "run push(b)",
            &format!("run push(b, {}, {})", port.x(), port.y()),
        )
        .replace(
            &format!("to=({}, {})", port.x(), port.y()),
            "to=(dx, dy)",
        )
        .replace(
            "    halt\n",
            &format!(
                "    vdrain a3 <- l1[256] to=({}, {}) buf=1\n    halt\n",
                mg.x(),
                mg.y(),
            ),
        )
        .replace(
            "    node 0\n",
            "    node 0\n        send mg[0]\n            fill A0 <- a n=1\n        wait mg[0]\n",
        )
        .replace(
            "        wait vc[0]\n",
            "        wait vc[0]\n        send mg[0]\n            fill B0 recv n=8\n            gemm 1x8x1 a=A0 b=B0 acc=0\n            drain 12800 n=8\n        wait mg[0]\n",
        );
    let module = read(&interpreted, "B delivery coordinates").expect("L1 motif");
    let binds = ktpu_sim::l1::Binds::from([
        ("a".into(), 4096),
        ("b".into(), 8192),
        ("out".into(), 12288),
    ]);
    ktpu_sim::l1::run(&module, "bpass", &binds, &mut sim)
        .expect("coordinate-addressed L1 execution");
    for (index, output) in [(0, 12800), (units.mg.len(), 12288)] {
        let resident: Vec<_> = sim.mg[index]
            .cluster
            .operand_words(true, 0, 0, 8)
            .into_iter()
            .flatten()
            .collect();
        assert_eq!(resident, input.bytes[8192..8192 + 1024]);
        assert_eq!(sim.mem.bytes[output..output + 256], outputs[0]);
    }

    let late_a = text
        .replace("            fill A0 <- a n=1\n", "")
        .replace(
            "            fill B0 recv n=8",
            "            fill A0 <- a n=1\n            fill B0 recv n=8",
        )
        .replace("        send mg[0]\n        wait mg[0]\n", "");
    for (mutant, kind) in [
        (late_a, ktpu_verify::Kind::Port),
        (text.replace("buf=1", "buf=0"), ktpu_verify::Kind::Deadlock),
    ] {
        let findings = ktpu_verify::check(&[packages(&mutant, &machine)], &target);
        assert!(
            findings.iter().any(|f| f.kind == kind && f.kind.refuses()),
            "{mutant}\n{findings:?}"
        );
    }
}
