//! The extension module `kohakutpu.compiler._rs` (feature `python`), built
//! by maturin from `software/compiler/pyproject.toml`; its stub is
//! `kohakutpu/compiler/_rs.pyi`.

use crate::stack::{CardStack, Stack};
use crate::{default_cache, Backend, Cache, Compiled, Input, Refused as StageRefused, Stage};
use ktpu_machine::Machine;
use ktpu_plan::program::Call;
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use std::collections::{HashMap, VecDeque};
use std::hash::Hash;
use std::sync::{Arc, Mutex, OnceLock};

mod bindings;
#[cfg(test)]
mod cache_tests;
mod library;

create_exception!(
    kohakutpu.compiler._rs,
    Refused,
    PyValueError,
    "A stage refused the kernel."
);

const MACHINES_KEPT_DEFAULT: usize = 64;
const TEMPLATES_KEPT_DEFAULT: usize = 1024;
const BOUND_KEPT_DEFAULT: usize = 4096;

#[derive(Clone, PartialEq, Eq, Hash)]
struct TemplateCacheKey {
    package: Arc<[u8]>,
    machine: Arc<str>,
    mesh: u32,
    template: Option<Arc<str>>,
    images: Option<Arc<str>>,
}

type CachedTemplate = (
    Arc<ktpu_l0::Package>,
    Arc<ktpu_l0::format::template::ParameterTable>,
    ktpu_l0::Facts,
);

#[derive(Clone, PartialEq, Eq, Hash)]
struct BoundCacheKey {
    template: TemplateCacheKey,
    translated: bool,
    addresses: Vec<u64>,
    lower: bool,
    after: Option<usize>,
    params: ktpu_l0::format::template::Params,
    count_path: u8,
}

struct LruMap<K, V> {
    limit: usize,
    map: HashMap<K, V>,
    order: VecDeque<K>,
}

impl<K: Clone + Eq + Hash, V: Clone> LruMap<K, V> {
    fn new(limit: usize) -> Self {
        Self {
            limit,
            map: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    fn len(&self) -> usize {
        self.map.len()
    }

    fn clear(&mut self) {
        self.map.clear();
        self.order.clear();
    }

    fn set_limit(&mut self, limit: usize) {
        self.limit = limit;
        while self.map.len() > self.limit {
            if let Some(old) = self.order.pop_front() {
                self.map.remove(&old);
            } else {
                break;
            }
        }
    }

    fn get(&mut self, key: &K) -> Option<V> {
        if self.limit == 0 {
            return None;
        }
        let val = self.map.get(key)?.clone();
        if self.order.back() != Some(key) {
            if let Some(pos) = self.order.iter().position(|k| k == key) {
                self.order.remove(pos);
            }
            self.order.push_back(key.clone());
        }
        Some(val)
    }

    fn insert(&mut self, key: K, val: V) {
        if self.limit == 0 {
            return;
        }
        if self.map.insert(key.clone(), val).is_some() {
            if let Some(pos) = self.order.iter().position(|k| k == &key) {
                self.order.remove(pos);
            }
        }
        self.order.push_back(key);
        while self.map.len() > self.limit {
            if let Some(old) = self.order.pop_front() {
                self.map.remove(&old);
            } else {
                break;
            }
        }
    }
}

struct BindCaches {
    machines: LruMap<String, Arc<Machine>>,
    templates: LruMap<TemplateCacheKey, CachedTemplate>,
    bound: LruMap<BoundCacheKey, Arc<Vec<u8>>>,
    machine_parses: usize,
    machine_hits: usize,
    template_parses: usize,
    template_hits: usize,
    bound_misses: usize,
    bound_hits: usize,
}

impl Default for BindCaches {
    fn default() -> Self {
        Self {
            machines: LruMap::new(MACHINES_KEPT_DEFAULT),
            templates: LruMap::new(TEMPLATES_KEPT_DEFAULT),
            bound: LruMap::new(BOUND_KEPT_DEFAULT),
            machine_parses: 0,
            machine_hits: 0,
            template_parses: 0,
            template_hits: 0,
            bound_misses: 0,
            bound_hits: 0,
        }
    }
}

fn bind_caches() -> &'static Mutex<BindCaches> {
    static CACHES: OnceLock<Mutex<BindCaches>> = OnceLock::new();
    CACHES.get_or_init(|| Mutex::new(BindCaches::default()))
}

fn machine(json: &str) -> PyResult<Arc<Machine>> {
    let bypass = crate::mutant::active().is_some();
    if !bypass {
        let mut guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
        if let Some(m) = guard.machines.get(&json.to_string()) {
            guard.machine_hits += 1;
            return Ok(m);
        }
    }
    let parsed =
        Arc::new(Machine::from_json(json).map_err(|e| PyValueError::new_err(e.to_string()))?);
    let mut guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
    guard.machine_parses += 1;
    if !bypass {
        guard.machines.insert(json.to_string(), parsed.clone());
    }
    Ok(parsed)
}

/// None: the default cache root; "": no cache; else that directory.
fn cache(dir: Option<&str>) -> Cache {
    match dir {
        None => default_cache(),
        Some("") => Cache::off(),
        Some(d) => Cache::new(d),
    }
}

pub(crate) fn template_table(
    text: &str,
) -> Result<ktpu_l0::format::template::ParameterTable, serde_json::Error> {
    let mut value: serde_json::Value = serde_json::from_str(text)?;
    if let Some(obj) = value.as_object_mut() {
        obj.remove("images");
    }
    serde_json::from_value(value)
}

fn compiled<'py>(py: Python<'py>, c: &Compiled) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("name", &c.name)?;
    d.set_item("l2", &c.l2)?;
    d.set_item("l1", &c.l1)?;
    let pkgs = PyDict::new(py);
    for (mesh, p) in &c.packages {
        let one = PyDict::new(py);
        one.set_item("bytes", PyBytes::new(py, &p.bytes))?;
        one.set_item("params", &p.params)?;
        one.set_item("images_json", p.images.to_string())?;
        pkgs.set_item(mesh, one)?;
    }
    d.set_item("packages", pkgs)?;
    let scratch = PyList::empty(py);
    for a in &c.scratch {
        let one = PyDict::new(py);
        one.set_item("space", &a.space)?;
        one.set_item("node", a.node)?;
        one.set_item("bytes", a.bytes)?;
        one.set_item("members", &a.members)?;
        scratch.append(one)?;
    }
    d.set_item("scratch", scratch)?;
    d.set_item("bound", &c.bound)?;
    let v = PyDict::new(py);
    v.set_item("refused", c.verdict.refused)?;
    let found = PyList::empty(py);
    for f in &c.verdict.findings {
        let one = PyDict::new(py);
        one.set_item("kind", &f.kind)?;
        one.set_item("clause", &f.clause)?;
        one.set_item("at", &f.at)?;
        one.set_item("text", &f.text)?;
        found.append(one)?;
    }
    v.set_item("findings", found)?;
    d.set_item("verdict", v)?;
    Ok(d)
}

