//! The plan goldens (`software/rust/golden/plan`, `plan_dump.py`): every
//! case reads into the planner's types and back to the same JSON, so no
//! field the Python planner recorded is lost.

use std::path::{Path, PathBuf};

use ktpu_plan::Case;
use serde_json::Value;

fn goldens() -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/plan");
    let mut out: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("{}: {e}", dir.display()))
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    out.sort();
    out
}

#[test]
fn every_plan_golden_reads_back_whole() {
    let files = goldens();
    assert!(files.len() >= 60, "{} goldens", files.len());
    for f in &files {
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap();
        let case: Case =
            serde_json::from_value(doc.clone()).unwrap_or_else(|e| panic!("{}: {e}", f.display()));
        assert_eq!(
            serde_json::to_value(&case).unwrap(),
            doc,
            "{}: a field does not read back",
            f.display()
        );
    }
}
