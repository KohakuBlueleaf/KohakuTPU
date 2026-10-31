//! Each cached stage's version: a digest of the sources of the stage crates
//! it runs, their `ktpu-*` dependencies (transitively), and this crate's
//! `src/stack` (the glue). Shared by `build.rs` and `tests/stage_version.rs`.

use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

/// `(stage, the crates it runs)`; the env name is `KTPU_STAGE_<stage>`.
pub const STAGES: &[(&str, &[&str])] = &[
    ("GRAPH", &["ktpu-graph"]),
    ("PASSES", &["ktpu-l2"]),
    ("LOWER", &["ktpu-l2"]),
    ("SCHEDULE", &["ktpu-l1"]),
    ("ORDER", &["ktpu-l1", "ktpu-verify"]),
    ("VERIFY", &["ktpu-l1", "ktpu-verify"]),
];

/// A file's bytes, None when it cannot be read.
pub type Read<'a> = &'a dyn Fn(&Path) -> Option<Vec<u8>>;

/// The `ktpu-*` crates `[dependencies]` of `krate`'s Cargo.toml names.
fn deps(crates: &Path, krate: &str, read: Read) -> Vec<String> {
    let toml = read(&crates.join(krate).join("Cargo.toml")).unwrap_or_default();
    let mut on = false;
    let mut out = Vec::new();
    for line in String::from_utf8_lossy(&toml).lines() {
        let line = line.trim();
        if line.starts_with('[') {
            on = line == "[dependencies]";
        } else if on && line.starts_with("ktpu-") {
            let name: String = line
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '-')
                .collect();
            out.push(name);
        }
    }
    out
}

/// `roots` and every `ktpu-*` crate they depend on.
pub fn closure(crates: &Path, roots: &[&str], read: Read) -> BTreeSet<String> {
    let mut seen = BTreeSet::new();
    let mut todo: Vec<String> = roots.iter().map(|s| s.to_string()).collect();
    while let Some(k) = todo.pop() {
        if seen.insert(k.clone()) {
            todo.extend(deps(crates, &k, read));
        }
    }
    seen
}

fn files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return;
    };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            files(&p, out);
        } else {
            out.push(p);
        }
    }
}

/// The source files a stage's version covers, relative to `crates`.
pub fn sources(crates: &Path, roots: &[&str], read: Read) -> Vec<PathBuf> {
    let mut out = Vec::new();
    for k in closure(crates, roots, read) {
        out.push(PathBuf::from(&k).join("Cargo.toml"));
        let mut fs = Vec::new();
        files(&crates.join(&k).join("src"), &mut fs);
        out.extend(
            fs.iter()
                .filter_map(|p| p.strip_prefix(crates).ok().map(Path::to_path_buf)),
        );
    }
    let mut glue = Vec::new();
    files(&crates.join("ktpu").join("src").join("stack"), &mut glue);
    out.extend(
        glue.iter()
            .filter_map(|p| p.strip_prefix(crates).ok().map(Path::to_path_buf)),
    );
    out.sort();
    out
}

/// The stage version: sha256 over each source's path and bytes, as hex.
pub fn version(crates: &Path, roots: &[&str], read: Read) -> String {
    let mut h = Sha256::new();
    for p in sources(crates, roots, read) {
        h.update(p.to_string_lossy().replace('\\', "/").as_bytes());
        h.update([0]);
        h.update(read(&crates.join(&p)).unwrap_or_default());
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}
