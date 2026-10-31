use ktpu_machine::{card::Card, machines_dir, names, Machine};
use serde_json::json;
use std::path::PathBuf;

fn fixture(suffix: &str) -> Machine {
    Machine::load(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(format!("multimesh_v9t6+{suffix}.json")),
    )
    .unwrap()
}

#[test]
fn every_machine_view_and_address() {
    for name in names(&machines_dir()) {
        let m = Machine::named(&name).unwrap();
        for nodes in [None, Some(1), Some(m.card.nodes)] {
            let mut missing = m.die.units.clone();
            if !missing.mg.is_empty() {
                missing.mg.pop();
            } else {
                missing.vc.pop();
            }
            for units in [None, Some(missing)] {
                let c = Card::of(&m, nodes, units.clone()).unwrap();
                assert_eq!(c.stage_bytes, 1 << 21);
                assert_eq!(c.board, name);
                assert_eq!(c.fetch, m.die.fetch_port);
                assert_eq!(c.inst_depth, m.unit.inst_depth);
                assert_eq!(c.stage_flits, *m.die.staging.flits.runtime());
                assert_eq!(c.regs, m.vc.regs);
                assert_eq!(c.rf_copies, m.vc.rf_copies);
                assert_eq!(c.sr, m.sr);
                assert_eq!(c.one_word_runs, m.mover.one_word_runs);
                assert_eq!(c.meshes.len(), nodes.unwrap_or(m.card.nodes) as usize);
                for mesh in &c.meshes {
                    assert_eq!(
                        mesh.units,
                        units.clone().unwrap_or_else(|| m.die.units.clone())
                    );
                    assert_eq!(mesh.mem_ports, m.die.sys_ports);
                    assert_eq!(mesh.agent, m.die.sys_ports[0]);
                    for offset in [0, 1, 1 << 20, (1 << 21) - 1] {
                        assert_eq!(
                            c.global_addr(offset, mesh.index).unwrap(),
                            offset | (u64::from(mesh.index) << 36)
                        );
                        assert_eq!(
                            c.stage_addr(offset, mesh.index).unwrap(),
                            offset | (u64::from(mesh.index) << 36) | (1 << 39)
                        );
                    }
                    assert!(c.stage_addr(1 << 21, mesh.index).is_err());
                    assert!(c.global_addr(1 << 36, mesh.index).is_err());
                }
                assert!(c.stage_addr(0, c.meshes.len() as u32).is_err());
                assert!(c.global_addr(0, c.meshes.len() as u32).is_err());
            }
        }
    }
}

#[test]
fn fixture_formulas() {
    let c = Card::of(&fixture("entry_words=2"), None, None).unwrap();
    assert_eq!(c.stage_bytes, 1 << 20);
    assert!(c.stage_addr(1 << 20, 0).is_err());
    let m = fixture("mesh_bits=1");
    assert!(Card::of(&m, None, None).is_err());
    assert_eq!(Card::of(&m, Some(2), None).unwrap().meshes.len(), 2);
    let c = Card::of(&fixture("mesh_shift=37,mesh_bits=1"), Some(2), None).unwrap();
    assert_eq!(c.global_addr(1, 1).unwrap(), (1 << 37) | 1);
    assert_eq!(c.stage_addr(1, 1).unwrap(), (1 << 39) | (1 << 37) | 1);
}

#[test]
fn overlapping_empty_and_out_of_range_fields_are_refused() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    for (path, value) in [
        ("die.address.special_bit", 40),
        ("die.address.reserved_bit", 39),
        ("die.address.mesh_bits", 3),
        ("die.address.mesh_bits", 0),
        ("die.address.aperture_bits", 5),
        ("die.address.aperture_bits", 0),
        ("die.address.mesh_shift", 64),
        ("die.address.aperture_shift", 20),
        ("die.staging.entry_words", 0),
        ("die.staging.aperture", 63),
    ] {
        assert!(
            Card::of(&m.with_fact(path, json!(value)).unwrap(), None, None).is_err(),
            "{path}={value}"
        );
    }
    assert!(Card::of(&m, Some(0), None).is_err());
}

#[cfg(feature = "mutants")]
#[test]
fn fixture_mutants_are_killed() {
    use ktpu_machine::mutant;
    assert_ne!(
        mutant::with("machine.card/stage-banks", || Card::of(
            &fixture("entry_words=2"),
            None,
            None
        )
        .unwrap()
        .stage_bytes),
        1 << 20
    );
    assert!(mutant::with("machine.card/mesh-bits", || Card::of(
        &fixture("mesh_bits=1"),
        None,
        None
    ))
    .is_ok());
    assert_ne!(
        mutant::with("machine.card/mesh-shift", || Card::of(
            &fixture("mesh_shift=37,mesh_bits=1"),
            Some(2),
            None
        )
        .unwrap()
        .stage_addr(1, 1)
        .unwrap()),
        (1 << 39) | (1 << 37) | 1
    );
}
