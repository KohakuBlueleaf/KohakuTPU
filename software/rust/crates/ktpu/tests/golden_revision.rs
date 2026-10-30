//! `golden/.revision` (written by `fetch.py`) equals `golden/REVISION`: a stale
//! golden copy fails here by name, not as unrelated golden mismatches.

use std::path::PathBuf;

fn golden() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../golden")
}

fn read(name: &str) -> String {
    let path = golden().join(name);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("{}: {e}; run software/rust/golden/fetch.py", path.display()))
        .trim()
        .to_string()
}

#[test]
fn the_golden_copy_is_the_revision_this_tree_needs() {
    let (want, have) = (read("REVISION"), read(".revision"));
    assert_eq!(
        have, want,
        "golden copy is dataset commit {have}, this tree needs {want}: run software/rust/golden/fetch.py"
    );
}
