//! DSL.CACHE.004 for the Rust stage caches: `KTPU_RCACHE` names the cache
//! root, empty turns the caches off, unset is the repository's
//! `build/rcache`. One test: the variable is process-wide.

use ktpu::cache_root;
use std::path::{Path, PathBuf};

fn roots() -> (Option<PathBuf>, Option<PathBuf>, Option<PathBuf>) {
    std::env::set_var("KTPU_RCACHE", "elsewhere");
    let named = cache_root();
    std::env::set_var("KTPU_RCACHE", "");
    let off = cache_root();
    std::env::remove_var("KTPU_RCACHE");
    (named, off, cache_root())
}

fn read_as_ruled() -> bool {
    let (named, off, unset) = roots();
    let build = unset
        .as_deref()
        .map(Path::to_path_buf)
        .is_some_and(|p| p.ends_with("build/rcache"));
    named.as_deref() == Some(Path::new("elsewhere")) && off.is_none() && build
}

#[test]
fn the_variable_names_the_root_and_empty_turns_it_off() {
    assert!(read_as_ruled(), "{:?}", roots());
    #[cfg(feature = "mutants")]
    assert!(
        !ktpu::mutant::with(ktpu::RCACHE_IGNORED, read_as_ruled).unwrap(),
        "the mutant ignoring the variable passes"
    );
}
