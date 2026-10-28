//! The compiler pipeline and the kohakutpu_rs Python module.
//!
//! ```text
//! graph (tracer JSON) -> L2 -> passes -> L1 -> schedule -> L0 -> verify
//! ```
//!
//! [`pipeline::compile`] runs one kernel through a [`Backend`] (the stage
//! crates are [`Stack`]); [`pipeline::compile_many`] runs many over rayon,
//! results in input order. Each stage is cached on disk under
//! [`cache_root`]`/<stage>/`.

#[cfg(feature = "stack")]
pub mod json;
#[cfg(feature = "stack")]
pub mod mutant;
pub mod output;
pub mod pipeline;
#[cfg(feature = "python")]
mod python;
#[cfg(feature = "stack")]
pub mod spec;
#[cfg(feature = "stack")]
pub mod stack;
#[cfg(feature = "stack")]
pub mod templates;
#[cfg(feature = "wasm")]
mod wasm;

pub use output::{Compiled, Finding, Input, PackageOut, Refused, Scratch, Verdict};
pub use pipeline::cache::Cache;
pub use pipeline::stage::{Backend, Stage};
pub use pipeline::{compile, compile_each, compile_many};
#[cfg(feature = "stack")]
pub use stack::{CardStack, Stack};

use std::path::{Path, PathBuf};

/// The mutant under which the cache variables are not read.
pub const RCACHE_IGNORED: &str = "ktpu.cache/rcache_ignored";

/// The variables naming the cache root (DSL.CACHE.004), the first set
/// wins: `KTPU_COMPILE_CACHE`, and `KTPU_RCACHE` its alias.
pub const CACHE_ENV: [&str; 2] = ["KTPU_COMPILE_CACHE", "KTPU_RCACHE"];

/// Where the stage caches live: a [`CACHE_ENV`] variable (empty: no
/// cache), else the repository's `build/rcache`; none on wasm32 (no file
/// system).
pub fn cache_root() -> Option<PathBuf> {
    if cfg!(target_arch = "wasm32") {
        return None;
    }
    let named = match mutant::active() == Some(RCACHE_IGNORED) {
        true => None,
        false => CACHE_ENV.iter().find_map(std::env::var_os),
    };
    match named {
        Some(dir) if dir.is_empty() => None,
        Some(dir) => Some(PathBuf::from(dir)),
        None => Some(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../build/rcache")),
    }
}

/// The cache at [`cache_root`].
pub fn default_cache() -> Cache {
    cache_root().map_or_else(Cache::off, Cache::new)
}
