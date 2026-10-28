//! A build that cannot read a machine file says so, naming every such file
//! and its error and the rebuild ([`ktpu_machine::unreadable`],
//! [`ktpu_machine::stale_message`]); a readable file is not named.

#[cfg(feature = "mutants")]
use std::path::Path;
use std::path::PathBuf;

use ktpu_machine::{machines_dir, stale_message, unreadable};
use serde_json::Value;

/// A fresh directory holding the board's machine file as `good`, the same
/// with a field this build does not know as `newer`, and another schema
/// version as `next`.
fn fake_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("ktpu-machine-check-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let text = std::fs::read_to_string(machines_dir().join("multimesh_v9t6.json")).unwrap();
    std::fs::write(dir.join("good.json"), &text).unwrap();
    let mut doc: Value = serde_json::from_str(&text).unwrap();
    doc["machine"]["mg"]["zz_new_fact"] = 1.into();
    std::fs::write(dir.join("newer.json"), doc.to_string()).unwrap();
    let mut doc: Value = serde_json::from_str(&text).unwrap();
    doc["schema"] = "0000000000000000".into();
    std::fs::write(dir.join("next.json"), doc.to_string()).unwrap();
    dir
}

#[cfg(feature = "mutants")]
fn named(dir: &Path) -> Vec<String> {
    unreadable(dir).into_iter().map(|(n, _)| n).collect()
}

#[test]
fn every_unreadable_file_is_named_with_its_error() {
    let dir = fake_dir("named");
    let bad = unreadable(&dir);
    let names: Vec<&str> = bad.iter().map(|(n, _)| n.as_str()).collect();
    assert_eq!(names, ["newer", "next"]);
    let msg = stale_message(&dir, &bad);
    assert!(
        msg.contains("newer: ") && msg.contains("zz_new_fact"),
        "{msg}"
    );
    assert!(
        msg.contains("next: ") && msg.contains("schema \"0000000000000000\""),
        "{msg}"
    );
    assert!(msg.contains("scripts/py/build_rs.py"), "{msg}");
    assert!(!msg.contains("good"), "{msg}");
}

#[test]
fn the_repository_machine_files_are_all_readable() {
    let dir = machines_dir();
    let bad = unreadable(&dir);
    assert!(bad.is_empty(), "{}", stale_message(&dir, &bad));
}

#[cfg(feature = "mutants")]
#[test]
fn the_check_off_names_nothing() {
    let dir = fake_dir("off");
    assert_eq!(named(&dir), ["newer", "next"]);
    let off = ktpu_machine::mutant::with("machine.check/off", || named(&dir));
    assert!(
        off.is_empty(),
        "the mutant hides the stale files the witness names"
    );
}
