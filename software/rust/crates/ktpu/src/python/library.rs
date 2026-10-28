//! Machine-scoped, versioned exact-class artifacts. Reads never invoke a compiler.

use super::{compiled, machine};
use crate::{Backend, Compiled, Stage};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

const LIBRARY_KEPT_DEFAULT: usize = 512;

#[derive(Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
struct Key {
    schema: u32,
    class: String,
    machine: String,
    versions: BTreeMap<String, String>,
}

#[derive(Serialize, Deserialize)]
struct Entry {
    key: Key,
    names: Vec<String>,
    compiled: Compiled,
}

type LibraryCacheKey = (PathBuf, Arc<[u8]>, Arc<str>, Arc<str>, Key);

struct LibraryCache {
    limit: usize,
    entries: VecDeque<(LibraryCacheKey, Arc<Compiled>)>,
    reads: usize,
    hits: usize,
}

impl Default for LibraryCache {
    fn default() -> Self {
        Self {
            limit: LIBRARY_KEPT_DEFAULT,
            entries: VecDeque::new(),
            reads: 0,
            hits: 0,
        }
    }
}

fn lib_cache() -> &'static Mutex<LibraryCache> {
    static CACHE: OnceLock<Mutex<LibraryCache>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(LibraryCache::default()))
}

pub(super) fn cache_stats() -> (usize, usize, usize, usize) {
    let g = lib_cache().lock().unwrap_or_else(|e| e.into_inner());
    (g.entries.len(), g.limit, g.reads, g.hits)
}

pub(super) fn cache_configure(limit: Option<usize>, clear: bool) {
    let mut g = lib_cache().lock().unwrap_or_else(|e| e.into_inner());
    if clear {
        g.entries.clear();
        g.reads = 0;
        g.hits = 0;
    }
    if let Some(n) = limit {
        g.limit = n;
        while g.entries.len() > n {
            g.entries.pop_front();
        }
    }
}

fn digest(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

fn clean(v: &mut Value) {
    match v {
        Value::Object(o) => {
            if let Some(at) = o.get_mut("where") {
                *at = json!("");
            }
            for x in o.values_mut() {
                clean(x);
            }
        }
        Value::Array(a) => a.iter_mut().for_each(clean),
        _ => (),
    }
}

fn stage_versions() -> PyResult<BTreeMap<String, String>> {
    if crate::mutant::active().is_none() {
        static VERSIONS: OnceLock<BTreeMap<String, String>> = OnceLock::new();
        if let Some(v) = VERSIONS.get() {
            return Ok(v.clone());
        }
        let v: BTreeMap<String, String> = Stage::ALL
            .into_iter()
            .map(|s| {
                crate::stack::CardStack::default()
                    .version(s)
                    .map(|v| (s.name().to_string(), v))
                    .ok_or_else(|| PyValueError::new_err("unversioned library stage"))
            })
            .collect::<PyResult<_>>()?;
        let _ = VERSIONS.set(v.clone());
        return Ok(v);
    }
    Stage::ALL
        .into_iter()
        .map(|s| {
            crate::stack::CardStack::default()
                .version(s)
                .map(|v| (s.name().to_string(), v))
                .ok_or_else(|| PyValueError::new_err("unversioned library stage"))
        })
        .collect()
}

fn identity(source: &str, text: &str, need_canonical: bool) -> PyResult<(Key, String, String)> {
    machine(text)?;
    let mut graph: Value =
        serde_json::from_str(source).map_err(|e| PyValueError::new_err(e.to_string()))?;
    if graph["format"] != "ktpu.graph" {
        return Err(PyValueError::new_err("library requires a traced graph"));
    }
    let name = graph["name"]
        .as_str()
        .ok_or_else(|| PyValueError::new_err("graph has no name"))?
        .to_string();
    graph["name"] = json!("");
    clean(&mut graph);
    let class = digest(&serde_json::to_string(&graph).unwrap());
    let canonical = if need_canonical {
        graph["name"] = json!(format!("t_{class}"));
        serde_json::to_string(&graph).unwrap()
    } else {
        String::new()
    };
    let versions = stage_versions()?;
    Ok((
        Key {
            schema: 1,
            class,
            machine: digest(text),
            versions,
        },
        canonical,
        name,
    ))
}

fn path(root: &str, key: &Key) -> PathBuf {
    Path::new(root)
        .join(&key.machine)
        .join(format!("{}.json", key.class))
}

fn error(e: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(e.to_string())
}

#[pyfunction]
fn library_key(source: &str, machine_json: &str) -> PyResult<String> {
    let (key, _, _) = identity(source, machine_json, false)?;
    serde_json::to_string(&key).map_err(error)
}

#[pyfunction]
#[pyo3(signature = (root, source, machine_json, mutant=None))]
fn library_get<'py>(
    py: Python<'py>,
    root: &str,
    source: &str,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    if let Some(n) = mutant {
        crate::mutant::with(n, || ()).map_err(PyValueError::new_err)?;
    }
    let no_cache = mutant.is_some() || crate::mutant::active().is_some();
    let (key, _, _) = identity(source, machine_json, false)?;
    let dest = path(root, &key);
    let raw = match std::fs::read(&dest) {
        Ok(raw) => raw,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(error(e)),
    };
    let ckey = (
        dest.clone(),
        Arc::from(raw.as_slice()),
        Arc::from(source),
        Arc::from(machine_json),
        key.clone(),
    );
    if !no_cache {
        let mut g = lib_cache().lock().unwrap_or_else(|e| e.into_inner());
        if g.limit > 0 {
            if let Some(pos) = g.entries.iter().position(|(k, _)| k == &ckey) {
                let entry = g.entries.remove(pos).unwrap();
                let c = entry.1.clone();
                g.entries.push_back(entry);
                g.hits += 1;
                drop(g);
                return compiled(py, &c).map(Some);
            }
        }
    }
    let entry: Entry = serde_json::from_slice(&raw).map_err(error)?;
    let mut g = lib_cache().lock().unwrap_or_else(|e| e.into_inner());
    g.reads += 1;
    drop(g);
    if entry.key != key {
        return Ok(None);
    }
    if entry.compiled.bound.is_some()
        || entry.compiled.verdict.refused
        || entry.compiled.packages.is_empty()
    {
        return Err(error("library artifact is not verified and relocatable"));
    }
    let c = Arc::new(entry.compiled);
    if !no_cache {
        let mut g = lib_cache().lock().unwrap_or_else(|e| e.into_inner());
        if g.limit > 0 {
            g.entries.push_back((ckey, c.clone()));
            while g.entries.len() > g.limit {
                g.entries.pop_front();
            }
        }
    }
    compiled(py, &c).map(Some)
}

