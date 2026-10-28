//! KTPU_STAGE_<STAGE>: each cached stage's version (`build/digest.rs`).

#[path = "build/digest.rs"]
mod digest;

use std::path::Path;

fn main() {
    let crates = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    let read = |p: &Path| std::fs::read(p).ok();
    let mut watched = std::collections::BTreeSet::new();
    for (stage, roots) in digest::STAGES {
        let v = digest::version(&crates, roots, &read);
        println!("cargo:rustc-env=KTPU_STAGE_{stage}={v}");
        watched.extend(digest::closure(&crates, roots, &read));
    }
    for k in watched {
        println!("cargo:rerun-if-changed=../{k}/src");
        println!("cargo:rerun-if-changed=../{k}/Cargo.toml");
    }
    println!("cargo:rerun-if-changed=src/stack");
    println!("cargo:rerun-if-changed=build/digest.rs");
}