fn refused<'py>(py: Python<'py>, r: &StageRefused) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("stage", r.stage.name())?;
    d.set_item("clause", &r.refusal.clause)?;
    d.set_item("message", &r.refusal.message)?;
    Ok(d)
}

fn raise(py: Python<'_>, r: &StageRefused) -> PyErr {
    let err = Refused::new_err(r.to_string());
    let value = err.value(py);
    let _ = value.setattr("stage", r.stage.name());
    let _ = value.setattr("clause", &r.refusal.clause);
    err
}

/// l2_of(graph_json, machine_json) -> str: the tracer's graph as L2 text
/// (the pipeline's graph stage, uncached).
#[pyfunction]
fn l2_of(py: Python<'_>, graph_json: &str, machine_json: &str) -> PyResult<String> {
    let m = machine(machine_json)?;
    let got = py.detach(|| {
        Stack
            .graph_to_l2(graph_json, &m)
            .map(|p| Stack.print_l2(&p))
    });
    got.map_err(|refusal| {
        raise(
            py,
            &StageRefused {
                stage: Stage::Graph,
                refusal,
            },
        )
    })
}

/// A JSON value as the Python value it reads as.
fn py_value<'py>(py: Python<'py>, v: &serde_json::Value) -> PyResult<Bound<'py, PyAny>> {
    use serde_json::Value as V;
    Ok(match v {
        V::Null => py.None().into_bound(py),
        V::Bool(b) => b.into_pyobject(py)?.to_owned().into_any(),
        V::Number(n) => match (n.as_i64(), n.as_u64()) {
            (Some(i), _) => i.into_pyobject(py)?.into_any(),
            (_, Some(u)) => u.into_pyobject(py)?.into_any(),
            _ => n.as_f64().unwrap_or(f64::NAN).into_pyobject(py)?.into_any(),
        },
        V::String(s) => s.into_pyobject(py)?.into_any(),
        V::Array(xs) => {
            let l = PyList::empty(py);
            for x in xs {
                l.append(py_value(py, x)?)?;
            }
            l.into_any()
        }
        V::Object(m) => {
            let d = PyDict::new(py);
            for (k, x) in m {
                d.set_item(k, py_value(py, x)?)?;
            }
            d.into_any()
        }
    })
}

/// cache_root() -> str | None: where `compile`'s stage caches live by
/// default (`KTPU_COMPILE_CACHE`, alias `KTPU_RCACHE`, else build/rcache;
/// None: off).
#[pyfunction]
fn cache_root() -> Option<String> {
    crate::cache_root().map(|p| p.to_string_lossy().into_owned())
}

/// debug_decode(region, machine_json) -> dict: a node's debug region (its
/// per-step trace and report) decoded (`ktpu_debug::decode_json`).
#[pyfunction]
fn debug_decode<'py>(
    py: Python<'py>,
    region: &[u8],
    machine_json: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let m = machine(machine_json)?;
    match ktpu_debug::decode_json(region, &m) {
        Ok(v) => py_value(py, &v),
        Err(e) => {
            let err = Refused::new_err(e.to_string());
            let _ = err.value(py).setattr("clause", &e.clause);
            let _ = err.value(py).setattr("stage", "debug");
            Err(err)
        }
    }
}

