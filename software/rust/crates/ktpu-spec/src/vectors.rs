//! Spec vectors: `software/tests/spec/vectors/<layer>/<area>/<CLAUSE>.<case>.json`,
//! the files the Python harness (`tests/spec/harness/vectors.py`) reads.

use serde::Deserialize;
use serde_json::Value;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Deserialize)]
pub struct Vector {
    /// `<layer>/<area>/<CLAUSE>.<case>`, set by [`load`].
    #[serde(skip)]
    pub id: String,
    pub clause: String,
    pub tier: u8,
    pub runner: String,
    #[serde(default)]
    pub machine: Option<String>,
    /// `frozen` (passes now) or `ruling` (passes once the stack follows it).
    pub status: String,
    pub input: Value,
    pub expect: Value,
    #[serde(default)]
    pub mutants: Vec<String>,
}

/// The repository's vectors directory.
pub fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/spec/vectors")
}

/// Every vector under `root`, sorted by id.
pub fn load(root: &Path) -> Result<Vec<Vector>, String> {
    let mut paths = Vec::new();
    walk(root, &mut paths).map_err(|e| format!("{}: {e}", root.display()))?;
    paths.sort();
    paths
        .into_iter()
        .map(|p| {
            let text = std::fs::read_to_string(&p).map_err(|e| format!("{}: {e}", p.display()))?;
            let mut v: Vector =
                serde_json::from_str(&text).map_err(|e| format!("{}: {e}", p.display()))?;
            let rel = p.strip_prefix(root).expect("under root");
            v.id = rel
                .with_extension("")
                .components()
                .map(|c| c.as_os_str().to_string_lossy())
                .collect::<Vec<_>>()
                .join("/");
            Ok(v)
        })
        .collect()
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let p = entry?.path();
        if p.is_dir() {
            walk(&p, out)?;
        } else if p.extension().is_some_and(|e| e == "json") {
            out.push(p);
        }
    }
    Ok(())
}
