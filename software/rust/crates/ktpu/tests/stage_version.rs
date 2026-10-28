//! Each stage's cache version covers its crates' sources and their
//! dependencies' (`build/digest.rs`): an edit to a dependency changes the
//! version of every stage built on it, and of no other.

#![cfg(feature = "stack")]

#[path = "../build/digest.rs"]
mod digest;

use ktpu::{Backend, Stack, Stage};
use std::path::{Path, PathBuf};

fn crates() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("..")
}

fn on_disk(p: &Path) -> Option<Vec<u8>> {
    std::fs::read(p).ok()
}

/// Every stage's version with one byte appended to `file` (under crates/).
fn edited(file: &str) -> Vec<(&'static str, String)> {
    let target = crates().join(file);
    let read = move |p: &Path| {
        let mut b = on_disk(p)?;
        if p == target {
            b.push(b'\n');
        }
        Some(b)
    };
    digest::STAGES
        .iter()
        .map(|(s, roots)| (*s, digest::version(&crates(), roots, &read)))
        .collect()
}

fn changed(file: &str) -> Vec<&'static str> {
    let before = edited("no such file");
    edited(file)
        .into_iter()
        .zip(before)
        .filter(|(a, b)| a.1 != b.1)
        .map(|(a, _)| a.0)
        .collect()
}

/// The mutant: a stage's version over its own crates' sources, no
/// dependency, picked by hand.
fn own_crates_only(roots: &[&str], read: digest::Read) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    for k in roots {
        let mut files = Vec::new();
        let mut todo = vec![crates().join(k).join("src")];
        while let Some(d) = todo.pop() {
            for e in std::fs::read_dir(&d).into_iter().flatten().flatten() {
                match e.path().is_dir() {
                    true => todo.push(e.path()),
                    false => files.push(e.path()),
                }
            }
        }
        files.sort();
        for f in files {
            h.update(read(&f).unwrap_or_default());
        }
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn a_version_of_its_own_crates_only_is_caught() {
    let target = crates().join("ktpu-isa/src/lib.rs");
    let read = |edit: bool| {
        let target = target.clone();
        move |p: &Path| {
            let mut b = on_disk(p)?;
            if edit && p == target {
                b.push(b'\n');
            }
            Some(b)
        }
    };
    let (plain, edited) = (read(false), read(true));
    let roots = ["ktpu-l2"];
    assert_eq!(
        own_crates_only(&roots, &plain),
        own_crates_only(&roots, &edited),
        "the mutant misses an isa edit under the passes"
    );
    assert_ne!(
        digest::version(&crates(), &roots, &plain),
        digest::version(&crates(), &roots, &edited)
    );
}

#[test]
fn the_built_versions_are_the_sources_digests() {
    let now = edited("no such file");
    for (stage, (name, v)) in Stage::ALL.iter().zip(&now) {
        assert_eq!(Stack.version(*stage).as_deref(), Some(v.as_str()), "{name}");
    }
}

#[test]
fn an_isa_edit_changes_every_stage() {
    assert_eq!(
        changed("ktpu-isa/src/lib.rs"),
        ["GRAPH", "PASSES", "LOWER", "SCHEDULE", "ORDER", "VERIFY"]
    );
}

#[test]
fn a_verify_edit_changes_only_the_stages_that_run_the_checker() {
    assert_eq!(changed("ktpu-verify/src/lib.rs"), ["ORDER", "VERIFY"]);
}

#[test]
fn an_l2_edit_leaves_the_l1_stages() {
    let got = changed("ktpu-l2/src/lib.rs");
    assert!(got.contains(&"PASSES") && got.contains(&"LOWER"), "{got:?}");
    assert!(
        !got.contains(&"SCHEDULE") && !got.contains(&"ORDER"),
        "{got:?}"
    );
}

#[test]
fn a_glue_edit_changes_every_stage() {
    assert_eq!(changed("ktpu/src/stack/mod.rs").len(), digest::STAGES.len());
}