/// compile(source, machine_json, name=None, cache=None, fetch=False,
/// mutant=None, l1=False, binds=None, passes=True) -> dict
///
/// `source` is L2 text or the tracer's graph JSON; with `l1`, L1 text built
/// as written (no passes, no image scheduler; `name` required). `fetch`:
/// packages fetch through the die's fetch port, as a card runs them
/// (`CardStack`); with `binds` (parameter -> address) also built at those
/// addresses rather than relocatable. `mutant`: that spec-vector mutant on
/// in every stage crate for this compile, no stage cache (`MUTANTS` lists
/// what the build carries; a build without the `mutants` feature raises
/// `ValueError`). `passes=False`: the L2 lowered with no L2 -> L2 pass, a
/// card's packages (`fetch=True`). `exp2_ranges` ("off", "intervals",
/// "rowmax"; None the default): what the vector search's range analysis may
/// prove of EXP2 / EXP2D arguments (`ktpu_l2::Ranges`), a card's packages.
/// `zero_sign` ("native", "preserve"; None the default `ZERO_SIGN`): how the
/// lowering moves a value it does not change (`ktpu_l2::ZeroSign`).
#[pyfunction]
#[pyo3(signature = (source, machine_json, name=None, cache_dir=None, fetch=false, mutant=None, l1=false, binds=None, passes=true, exp2_ranges=None, zero_sign=None))]
#[allow(clippy::too_many_arguments)]
fn compile<'py>(
    py: Python<'py>,
    source: &str,
    machine_json: &str,
    name: Option<&str>,
    cache_dir: Option<&str>,
    fetch: bool,
    mutant: Option<&str>,
    l1: bool,
    binds: Option<std::collections::BTreeMap<String, i128>>,
    passes: bool,
    exp2_ranges: Option<&str>,
    zero_sign: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    if (binds.is_some() || !passes || exp2_ranges.is_some()) && !fetch {
        return Err(PyValueError::new_err(
            "binds=, passes=False and exp2_ranges= build a card's packages: fetch=True",
        ));
    }
    let ranges = exp2_ranges
        .map(|r| {
            ktpu_l2::Ranges::parse(r).ok_or_else(|| {
                PyValueError::new_err(format!("exp2_ranges is off, intervals or rowmax: {r}"))
            })
        })
        .transpose()?;
    let zero_sign = zero_sign.map(signed).transpose()?;
    let card = CardStack {
        binds,
        no_passes: !passes,
        ranges,
        zero_sign,
        node_fed: !fetch,
        ..CardStack::default()
    };
    let m = machine(machine_json)?;
    let input = match l1 {
        true => Input::L1(source.to_string()),
        false => Input::detect(source),
    };
    let c = match mutant {
        Some(_) => Cache::off(),
        None => cache(cache_dir),
    };
    let run = || match (fetch, zero_sign) {
        (false, None) => crate::compile(&Stack, &input, name, &m, &c),
        _ => crate::compile(&card, &input, name, &m, &c),
    };
    let got = py.detach(|| match mutant {
        Some(n) => crate::mutant::with(n, run),
        None => Ok(run()),
    });
    let got = got.map_err(PyValueError::new_err)?;
    match got {
        Ok(c) => compiled(py, &c),
        Err(r) => Err(raise(py, &r)),
    }
}

