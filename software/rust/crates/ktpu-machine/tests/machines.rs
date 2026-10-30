//! Every machine file loads into the typed schema; paths, sources and the
//! digest agree with the file and the frozen canonical machine witnesses.

use ktpu_machine::{
    machines_dir, names, schema_version, schema_version_of, Machine, MachineError, UnitKind,
    SCHEMA_SOURCES,
};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::PathBuf;

fn all() -> Vec<Machine> {
    let dir = machines_dir();
    let names = names(&dir);
    for want in ["multimesh_v9t6", "multimesh_v9t8", "sru_v9t7"] {
        assert!(
            names.iter().any(|n| n == want),
            "{want} in {dir:?}: {names:?}"
        );
    }
    assert!(!names.iter().any(|n| n == "measured"));
    names.iter().map(|n| Machine::named(n).unwrap()).collect()
}

fn golden() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden/machine/digests.json")
}

#[test]
fn every_machine_file_loads() {
    for m in all() {
        assert_eq!(m.die.units.mg.len() as u32, m.mg.count, "{}", m.name);
        assert_eq!(m.die.units.vc.len() as u32, m.vc.count, "{}", m.name);
        assert_eq!(m.die.units.sr.len() as u32, m.sr.count, "{}", m.name);
        assert_eq!(m.die.word_bytes * 8, m.die.noc.data_bits, "{}", m.name);
        assert_eq!(m.vc.chunks * m.vc.lanes, m.vc.vlmax, "{}", m.name);
        assert_eq!(m.sr_facts().is_some(), m.sr.count > 0, "{}", m.name);
    }
}

#[test]
fn dram_capacity_is_one_machine_fact() {
    for m in all() {
        assert_eq!(
            m.get("card.dram.bytes"),
            Some(&Value::from(m.card.dram.bytes))
        );
        assert!(m.get("card.dram.words").is_none());
        assert!(m.source("card.dram.bytes").is_some());
        assert!(m.card.dram.bytes > 0);
    }
    let model = Machine::named("vlt_v9t6s_1n").unwrap();
    assert_eq!(model.card.dram.bytes, 256 << 20);
    let smaller = model
        .with_fact("card.dram.bytes", Value::from(64 << 20))
        .unwrap();
    assert_eq!(smaller.card.dram.bytes, 64 << 20);
}

#[test]
fn write_line_is_a_sourced_fact_distinct_from_the_transfer_word() {
    for m in all() {
        assert_eq!(m.card.dram.line_bytes, 64);
        assert_eq!(m.die.word_bytes, 32);
        assert_eq!(m.get("card.dram.line_bytes"), Some(&Value::from(64)));
        let src = m.source("card.dram.line_bytes").unwrap();
        assert!(src.contains("xilinx-fpga/xcvu13p/bd/kx_pbd_4x4.v:"));
        assert!(src.contains("35_xache.tcl:"));
        let wider = m
            .with_fact("card.dram.line_bytes", Value::from(128))
            .unwrap();
        assert_eq!(wider.card.dram.line_bytes, 128);
        assert_ne!(wider.digest(), m.digest());
    }
}

#[test]
fn get_walks_the_raw_file_and_agrees_with_the_typed_facts() {
    for m in all() {
        assert_eq!(m.get("vc.lanes"), Some(&Value::from(m.vc.lanes)));
        assert_eq!(
            m.get("mover.one_word_runs"),
            Some(&Value::from(m.mover.one_word_runs))
        );
        assert_eq!(
            m.get("die.noc.addr_bits").and_then(Value::as_u64),
            Some(m.die.noc.addr_bits as u64)
        );
        assert!(m.get("vc.nothing").is_none());
        assert!(m.source("vc.lanes").is_some());
    }
}

#[test]
fn unit_kinds_and_coordinates() {
    let m = Machine::named("sru_v9t7").unwrap();
    assert_eq!(m.kinds(), vec![UnitKind::Mg, UnitKind::Sr]);
    assert_eq!(
        m.unit(UnitKind::Sr, 1).map(|c| (c.x(), c.y())),
        Some((2, 2))
    );
    assert_eq!(m.sr_facts().unwrap().profile.f_lanes, 16);
    let v = Machine::named("multimesh_v9t8").unwrap();
    assert!(v.sr_facts().is_none());
    assert_eq!(v.card.host.ctrl_base, 0x800000);
    assert_eq!(UnitKind::parse("VC"), Some(UnitKind::Vc));
    assert!(v.fw.has_part("kohakuaccel/dispatch"));
    assert_eq!(v.mover.modes.transform, 5);
}