#[pyfunction]
fn library_prebuild<'py>(
    py: Python<'py>,
    root: &str,
    source: &str,
    machine_json: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let (key, canonical, name) = identity(source, machine_json, true)?;
    let m = machine(machine_json)?;
    let dest = path(root, &key);
    let mut entry = match std::fs::read(&dest) {
        Ok(raw) => serde_json::from_slice::<Entry>(&raw)
            .ok()
            .filter(|e| e.key == key),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => None,
        Err(e) => return Err(error(e)),
    };
    let hit = entry.is_some();
    let started = std::time::Instant::now();
    if entry.is_none() {
        let c = py
            .detach(|| {
                crate::compile(
                    &crate::stack::CardStack::default(),
                    &crate::Input::Graph(canonical),
                    None,
                    &m,
                    &crate::Cache::off(),
                )
            })
            .map_err(|e| super::raise(py, &e))?;
        if c.bound.is_some() || c.verdict.refused || c.packages.is_empty() {
            return Err(error(format!(
                "class {} is not verified and relocatable: {:?}; {:?}",
                key.class, c.bound, c.verdict
            )));
        }
        entry = Some(Entry {
            key: key.clone(),
            names: vec![],
            compiled: c,
        });
    }
    let mut entry = entry.unwrap();
    if !entry.names.contains(&name) {
        entry.names.push(name);
        entry.names.sort();
    }
    let directory = dest.parent().unwrap();
    std::fs::create_dir_all(directory).map_err(error)?;
    let temp = dest.with_extension(format!("{}.tmp", std::process::id()));
    std::fs::write(&temp, serde_json::to_vec(&entry).map_err(error)?).map_err(error)?;
    std::fs::rename(&temp, &dest).map_err(error)?;
    lib_cache()
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .entries
        .retain(|((p, _, _, _, _), _)| p != &dest);
    let index_path = directory.join("index.json");
    let mut index: BTreeMap<String, Value> = std::fs::read(&index_path)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or_default();
    let mut present = std::collections::BTreeSet::new();
    for file in std::fs::read_dir(directory).map_err(error)? {
        let p = file.map_err(error)?.path();
        if p.file_name().is_some_and(|n| n == "index.json")
            || p.extension().is_none_or(|e| e != "json")
        {
            continue;
        }
        if let Some(stem) = p.file_stem().and_then(|s| s.to_str()) {
            present.insert(stem.to_string());
            if stem != entry.key.class && !index.contains_key(stem) {
                let e: Entry =
                    serde_json::from_slice(&std::fs::read(&p).map_err(error)?).map_err(error)?;
                index.insert(e.key.class.clone(), json!({"key": e.key, "names": e.names}));
            }
        }
    }
    index.retain(|k, _| present.contains(k));
    index.insert(
        entry.key.class.clone(),
        json!({"key": entry.key, "names": entry.names}),
    );
    let temp = directory.join(format!("index.{}.tmp", std::process::id()));
    std::fs::write(&temp, serde_json::to_vec(&index).map_err(error)?).map_err(error)?;
    std::fs::rename(temp, index_path).map_err(error)?;
    let out = PyDict::new(py);
    out.set_item("key", serde_json::to_string(&key).unwrap())?;
    out.set_item("hit", hit)?;
    out.set_item("seconds", started.elapsed().as_secs_f64())?;
    out.set_item("bytes", std::fs::metadata(dest).map_err(error)?.len())?;
    Ok(out)
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(library_key, m)?)?;
    m.add_function(wrap_pyfunction!(library_get, m)?)?;
    m.add_function(wrap_pyfunction!(library_prebuild, m)?)?;
    Ok(())
}