/// bind(package, bindings, machine_json, mesh=0, lower=False, mutant=None,
/// after=None) -> bytes
///
/// A relocatable package (`compile`'s), cut after step `after` (RUL.076,
/// `Package::cut`), bound at `bindings` (one address a buffer), and with
/// `lower` lowered for mesh `mesh`'s dispatch engine: the bytes a card runs.
/// Raises `Refused` (stage "bind") naming the L0 clause.
#[pyfunction]
#[pyo3(signature = (package, bindings, machine_json, mesh=0, lower=false, mutant=None, after=None, params=None, template_json=None, translations=None, count_path="step", cores=None, images_json=None))]
#[allow(clippy::too_many_arguments)]
fn bind<'py>(
    py: Python<'py>,
    package: &[u8],
    bindings: Vec<u64>,
    machine_json: &str,
    mesh: u32,
    lower: bool,
    mutant: Option<&str>,
    after: Option<usize>,
    params: Option<ktpu_l0::format::template::Params>,
    template_json: Option<&str>,
    translations: Option<Vec<ktpu_l0::format::bind::Translation>>,
    count_path: &str,
    cores: Option<PyRefMut<'_, bindings::cores::Cores>>,
    images_json: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    let params = params.unwrap_or_default();
    let (path, path_code) = match count_path {
        "step" => (ktpu_l0::format::template::CountPath::Step, 0u8),
        "register" => (ktpu_l0::format::template::CountPath::Register, 1u8),
        _ => return Err(PyValueError::new_err("count_path must be step or register")),
    };
    let no_cache = mutant.is_some() || crate::mutant::active().is_some();
    let m = if no_cache {
        let parsed = Arc::new(
            Machine::from_json(machine_json).map_err(|e| PyValueError::new_err(e.to_string()))?,
        );
        bind_caches()
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .machine_parses += 1;
        parsed
    } else {
        machine(machine_json)?
    };
    if let Some(c) = &cores {
        let cfg = ktpu_isa::unit::vector::VecCfg::of(&m)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        if c.inner.isa.cfg != cfg {
            return Err(PyValueError::new_err(
                "cores belong to another vector machine",
            ));
        }
    }
    let tkey = (!no_cache).then(|| TemplateCacheKey {
        package: Arc::from(package),
        machine: Arc::from(machine_json),
        mesh,
        template: template_json.map(Arc::from),
        images: images_json.map(Arc::from),
    });
    let cached_template = tkey.as_ref().and_then(|key| {
        let mut guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
        let val = guard.templates.get(key)?;
        guard.template_hits += 1;
        Some(val)
    });
    let (table_pre, images_pre) = if cached_template.is_none() {
        let table: ktpu_l0::format::template::ParameterTable = template_json
            .map(template_table)
            .transpose()
            .map_err(|e| PyValueError::new_err(format!("template metadata: {e}")))?
            .unwrap_or_default();
        let images: Option<serde_json::Value> =
            images_json
                .map(serde_json::from_str)
                .transpose()
                .map_err(|e| PyValueError::new_err(format!("image manifest: {e}")))?;
        let images = images.or_else(|| {
            template_json
                .and_then(|s| serde_json::from_str::<serde_json::Value>(s).ok())
                .and_then(|v| v.get("images").cloned())
        });
        (Some(Arc::new(table)), images)
    } else {
        (None, None)
    };
    let state = cores.as_ref().map(|c| c.inner.clone());
    let run = || {
        let mut state = state;
        let (pkg_arc, table_arc, f) = match cached_template {
            Some(got) => got,
            None => {
                let f = ktpu_l0::Facts::of_mesh(&m, mesh)?;
                let pkg = ktpu_l0::Package::from_bytes(package, &f)?;
                if let Some(images) = images_pre.as_ref().filter(|v| !v.is_null()) {
                    let isa = ktpu_isa::unit::vector::VecIsa::new(&m)
                        .map_err(|e| ktpu_l0::Refusal::new("IMR.MANIFEST", e.to_string()))?;
                    ktpu_l1::lower::manifest::validate(&pkg, &isa, images)
                        .map_err(|e| ktpu_l0::Refusal::new("IMR.MANIFEST", e.to_string()))?;
                }
                let got = (Arc::new(pkg), table_pre.expect("parsed above"), f);
                let mut guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
                guard.template_parses += 1;
                if let Some(key) = tkey.clone() {
                    guard.templates.insert(key, got.clone());
                }
                got
            }
        };
        let bkey = if !no_cache && state.is_none() {
            let eff_template = ktpu_l0::format::template::Template {
                package: (*pkg_arc).clone(),
                table: (*table_arc).clone(),
            };
            let buffers = eff_template.effective_buffers()?;
            let addresses = match &translations {
                Some(tr) => ktpu_l0::format::bind::translate(&buffers, &bindings, tr)?,
                None => bindings.clone(),
            };
            let key = BoundCacheKey {
                template: tkey.expect("set when !no_cache"),
                translated: translations.is_some(),
                addresses,
                lower,
                after,
                params: params.clone(),
                count_path: path_code,
            };
            let mut guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
            if let Some(bytes) = guard.bound.get(&key) {
                guard.bound_hits += 1;
                return Ok((bytes.as_ref().clone(), None));
            }
            Some(key)
        } else {
            None
        };
        let bind_fn = |t: &ktpu_l0::format::template::Template| match &translations {
            Some(tr) => t.bind_handles_with_path(&bindings, tr, &params, path),
            None => t.bind_with_path(&bindings, &params, path),
        };
        let mut template = ktpu_l0::format::template::Template {
            package: (*pkg_arc).clone(),
            table: (*table_arc).clone(),
        };
        if let Some(k) = after {
            // Validate the complete metadata before selecting original step indices.
            bind_fn(&template)?;
            template.package = template.package.cut(k)?;
            if !ktpu_l0::mutant::on("l0.template/cut_sites") {
                template
                    .table
                    .sites
                    .retain(|s| s.step.is_none_or(|i| i <= k));
            }
        }
        let pkg = bind_fn(&template)?;
        let pkg = if state.is_some() && pkg.steps.iter().any(|s| s.op == ktpu_l0::Op::Engine) {
            ktpu_l1::lower::engine::lift(&pkg, &f)
                .map_err(|e| ktpu_l0::Refusal::new("IMR.ENGINE", e.to_string()))?
        } else {
            pkg
        };
        let pkg = match state.as_mut() {
            Some(c) => ktpu_l1::lower::residency::package(&pkg, c)
                .map_err(|e| ktpu_l0::Refusal::new("IMR.REWRITE", e.to_string()))?,
            None => pkg,
        };
        let pkg = match lower {
            true => {
                let low = ktpu_l0::engine::lower(&pkg, &f)?;
                ktpu_l0::replay::check(&pkg, &low, &f)?;
                if state.is_some() {
                    ktpu_l1::lower::engine::compact(&low)
                        .map_err(|e| ktpu_l0::Refusal::new("IMR.ENGINE", e.to_string()))?
                } else {
                    low
                }
            }
            false => pkg,
        };
        let bytes = pkg.to_bytes(&f)?;
        let mut guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
        guard.bound_misses += 1;
        if let Some(key) = bkey {
            guard.bound.insert(key, Arc::new(bytes.clone()));
        }
        Ok::<_, ktpu_l0::Refusal>((bytes, state))
    };
    let got = match mutant {
        Some(n) => crate::mutant::with(n, run).map_err(PyValueError::new_err)?,
        None => run(),
    };
    match got {
        Ok((bytes, state)) => {
            if let (Some(mut c), Some(state)) = (cores, state) {
                c.inner = state;
            }
            Ok(PyBytes::new(py, &bytes))
        }
        Err(e) => {
            let err = Refused::new_err(e.to_string());
            let _ = err.value(py).setattr("clause", e.clause);
            let _ = err.value(py).setattr("stage", "bind");
            Err(err)
        }
    }
}

