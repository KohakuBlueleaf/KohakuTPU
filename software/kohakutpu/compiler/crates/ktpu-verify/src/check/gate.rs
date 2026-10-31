//! The gate before a run (L1.VER.005): refuse, warn or off, with findings
//! cached by a key that names no absolute address.

use std::fs;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{check, Nodes};
use ktpu_l0::Package;

use crate::model::finding::{Finding, Kind};
use crate::target::Target;

/// A digest of this crate's source (build.rs): what the checker is.
pub const SOURCE_DIGEST: &str = env!("KTPU_VERIFY_SOURCE");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Refuse a run with a finding of a refusing kind.
    Refuse,
    /// Return every finding.
    Warn,
    /// Check nothing.
    Off,
}

impl FromStr for Mode {
    type Err = GateError;

    fn from_str(s: &str) -> Result<Mode, GateError> {
        match s {
            "refuse" => Ok(Mode::Refuse),
            "warn" => Ok(Mode::Warn),
            "off" => Ok(Mode::Off),
            _ => Err(GateError::Mode(s.to_string())),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum GateError {
    #[error("L1.VER.005: check mode {0:?}: one of refuse, warn, off")]
    Mode(String),
    /// The checker refuses this run: findings of a refusing kind.
    #[error("L1.VER.005: the static checker refuses this run ({} findings)", .0.iter().filter(|f| f.kind.refuses()).count())]
    Refused(Vec<Finding>),
}

/// `check`'s findings for a run about to start. `Refuse` errs when any
/// finding is of a refusing kind, `Warn` returns them all, `Off` checks
/// nothing. With `cache` (a directory and the run's `key`), the findings
/// are kept in `<dir>/<key>.json` and read back for the same key.
pub fn gate(
    runs: &[Nodes],
    t: &Target,
    mode: Mode,
    cache: Option<(&Path, &str)>,
) -> Result<Vec<Finding>, GateError> {
    if mode == Mode::Off {
        return Ok(Vec::new());
    }
    let path = cache.map(|(dir, key)| dir.join(format!("{key}.json")));
    let found = match path.as_deref().and_then(load) {
        Some(found) => found,
        None => {
            let found = check(runs, t);
            if let Some(p) = &path {
                store(p, &found);
            }
            found
        }
    };
    if mode == Mode::Refuse && found.iter().any(|f| f.kind.refuses()) {
        return Err(GateError::Refused(found));
    }
    Ok(found)
}

/// A cache entry's findings; None when there is none or it does not parse.
fn load(path: &Path) -> Option<Vec<Finding>> {
    let text = fs::read_to_string(path).ok()?;
    let rows: Value = serde_json::from_str(&text).ok()?;
    rows.as_array()?
        .iter()
        .map(|r| {
            let kind = Kind::parse(r.get(0)?.as_str()?)?;
            Some(Finding::new(kind, r.get(1)?.as_str()?, r.get(2)?.as_str()?))
        })
        .collect()
}

/// A cache entry written whole or not at all: the bytes reach the disk in a
/// sibling file before it is renamed over `path`. An entry another process
/// already stored is kept (its rows are the key's).
fn store(path: &Path, found: &[Finding]) {
    let rows: Vec<Value> = found
        .iter()
        .map(|f| Value::from(vec![f.kind.name(), &f.at, &f.text]))
        .collect();
    let text = Value::from(rows).to_string();
    if let Some(dir) = path.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let tmp: PathBuf = path.with_extension(format!("json.{}.tmp", std::process::id()));
    let written = fs::File::create(&tmp).and_then(|mut f| {
        f.write_all(text.as_bytes())?;
        f.sync_all()
    });
    if written.is_err() || fs::rename(&tmp, path).is_err() {
        let _ = fs::remove_file(&tmp);
    }
}

/// `((i, j, offset), ...)`: each pair of a package's buffers whose bound
/// extents meet or touch, and buffer j's offset from buffer i.
pub fn overlaps(pkg: &Package, bindings: &[u64]) -> Vec<(usize, usize, i128)> {
    let spans: Vec<(i128, i128)> = bindings
        .iter()
        .zip(&pkg.buffers)
        .map(|(&at, b)| (at as i128, b.size as i128))
        .collect();
    let mut out = Vec::new();
    for (i, &(bi, si)) in spans.iter().enumerate() {
        for (j, &(bj, sj)) in spans.iter().enumerate() {
            if i < j && bi <= bj + sj && bj <= bi + si {
                out.push((i, j, bj - bi));
            }
        }
    }
    out
}

/// A digest of what `check` decides from: the checker's source, the
/// target, and each node's first package as built against empty vector
/// cores (relocatable: no absolute address) with its bindings' overlaps.
pub fn key(first: &std::collections::BTreeMap<u32, (Package, Vec<u64>)>, t: &Target) -> String {
    let mut h = Sha256::new();
    h.update(SOURCE_DIGEST.as_bytes());
    h.update(t.digest_text().as_bytes());
    for (mesh, (pkg, bindings)) in first {
        h.update(mesh.to_string().as_bytes());
        h.update(format!("{pkg:?}").as_bytes());
        h.update(format!("{:?}", overlaps(pkg, bindings)).as_bytes());
        // Partial ownership depends on line phase and rounded-line neighbours.
        let line = t.facts.line_bytes.max(1) as u64;
        let phases: Vec<_> = bindings.iter().map(|at| at % line).collect();
        h.update(format!("{phases:?}").as_bytes());
        for (i, (&a, ab)) in bindings.iter().zip(&pkg.buffers).enumerate() {
            for (j, (&b, bb)) in bindings.iter().zip(&pkg.buffers).enumerate().skip(i + 1) {
                if a / line <= b.saturating_add(bb.size.saturating_sub(1)) / line
                    && b / line <= a.saturating_add(ab.size.saturating_sub(1)) / line
                {
                    h.update(format!("line:{i}:{j}:{}", b as i128 - a as i128).as_bytes());
                }
            }
        }
    }
    let d = h.finalize();
    d.iter().take(16).map(|b| format!("{b:02x}")).collect()
}