#[test]
fn an_unknown_fact_is_refused() {
    let path = machines_dir().join("multimesh_v9t8.json");
    let text = std::fs::read_to_string(path).unwrap();
    let mut doc: Value = serde_json::from_str(&text).unwrap();
    doc["machine"]["vc"]["new_fact"] = Value::from(1);
    assert!(Machine::from_json(&doc.to_string()).is_err());
    doc["machine"]["vc"]
        .as_object_mut()
        .unwrap()
        .remove("new_fact");
    doc["machine"]["vc"]
        .as_object_mut()
        .unwrap()
        .remove("lanes");
    assert!(Machine::from_json(&doc.to_string()).is_err());
    doc["schema"] = Value::from(2);
    assert!(Machine::from_json(&doc.to_string()).is_err());
}

#[test]
fn the_schema_sources_are_the_schema_directory() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/schema");
    let mut files: Vec<String> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .filter(|n| n.ends_with(".rs"))
        .collect();
    files.sort();
    let listed: Vec<&str> = SCHEMA_SOURCES.iter().map(|(n, _)| *n).collect();
    assert_eq!(files, listed);
    for (n, text) in SCHEMA_SOURCES {
        assert_eq!(std::fs::read_to_string(dir.join(n)).unwrap(), text, "{n}");
    }
}

/// The schema sources with `edit` applied to `file`'s text.
fn edited(file: &str, edit: impl Fn(&str) -> String) -> Vec<String> {
    SCHEMA_SOURCES
        .iter()
        .map(|(n, t)| if *n == file { edit(t) } else { t.to_string() })
        .collect()
}

fn version(texts: &[String]) -> String {
    schema_version_of(&texts.iter().map(String::as_str).collect::<Vec<_>>())
}

#[test]
fn a_new_field_changes_the_schema_version_and_a_comment_does_not() {
    let base = version(&edited("system.rs", str::to_string));
    assert_eq!(base, schema_version());
    let field = edited("system.rs", |t| {
        t.replacen(
            "pub gt4_rows: u32,",
            "pub gt4_rows: u32,\n    pub gt4_extra: u32,",
            1,
        )
    });
    assert_ne!(version(&field), base);
    let typed = edited("system.rs", |t| {
        t.replacen("pub gt4_rows: u32,", "pub gt4_rows: u64,", 1)
    });
    assert_ne!(version(&typed), base);
    let comment = edited("system.rs", |t| {
        t.replacen(
            "pub gt4_rows: u32,",
            "// a note\n    pub gt4_rows:   u32, // rows",
            1,
        )
    });
    assert_eq!(version(&comment), base);
}

#[test]
fn a_file_of_another_schema_version_is_refused() {
    let path = machines_dir().join("multimesh_v9t8.json");
    let mut doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    assert_eq!(doc["schema"], Value::from(schema_version()));
    let older = version(&edited("system.rs", |t| {
        t.replacen("pub gt4_granule_bits: u32,", "", 1)
    }));
    assert_ne!(older, schema_version());
    for found in [Value::from(older), Value::from(1), Value::Null] {
        doc["schema"] = found.clone();
        let err = Machine::from_json(&doc.to_string()).unwrap_err();
        assert!(
            matches!(&err, MachineError::Schema { found: f } if *f == found),
            "{err}"
        );
    }
}

#[test]
fn digest_matches_frozen_machine_witnesses() {
    let text = std::fs::read_to_string(golden()).expect("golden/machine/digests.json");
    let want: BTreeMap<String, String> = serde_json::from_str(&text).unwrap();
    let got: BTreeMap<String, String> =
        all().iter().map(|m| (m.name.clone(), m.digest())).collect();
    assert_eq!(got, want);
}

#[test]
fn variants_override_one_fact() {
    let m = Machine::named("multimesh_v9t8").unwrap();
    let one = m.with_nodes(1).unwrap();
    assert_eq!((one.card.nodes, one.card.chain.clone()), (1, vec![0]));
    assert_ne!(one.digest(), m.digest());
    let safe = m
        .with_fact("mover.one_word_runs", Value::from(true))
        .unwrap();
    assert!(safe.mover.one_word_runs && !m.mover.one_word_runs);
    assert_eq!(safe.get("mover.one_word_runs"), Some(&Value::from(true)));
    assert!(m.with_fact("mover.nothing", Value::from(1)).is_err());
    assert!(m.with_fact("vc.lanes", Value::from("x")).is_err());
}

#[test]
fn digest_ignores_sources_and_follows_facts() {
    let path = machines_dir().join("multimesh_v9t8.json");
    let text = std::fs::read_to_string(path).unwrap();
    let base = Machine::from_json(&text).unwrap().digest();
    let mut doc: Value = serde_json::from_str(&text).unwrap();
    doc["sources"]["vc.lanes"] = Value::from("elsewhere");
    assert_eq!(Machine::from_json(&doc.to_string()).unwrap().digest(), base);
    doc["machine"]["vc"]["lanes"] = Value::from(8);
    assert_ne!(Machine::from_json(&doc.to_string()).unwrap().digest(), base);
}