#[pyfunction]
fn bind_cache_stats(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
    let d = PyDict::new(py);
    d.set_item("machines_len", guard.machines.len())?;
    d.set_item("machines_limit", guard.machines.limit)?;
    d.set_item("machine_parses", guard.machine_parses)?;
    d.set_item("machine_hits", guard.machine_hits)?;
    d.set_item("templates_len", guard.templates.len())?;
    d.set_item("templates_limit", guard.templates.limit)?;
    d.set_item("template_parses", guard.template_parses)?;
    d.set_item("template_hits", guard.template_hits)?;
    d.set_item("bound_len", guard.bound.len())?;
    d.set_item("bound_limit", guard.bound.limit)?;
    d.set_item("bound_misses", guard.bound_misses)?;
    d.set_item("bound_hits", guard.bound_hits)?;
    let (lib_len, lib_limit, lib_reads, lib_hits) = library::cache_stats();
    d.set_item("library_len", lib_len)?;
    d.set_item("library_limit", lib_limit)?;
    d.set_item("library_reads", lib_reads)?;
    d.set_item("library_hits", lib_hits)?;
    Ok(d)
}

#[pyfunction]
#[pyo3(signature = (machines=None, templates=None, bound=None, library=None, clear=false))]
fn bind_cache_configure(
    machines: Option<usize>,
    templates: Option<usize>,
    bound: Option<usize>,
    library: Option<usize>,
    clear: bool,
) {
    let mut guard = bind_caches().lock().unwrap_or_else(|e| e.into_inner());
    if clear {
        guard.machines.clear();
        guard.templates.clear();
        guard.bound.clear();
        guard.machine_parses = 0;
        guard.machine_hits = 0;
        guard.template_parses = 0;
        guard.template_hits = 0;
        guard.bound_misses = 0;
        guard.bound_hits = 0;
    }
    if let Some(n) = machines {
        guard.machines.set_limit(n);
    }
    if let Some(n) = templates {
        guard.templates.set_limit(n);
    }
    if let Some(n) = bound {
        guard.bound.set_limit(n);
    }
    drop(guard);
    library::cache_configure(library, clear);
}

/// A `zero_sign` argument's value.
fn signed(name: &str) -> PyResult<ktpu_l2::ZeroSign> {
    ktpu_l2::ZeroSign::parse(name)
        .ok_or_else(|| PyValueError::new_err(format!("zero_sign is native or preserve: {name}")))
}

/// One machine for every source, or one a source.
#[derive(FromPyObject)]
enum Machines {
    One(String),
    Each(Vec<String>),
}

/// compile_many(sources, machine_json, cache=None, threads=None,
/// zero_sign=None) -> list
///
/// `machine_json`: one machine file's text, or a list of one a source. Item
/// i is source i's dict, or `{"refused": {...}}`. `zero_sign`: `compile`'s.
#[pyfunction]
#[pyo3(name = "compile_many", signature = (sources, machine_json, cache_dir=None, threads=None, zero_sign=None))]
fn compile_many<'py>(
    py: Python<'py>,
    sources: Vec<String>,
    machine_json: Machines,
    cache_dir: Option<&str>,
    threads: Option<usize>,
    zero_sign: Option<&str>,
) -> PyResult<Bound<'py, PyList>> {
    let zero_sign = zero_sign.map(signed).transpose()?;
    let card = CardStack {
        node_fed: true,
        zero_sign,
        ..CardStack::default()
    };
    let texts = match machine_json {
        Machines::One(t) => vec![t; sources.len().min(1)],
        Machines::Each(ts) if ts.len() == sources.len() => ts,
        Machines::Each(ts) => {
            return Err(PyValueError::new_err(format!(
                "{} machines for {} sources",
                ts.len(),
                sources.len()
            )))
        }
    };
    let ms: Vec<Arc<Machine>> = texts.iter().map(|t| machine(t)).collect::<PyResult<_>>()?;
    let inputs: Vec<(Input, Option<String>, &Machine)> = sources
        .iter()
        .enumerate()
        .map(|(i, s)| (Input::detect(s), None, &*ms[i.min(ms.len() - 1)]))
        .collect();
    let c = cache(cache_dir);
    let got = py.detach(|| {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads.unwrap_or(0))
            .build()
            .map_err(|e| e.to_string())?;
        Ok::<_, String>(pool.install(|| match zero_sign {
            None => crate::compile_each(&Stack, &inputs, &c),
            Some(_) => crate::compile_each(&card, &inputs, &c),
        }))
    });
    let got = got.map_err(PyValueError::new_err)?;
    let out = PyList::empty(py);
    for r in &got {
        match r {
            Ok(c) => out.append(compiled(py, c)?)?,
            Err(e) => {
                let d = PyDict::new(py);
                d.set_item("refused", refused(py, e)?)?;
                out.append(d)?;
            }
        }
    }
    Ok(out)
}

