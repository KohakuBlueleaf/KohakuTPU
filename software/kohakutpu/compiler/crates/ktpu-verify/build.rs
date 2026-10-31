//! KTPU_VERIFY_SOURCE: a digest of every source file, so a gate cache entry
//! is keyed by the checker that wrote it.

use std::fs;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

fn files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = fs::read_dir(dir) else {
        return;
    };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            files(&p, out);
        } else if p.extension().is_some_and(|x| x == "rs") {
            out.push(p);
        }
    }
}

fn main() {
    let src = Path::new("src");
    println!("cargo:rerun-if-changed=src");
    let mut all = Vec::new();
    files(src, &mut all);
    all.sort();
    let mut h = Sha256::new();
    for p in &all {
        h.update(p.to_string_lossy().replace('\\', "/").as_bytes());
        h.update(fs::read(p).unwrap_or_default());
    }
    let d = h.finalize();
    let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
    println!("cargo:rustc-env=KTPU_VERIFY_SOURCE={hex}");
}