/// Compile box points with the representative's image and retile choices
/// frozen. `zero_sign`: `compile`'s.
#[pyfunction]
#[pyo3(signature = (representative, sources, machine_json, parameter_rotations=false, zero_sign=None))]
fn template_compile<'py>(
    py: Python<'py>,
    representative: &str,
    sources: Vec<String>,
    machine_json: &str,
    parameter_rotations: bool,
    zero_sign: Option<&str>,
) -> PyResult<Bound<'py, PyList>> {
    let m = machine(machine_json)?;
    let zero_sign = zero_sign
        .map(signed)
        .transpose()?
        .unwrap_or(ktpu_l2::ZeroSign::DEFAULT);
    let got = py
        .detach(|| {
            let compiler = crate::templates::compiler::PinnedCompiler::with_rotation_parameters(
                representative,
                &m,
                parameter_rotations,
                zero_sign,
            )?;
            sources
                .iter()
                .map(|s| compiler.compile(s))
                .collect::<Result<Vec<_>, StageRefused>>()
        })
        .map_err(|e| raise(py, &e))?;
    let out = PyList::empty(py);
    for c in got {
        out.append(compiled(py, &c)?)?;
    }
    Ok(out)
}

/// Generate a machine-specific kernel graph without a Python compiler frontend.
#[pyfunction]
#[pyo3(signature = (generator, args_json, machine_json, mutant=None))]
fn generate(
    generator: &str,
    args_json: &str,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<String> {
    let m = machine(machine_json)?;
    let args = serde_json::from_str(args_json)
        .map_err(|e| PyValueError::new_err(format!("generator arguments: {e}")))?;
    let run = || ktpu_kernels::call::call(generator, &args, &m);
    let kernel = match mutant {
        Some(n) => crate::mutant::with(n, run).map_err(PyValueError::new_err)?,
        None => run(),
    }
    .map_err(|e| PyValueError::new_err(e.to_string()))?;
    serde_json::to_string(&kernel.graph).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// relayout_plan(args_json, machine_json) -> str: `relayout.plan`'s plan of
/// the call's arguments as JSON `{"shape": repr(Plan.shape()), "index":
/// [..], "masks": [[..], ..]}`, or `{"unplanned": why}`; a refusal raises
/// `ValueError`.
#[pyfunction]
fn relayout_plan(args_json: &str, machine_json: &str) -> PyResult<String> {
    let m = machine(machine_json)?;
    let args = serde_json::from_str(args_json)
        .map_err(|e| PyValueError::new_err(format!("relayout.plan arguments: {e}")))?;
    let got = ktpu_kernels::call::relayout_plan(&args, &m)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(match got {
        Ok(pl) => serde_json::json!({
            "shape": ktpu_kernels::gen::relayout::shape_repr(&pl).repr(),
            "index": pl.index,
            "masks": pl.masks,
        }),
        Err(u) => serde_json::json!({"unplanned": u.0}),
    }
    .to_string())
}

/// pairs_blocks(machine_json, pairing=None, mutant=None) -> bool: whether a
/// contraction on the machine pairs K blocks under `pairing` (`MX7_PAIRING`
/// when None): `ktpu_machine::Machine::pairs_blocks`, RUL.099.
#[pyfunction]
#[pyo3(signature = (machine_json, pairing=None, mutant=None))]
fn pairs_blocks(machine_json: &str, pairing: Option<&str>, mutant: Option<&str>) -> PyResult<bool> {
    let m = machine(machine_json)?;
    let p = match pairing {
        None => ktpu_machine::PAIRING,
        Some(name) => ktpu_machine::Pairing::parse(name).ok_or_else(|| {
            PyValueError::new_err(format!("a pairing (vector, unpaired): {name}"))
        })?,
    };
    match mutant {
        Some(n) => crate::mutant::with(n, || m.pairs_blocks(p)).map_err(PyValueError::new_err),
        None => Ok(m.pairs_blocks(p)),
    }
}

/// mm_knobs() -> str: the contraction knobs the environment's `KTPU_MM_*`
/// overrides resolve (`ktpu_kernels::gen::matmul::Knobs::resolve`), a JSON
/// object by knob name.
#[pyfunction]
fn mm_knobs() -> String {
    ktpu_kernels::gen::matmul::Knobs::resolve()
        .json()
        .to_string()
}

/// Every contraction knob as JSON: `[{name, env, group}]`, group `plan` or
/// `kernel` (the call recording it).
fn mm_knob_table() -> String {
    use ktpu_kernels::gen::matmul::{Group, KNOBS};
    let rows: Vec<serde_json::Value> = KNOBS
        .iter()
        .map(|k| {
            let group = match k.group {
                Group::Plan => "plan",
                Group::Kernel => "kernel",
            };
            serde_json::json!({"name": k.name, "env": k.env, "group": group})
        })
        .collect();
    serde_json::Value::Array(rows).to_string()
}

/// The vector knobs' defaults (`ktpu_kernels::gen::vector::defaults`), a
/// JSON object: Python's `vector.TRIPS` (`"hchunk"` or `"balanced"`).
fn vector_defaults() -> String {
    let trips = match ktpu_kernels::gen::vector::defaults().trips {
        ktpu_kernels::gen::vector::Trips::HChunk => "hchunk",
        ktpu_kernels::gen::vector::Trips::Balanced => "balanced",
    };
    serde_json::json!({ "trips": trips }).to_string()
}

/// mm_plan(args_json, machine_json, mutant=None) -> str: the tiling of a
/// `matmul.plan` call's arguments (`ktpu_kernels::call::mm_plan`), JSON of
/// `bt m n k mp kp tm tn nk shared quantum`; ValueError on a refusal.
#[pyfunction]
#[pyo3(signature = (args_json, machine_json, mutant=None))]
fn mm_plan(args_json: &str, machine_json: &str, mutant: Option<&str>) -> PyResult<String> {
    let m = machine(machine_json)?;
    let args = serde_json::from_str(args_json)
        .map_err(|e| PyValueError::new_err(format!("matmul.plan arguments: {e}")))?;
    let run = || ktpu_kernels::call::mm_plan(&args, &m);
    let p = match mutant {
        Some(n) => crate::mutant::with(n, run).map_err(PyValueError::new_err)?,
        None => run(),
    }
    .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(serde_json::json!({
        "bt": p.bt, "m": p.m, "n": p.n, "k": p.k, "mp": p.mp, "kp": p.kp,
        "tm": p.tm, "tn": p.tn, "nk": p.nk, "shared": p.shared, "quantum": p.quantum,
    })
    .to_string())
}

/// mm_resolved(args_json, machine_json) -> str: a `matmul.mm_kernel` call's
/// kernel knobs as its kernel takes them (`ktpu_kernels::call::mm_resolved`),
/// JSON `{knobs, plain}`; ValueError on a refusal.
#[pyfunction]
fn mm_resolved(args_json: &str, machine_json: &str) -> PyResult<String> {
    let m = machine(machine_json)?;
    let args = serde_json::from_str(args_json)
        .map_err(|e| PyValueError::new_err(format!("matmul.mm_kernel arguments: {e}")))?;
    ktpu_kernels::call::mm_resolved(&args, &m)
        .map(|v| v.to_string())
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// mm_takes_f16(args_json, machine_json) -> bool: whether a contraction of
/// `{k, nk}` takes A as f16 under the kernel knobs it names
/// (`ktpu_kernels::call::mm_takes_f16`); ValueError on a refusal.
#[pyfunction]
fn mm_takes_f16(args_json: &str, machine_json: &str) -> PyResult<bool> {
    let m = machine(machine_json)?;
    let args = serde_json::from_str(args_json)
        .map_err(|e| PyValueError::new_err(format!("matmul.takes_f16 arguments: {e}")))?;
    ktpu_kernels::call::mm_takes_f16(&args, &m).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// The rows reduction knobs' defaults (`ktpu_kernels::gen::reductions::
/// defaults`), a JSON object: Python's `rows.SINGLE_PACKAGE_AST`.
fn rows_defaults() -> String {
    let d = ktpu_kernels::gen::reductions::defaults();
    serde_json::json!({"single_package_ast": d.single_package_ast}).to_string()
}

/// The pending-operand knobs' defaults (`ktpu_kernels::gen::prepared`), a
/// JSON object: Python's `forms.PREPARED` / `forms.PREPARED_QUANT`.
fn prepared_defaults() -> String {
    let d = ktpu_kernels::gen::prepared::defaults();
    serde_json::json!({
        "prepared": d.prepared,
        "prepared_quant": d.prepared_quant,
        "composed": d.composed.name(),
    })
    .to_string()
}

/// l1_cycles(l1_text, name, machine_json) -> int: `ktpu_l2::cost` of a compiled kernel.
#[pyfunction]
fn l1_cycles(l1_text: &str, name: &str, machine_json: &str) -> PyResult<i64> {
    let m = machine(machine_json)?;
    let module =
        ktpu_l1::text::read(l1_text, "<l1>").map_err(|e| PyValueError::new_err(e.to_string()))?;
    ktpu_l2::cost::cycles(&module, name, &ktpu_l2::target::Target::of(&m))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// read_composed(mode, read_json, launches_json, machine_json) -> bool:
/// `ktpu_plan::run::reads::composed` of `{"gen", "args"}` calls.
#[pyfunction]
fn read_composed(
    mode: &str,
    read_json: &str,
    launches_json: &str,
    machine_json: &str,
) -> PyResult<bool> {
    let m = machine(machine_json)?;
    let bad = |e: serde_json::Error| PyValueError::new_err(format!("a read's calls: {e}"));
    let read = serde_json::from_str(read_json).map_err(bad)?;
    let launches: Vec<Call> = serde_json::from_str(launches_json).map_err(bad)?;
    let mode = ktpu_kernels::gen::prepared::Composed::named(mode)
        .ok_or_else(|| PyValueError::new_err(format!("a composed mode {mode:?}")))?;
    let root = cache(None);
    let cost = |c: &Call| built_cycles(c, &m, &root);
    Ok(ktpu_plan::run::reads::composed(
        mode, &read, &launches, &m, &cost,
    ))
}

/// The cycles of `call`'s kernel as a card builds it, through the stage
/// caches; None where it does not build.
fn built_cycles(call: &Call, m: &Machine, root: &Cache) -> Option<i64> {
    let kernel = ktpu_kernels::call::call(&call.gen, &call.args, m).ok()?;
    let source = Input::detect(&serde_json::to_string(&kernel.graph).ok()?);
    let name = kernel.graph.name.as_str();
    let built = crate::compile(&CardStack::default(), &source, Some(name), m, root).ok()?;
    let module = ktpu_l1::text::read(&built.l1, "<l1>").ok()?;
    ktpu_l2::cost::cycles(&module, name, &ktpu_l2::target::Target::of(m)).ok()
}

/// The relayout knobs' defaults (`ktpu_kernels::gen::relayout`), a JSON
/// object: Python's `relayout.PULL_TREE`.
fn relayout_defaults() -> String {
    serde_json::json!({"pull_tree": ktpu_kernels::gen::relayout::PULL_TREE}).to_string()
}

#[pymodule]
#[pyo3(name = "_rs")]
fn kohakutpu_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let dir = ktpu_machine::machines_dir();
    let bad = ktpu_machine::unreadable(&dir);
    if !bad.is_empty() {
        return Err(pyo3::exceptions::PyImportError::new_err(
            ktpu_machine::stale_message(&dir, &bad),
        ));
    }
    m.add("MACHINE_SCHEMA", ktpu_machine::schema_version())?;
    m.add_function(wrap_pyfunction!(l2_of, m)?)?;
    m.add_function(wrap_pyfunction!(cache_root, m)?)?;
    m.add_function(wrap_pyfunction!(debug_decode, m)?)?;
    m.add_function(wrap_pyfunction!(compile, m)?)?;
    m.add_function(wrap_pyfunction!(generate, m)?)?;
    m.add_function(wrap_pyfunction!(read_composed, m)?)?;
    m.add_function(wrap_pyfunction!(l1_cycles, m)?)?;
    m.add_function(wrap_pyfunction!(pairs_blocks, m)?)?;
    m.add_function(wrap_pyfunction!(relayout_plan, m)?)?;
    m.add("MX7_PAIRING", ktpu_machine::PAIRING.name())?;
    m.add_function(wrap_pyfunction!(mm_knobs, m)?)?;
    m.add_function(wrap_pyfunction!(mm_plan, m)?)?;
    m.add_function(wrap_pyfunction!(mm_resolved, m)?)?;
    m.add_function(wrap_pyfunction!(mm_takes_f16, m)?)?;
    m.add("MM_KNOBS", mm_knob_table())?;
    m.add("VECTOR_DEFAULTS", vector_defaults())?;
    m.add("ROWS_DEFAULTS", rows_defaults())?;
    m.add("RELAYOUT_DEFAULTS", relayout_defaults())?;
    m.add("PREPARED_DEFAULTS", prepared_defaults())?;
    m.add("ZERO_SIGN", ktpu_l2::ZeroSign::DEFAULT.name())?;
    m.add(
        "ZERO_SIGNS",
        ktpu_l2::ZeroSign::ALL.map(ktpu_l2::ZeroSign::name).to_vec(),
    )?;
    m.add_function(wrap_pyfunction!(bind, m)?)?;
    m.add_function(wrap_pyfunction!(bind_cache_stats, m)?)?;
    m.add_function(wrap_pyfunction!(bind_cache_configure, m)?)?;
    m.add_function(wrap_pyfunction!(compile_many, m)?)?;
    m.add_function(wrap_pyfunction!(template_compile, m)?)?;
    bindings::register(m)?;
    library::register(m)?;
    m.add("Refused", m.py().get_type::<Refused>())?;
    m.add("MUTANTS", crate::mutant::names())?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
