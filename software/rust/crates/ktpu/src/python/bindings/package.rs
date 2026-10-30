//! L0 packages built and read without
//! the Python package modules, for the firmware tools, the debug spec
//! runner, and the package interpreter the driver tests run on.

use super::{machine, mutated, Refused};
use ktpu_isa::W256;
use ktpu_l0::format as F;
use ktpu_l0::spec::build::{builder, call, named};
use ktpu_l0::{engine, Alu, Cond, Facts, Op, Package};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyInt, PyList};
use serde_json::{Map, Value};
use std::sync::{Arc, Mutex, OnceLock};

use super::super::LruMap;

pub(super) type AdmissionContext = (Facts, Arc<ktpu_verify::Target>);

pub(super) fn admission_context(
    machine_json: &str,
    mesh: u32,
    cached: bool,
) -> PyResult<AdmissionContext> {
    static CONTEXTS: OnceLock<Mutex<LruMap<(String, u32), AdmissionContext>>> = OnceLock::new();
    let contexts = CONTEXTS.get_or_init(|| Mutex::new(LruMap::new(64)));
    let key = (machine_json.to_owned(), mesh);
    if cached {
        if let Some(context) = contexts.lock().unwrap_or_else(|e| e.into_inner()).get(&key) {
            return Ok(context);
        }
    }
    let m = machine(machine_json)?;
    let f = Facts::of_mesh(&m, mesh).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let target = ktpu_verify::Target::from_machine(&m, None).map_err(PyValueError::new_err)?;
    let context = (f, Arc::new(target));
    if cached {
        contexts
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(key, context.clone());
    }
    Ok(context)
}

pub(super) fn admit_package(
    pkg: &Package,
    mesh: u32,
    target: &ktpu_verify::Target,
) -> PyResult<()> {
    ktpu_verify::lines::admit(pkg, mesh, target).map_err(PyValueError::new_err)
}

/// An L0 refusal as `Refused`, its `clause` set.
fn refused(py: Python<'_>, r: ktpu_l0::Refusal) -> PyErr {
    let err = Refused::new_err(r.to_string());
    let _ = err.value(py).setattr("clause", r.clause);
    let _ = err.value(py).setattr("stage", "package");
    err
}

fn wide(py: Python<'_>, w: W256) -> PyResult<Bound<'_, PyAny>> {
    py.get_type::<PyInt>()
        .call_method1("from_bytes", (w.to_le_bytes().to_vec(), "little"))
}

fn facts(machine_json: &str, mesh: u32) -> PyResult<Facts> {
    let m = machine(machine_json)?;
    Facts::of_mesh(&m, mesh).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// What `package_build` makes before it is written: a built package, or a
/// refusal; `Err` for a spec or call that does not parse.
type Built = Result<Result<Package, ktpu_l0::Refusal>, String>;

/// package_build(builder_json, calls_json, machine_json, mesh=0,
/// defaults=True, checksum=False, lower=False, mutant=None) -> bytes: a
/// package from `PackageBuilder` calls (`ktpu_l0::spec::build`'s forms: the
/// builder as `{"types": [[x, y, name]], "credit", "mesh", "signature",
/// "fetch"}`, each call `[name, *args, {kw}]`), built (`defaults`,
/// `checksum`), with `lower` lowered for the dispatch engine, written.
#[pyfunction]
#[pyo3(signature = (builder_json, calls_json, machine_json, mesh=0, defaults=true, checksum=false, lower=false, mutant=None))]
#[allow(clippy::too_many_arguments)]
fn package_build<'py>(
    py: Python<'py>,
    builder_json: &str,
    calls_json: &str,
    machine_json: &str,
    mesh: u32,
    defaults: bool,
    checksum: bool,
    lower: bool,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    let f = facts(machine_json, mesh)?;
    let spec: Value = serde_json::from_str(builder_json)
        .map_err(|e| PyValueError::new_err(format!("a builder: {e}")))?;
    let calls: Vec<Value> = serde_json::from_str(calls_json)
        .map_err(|e| PyValueError::new_err(format!("builder calls: {e}")))?;
    let run = || -> Built {
        let mut b = builder(&spec, &f)?;
        for c in &calls {
            let c = c.as_array().ok_or("a call is [name, *args, {kw}]")?;
            let name = c.first().and_then(Value::as_str).ok_or("a call's name")?;
            let mut args: Vec<Value> = c[1..].to_vec();
            let kw = match args.last() {
                Some(Value::Object(o)) => {
                    let o = o.clone();
                    args.pop();
                    o
                }
                _ => Map::new(),
            };
            if let Err(r) = call(&mut b, name, &named(name, &args, &kw)?)? {
                return Ok(Err(r));
            }
        }
        let pkg = b.build(defaults, checksum);
        Ok(match lower {
            true => engine::lower(&pkg, &f),
            false => Ok(pkg),
        })
    };
    let pkg = mutated(mutant, run)?
        .map_err(PyValueError::new_err)?
        .map_err(|r| refused(py, r))?;
    let raw = mutated(mutant, || pkg.to_bytes(&f))?.map_err(|r| refused(py, r))?;
    Ok(PyBytes::new(py, &raw))
}

/// package_read(package, machine_json, mesh=0, bindings=None, mutant=None)
/// -> dict: the package's tables as ints (`units`, `buffers`, `steps`,
/// `relocs`, `payloads`, `signature`, `ack_reserve`, `checksum`), each
/// ENGINE step's `(code, value)` entries by step index (`engines`), and with
/// `bindings` the payloads relocated (`bound`).
#[pyfunction]
#[pyo3(signature = (package, machine_json, mesh=0, bindings=None, mutant=None))]
fn package_read<'py>(
    py: Python<'py>,
    package: &[u8],
    machine_json: &str,
    mesh: u32,
    bindings: Option<Vec<u64>>,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    let f = facts(machine_json, mesh)?;
    let run = || -> Result<(Package, Option<Vec<W256>>), ktpu_l0::Refusal> {
        let pkg = Package::from_bytes(package, &f)?;
        let bound = match &bindings {
            Some(b) => Some(pkg.bound(b)?),
            None => None,
        };
        Ok((pkg, bound))
    };
    let (pkg, bound) = mutated(mutant, run)?.map_err(|r| refused(py, r))?;
    let d = PyDict::new(py);
    let units = PyList::empty(py);
    for u in &pkg.units {
        let one = PyDict::new(py);
        one.set_item("type", u.type_code)?;
        one.set_item("x", u.x)?;
        one.set_item("y", u.y)?;
        one.set_item("mesh", u.mesh)?;
        one.set_item("credit", u.credit)?;
        one.set_item("fetch", u.fetch)?;
        units.append(one)?;
    }
    d.set_item("units", units)?;
    let buffers = PyList::empty(py);
    for b in &pkg.buffers {
        let one = PyDict::new(py);
        one.set_item("size", b.size)?;
        one.set_item("default", b.default)?;
        one.set_item("kind", b.kind as u8)?;
        one.set_item("mesh", b.mesh)?;
        buffers.append(one)?;
    }
    d.set_item("buffers", buffers)?;
    let steps: Vec<(u8, u8, u64, u64, u64)> = pkg
        .steps
        .iter()
        .map(|s| (s.op as u8, s.flags, s.unit, s.count, s.arg))
        .collect();
    d.set_item("steps", steps)?;
    let relocs: Vec<(u64, u64, u64, u64, i128, u64)> = pkg
        .relocs
        .iter()
        .map(|r| (r.payload, r.bit, r.width, r.buffer, r.addend, r.shift))
        .collect();
    d.set_item("relocs", relocs)?;
    let ints = |ws: &[W256]| -> PyResult<Bound<'py, PyList>> {
        let l = PyList::empty(py);
        for w in ws {
            l.append(wide(py, *w)?)?;
        }
        Ok(l)
    };
    d.set_item("payloads", ints(&pkg.payloads)?)?;
    d.set_item("signature", pkg.signature)?;
    d.set_item("ack_reserve", pkg.ack_reserve)?;
    d.set_item("checksum", pkg.checksum)?;
    let engines = PyDict::new(py);
    for (i, s) in pkg.steps.iter().enumerate() {
        if s.op == Op::Engine {
            engines.set_item(i, engine::entries(&pkg, s))?;
        }
    }
    d.set_item("engines", engines)?;
    match bound {
        Some(b) => d.set_item("bound", ints(&b)?)?,
        None => d.set_item("bound", py.None())?,
    }
    Ok(d)
}

/// package_stream(package, entries_json, machine_json, mesh=0, mutant=None)
/// -> bytes: `package` with its steps replaced by one ENGINE step of
/// `entries` (`[["put", code, value] | ["wait", source, count]]`, a WAIT
/// whose count is not positive left out), their payloads appended
/// (`ktpu_l0::engine::stream`), written.
#[pyfunction]
#[pyo3(signature = (package, entries_json, machine_json, mesh=0, mutant=None))]
fn package_stream<'py>(
    py: Python<'py>,
    package: &[u8],
    entries_json: &str,
    machine_json: &str,
    mesh: u32,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    let f = facts(machine_json, mesh)?;
    let raw: Vec<(String, u64, i128)> = serde_json::from_str(entries_json)
        .map_err(|e| PyValueError::new_err(format!("engine entries: {e}")))?;
    let entries = raw
        .into_iter()
        .map(|(kind, a, b)| match kind.as_str() {
            "put" => u64::try_from(b)
                .map(|v| engine::Entry::Put(a, v))
                .map_err(|_| format!("a put of {b}")),
            "wait" => Ok(engine::Entry::Wait(a, b)),
            other => Err(format!("no engine entry {other:?}")),
        })
        .collect::<Result<Vec<_>, _>>()
        .map_err(PyValueError::new_err)?;
    let run = || -> Result<Vec<u8>, ktpu_l0::Refusal> {
        let pkg = Package::from_bytes(package, &f)?;
        engine::stream(&pkg, &f, &entries).to_bytes(&f)
    };
    let raw = mutated(mutant, run)?.map_err(|r| refused(py, r))?;
    Ok(PyBytes::new(py, &raw))
}

/// package_format(machine_json, mesh=0) -> dict: the codes a package's
/// reader names: `op`, `alu`, `cond` (name -> code), the step flags, `regs`,
/// `mover_skip`, `any_unit`, and the dispatch engine's register codes,
/// `src_mover`, `ctr_mask` and payload bytes.
#[pyfunction]
#[pyo3(signature = (machine_json, mesh=0))]
fn package_format<'py>(
    py: Python<'py>,
    machine_json: &str,
    mesh: u32,
) -> PyResult<Bound<'py, PyDict>> {
    let f = facts(machine_json, mesh)?;
    let d = PyDict::new(py);
    let named = |pairs: Vec<(String, u8)>| -> PyResult<Bound<'py, PyDict>> {
        let o = PyDict::new(py);
        for (k, v) in pairs {
            o.set_item(k, v)?;
        }
        Ok(o)
    };
    let up = |s: String| s.to_uppercase();
    d.set_item(
        "op",
        named(
            Op::ALL
                .iter()
                .map(|o| (o.name().to_string(), *o as u8))
                .collect(),
        )?,
    )?;
    d.set_item(
        "alu",
        named(
            Alu::ALL
                .iter()
                .map(|a| (up(format!("{a:?}")), *a as u8))
                .collect(),
        )?,
    )?;
    d.set_item(
        "cond",
        named(
            Cond::ALL
                .iter()
                .map(|c| (up(format!("{c:?}")), *c as u8))
                .collect(),
        )?,
    )?;
    for (k, v) in [
        ("F_POSTED", F::F_POSTED),
        ("F_IMM", F::F_IMM),
        ("F_REG", F::F_REG),
        ("F_INDEXED", F::F_INDEXED),
        ("F_CTRL", F::F_CTRL),
        ("F_CONSUME", F::F_CONSUME),
        ("F_MEM", F::F_MEM),
        ("F_ACK", F::F_ACK),
    ] {
        d.set_item(k, v)?;
    }
    for (k, v) in [
        ("REGS", f.regs),
        ("MOVER_SKIP", F::MOVER_SKIP),
        ("ANY_UNIT", F::ANY_UNIT),
        ("WAIT", engine::WAIT),
        ("REL", engine::REL),
        ("NONE", engine::NONE),
        ("DST", engine::DST),
        ("ARG0", engine::ARG0),
        ("ARG1", engine::ARG1),
        ("ARG2", engine::ARG2),
        ("ARG3", engine::ARG3),
        ("GO", engine::GO),
        ("IL_RING", engine::IL_RING),
        ("TYPED_MEM_RD_REQ", engine::TYPED_MEM_RD_REQ),
        ("SRC_MOVER", f.src_mover()),
        ("CTR_MASK", f.ctr_mask()),
        ("PAYLOAD", f.word_bytes),
    ] {
        d.set_item(k, v)?;
    }
    Ok(d)
}

/// Derive T1 metadata from precompiled samples; this entry never compiles.
#[pyfunction]
#[pyo3(signature = (packages, params_json, class_json, machine_json, mesh=0, values_json="{}", confined=false))]
#[allow(clippy::too_many_arguments)]
fn template_derive<'py>(
    py: Python<'py>,
    packages: Vec<Vec<u8>>,
    params_json: &str,
    class_json: &str,
    machine_json: &str,
    mesh: u32,
    values_json: &str,
    confined: bool,
) -> PyResult<Bound<'py, PyDict>> {
    let recipes: crate::templates::values::Recipes =
        serde_json::from_str(values_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let f = facts(machine_json, mesh)?;
    let params: Vec<F::template::Params> =
        serde_json::from_str(params_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let class =
        serde_json::from_str(class_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    if params.len() != packages.len() {
        return Err(PyValueError::new_err("one parameter row per package"));
    }
    let samples = packages
        .iter()
        .zip(params)
        .enumerate()
        .map(|(i, (raw, params))| {
            Ok(crate::templates::Sample {
                label: format!("sample {i}"),
                params,
                package: Package::from_bytes(raw, &f).map_err(|r| refused(py, r))?,
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    let (t, certificate) = if confined {
        let (t, cert) =
            crate::templates::derive_confined(class, &samples, &recipes, &*machine(machine_json)?)
                .map_err(PyValueError::new_err)?;
        (t, Some(cert))
    } else {
        (
            crate::templates::derive_with_values(class, &samples, &recipes)
                .map_err(PyValueError::new_err)?,
            None,
        )
    };
    for s in &samples {
        let binds = vec![0; s.package.buffers.len()];
        let mut expanded = s.clone();
        expanded.params =
            crate::templates::values::bind(&recipes, &s.params).map_err(PyValueError::new_err)?;
        crate::templates::prove(&t, &expanded, &binds, &f).map_err(PyValueError::new_err)?;
    }
    let out = PyDict::new(py);
    out.set_item(
        "confinement_json",
        serde_json::to_string(&certificate).map_err(|e| PyValueError::new_err(e.to_string()))?,
    )?;
    out.set_item(
        "bytes",
        PyBytes::new(py, &t.package.to_bytes(&f).map_err(|r| refused(py, r))?),
    )?;
    let m = machine(machine_json)?;
    let isa = ktpu_isa::unit::vector::VecIsa::new(&m)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let images = ktpu_l1::lower::manifest::describe(&t.package, &isa);
    let mut table =
        serde_json::to_value(&t.table).map_err(|e| PyValueError::new_err(e.to_string()))?;
    table["images"] = images.clone();
    out.set_item("images_json", images.to_string())?;
    out.set_item("template_json", table.to_string())?;
    Ok(out)
}

/// Independent launch proof, including every serialized package byte.
#[pyfunction]
#[pyo3(signature = (package, template_json, direct, params_json, bindings, machine_json, label, mesh=0))]
#[allow(clippy::too_many_arguments)]
fn template_prove(
    py: Python<'_>,
    package: &[u8],
    template_json: &str,
    direct: &[u8],
    params_json: &str,
    bindings: Vec<u64>,
    machine_json: &str,
    label: &str,
    mesh: u32,
) -> PyResult<()> {
    let f = facts(machine_json, mesh)?;
    let template = F::template::Template {
        package: Package::from_bytes(package, &f).map_err(|r| refused(py, r))?,
        table: crate::python::template_table(template_json)
            .map_err(|e| PyValueError::new_err(e.to_string()))?,
    };
    let sample = crate::templates::Sample {
        label: label.into(),
        params: serde_json::from_str(params_json)
            .map_err(|e| PyValueError::new_err(e.to_string()))?,
        package: Package::from_bytes(direct, &f).map_err(|r| refused(py, r))?,
    };
    crate::templates::prove(&template, &sample, &bindings, &f).map_err(PyValueError::new_err)
}

#[pyfunction]
#[pyo3(signature = (width, kblock, max_block, policy="pow2"))]
fn template_width(width: u64, kblock: u64, max_block: u64, policy: &str) -> PyResult<Option<u64>> {
    use crate::templates::policy::ShapeClass;
    let p = match policy {
        "exact" => ShapeClass::Exact,
        "pow2" => ShapeClass::Pow2,
        "coarse" => ShapeClass::Coarse,
        _ => return Err(PyValueError::new_err("ShapeClass: exact, pow2 or coarse")),
    };
    p.width(width, kblock, max_block)
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn template_points(class_json: &str) -> PyResult<String> {
    let class =
        serde_json::from_str(class_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let points = crate::templates::points(&class, 4096).map_err(PyValueError::new_err)?;
    serde_json::to_string(&points).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Alias environment assumed by coverage, after resolving default bindings.
#[pyfunction]
#[pyo3(signature = (package, bindings, machine_json, mesh=0))]
fn template_environment(
    package: &[u8],
    bindings: Vec<u64>,
    machine_json: &str,
    mesh: u32,
) -> PyResult<String> {
    let f = facts(machine_json, mesh)?;
    let package =
        Package::from_bytes(package, &f).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let addresses = F::bind::bind_addresses(&package.buffers, &bindings);
    let environment = F::bind::environment(&package.buffers, &addresses)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    serde_json::to_string(&environment).map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pyfunction]
fn template_certificate(package: &[u8], table: &str, machine_json: &str) -> PyResult<String> {
    let f = facts(machine_json, 0)?;
    let t = F::template::Template {
        package: Package::from_bytes(package, &f)
            .map_err(|e| PyValueError::new_err(e.to_string()))?,
        table: crate::python::template_table(table)
            .map_err(|e| PyValueError::new_err(e.to_string()))?,
    };
    let certificate = crate::templates::confinement::derive(&t, &*machine(machine_json)?)
        .map_err(PyValueError::new_err)?;
    serde_json::to_string(&certificate).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Full checker coverage over a template box; no compiler is called.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (packages, tables, bindings, machine_json, digest, interior=16, cap=65536, confined=false, mode="sample"))]
fn template_verify(
    packages: std::collections::BTreeMap<u32, Vec<u8>>,
    tables: std::collections::BTreeMap<u32, String>,
    bindings: std::collections::BTreeMap<u32, Vec<u64>>,
    machine_json: &str,
    digest: &str,
    interior: usize,
    cap: usize,
    confined: bool,
    mode: &str,
) -> PyResult<String> {
    let m = machine(machine_json)?;
    let target = ktpu_verify::Target::from_machine(&m, None).map_err(PyValueError::new_err)?;
    let mut templates: std::collections::BTreeMap<u32, F::template::Template> = packages
        .iter()
        .map(|(mesh, raw)| {
            let f = facts(machine_json, *mesh)?;
            Ok((
                *mesh,
                F::template::Template {
                    package: Package::from_bytes(raw, &f)
                        .map_err(|e| PyValueError::new_err(e.to_string()))?,
                    table: crate::python::template_table(
                        tables
                            .get(mesh)
                            .ok_or_else(|| PyValueError::new_err("missing mesh table"))?,
                    )
                    .map_err(|e| PyValueError::new_err(e.to_string()))?,
                },
            ))
        })
        .collect::<PyResult<_>>()?;
    if mode == "proof" {
        let verdict = match crate::templates::proof::prove(&templates, &bindings, &m, cap) {
            Ok(proven) => {
                serde_json::json!({"status": "PROVEN", "certificate": proven.certificate()})
            }
            Err(refusal) => serde_json::json!({"status": "REFUSED", "refusal": refusal}),
        };
        return Ok(verdict.to_string());
    }
    if mode != "sample" {
        return Err(PyValueError::new_err(
            "template verification mode: sample or proof",
        ));
    }
    if confined {
        let certificates = templates
            .values()
            .map(|t| crate::templates::confinement::derive(t, &m))
            .collect::<Result<Vec<_>, _>>()
            .map_err(PyValueError::new_err)?;
        for t in templates.values_mut() {
            for (name, bounds) in &mut t.table.parameters {
                if certificates
                    .iter()
                    .all(|c| c.data_only.get(name) == Some(&true))
                {
                    bounds.hi = bounds.lo;
                }
            }
        }
    }
    let points = crate::templates::verification::verify(
        &templates, &bindings, digest, &target, interior, cap,
    )
    .map_err(PyValueError::new_err)?;
    serde_json::to_string(&points).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// An opaque, in-process proof capability tied to exact contents and addresses.
#[pyclass(frozen)]
struct TemplateProof {
    proven: crate::templates::proof::ProvenBox,
    facts: Facts,
    mesh: u32,
}

#[pymethods]
impl TemplateProof {
    #[getter]
    fn certificate(&self) -> PyResult<String> {
        serde_json::to_string(self.proven.certificate())
            .map_err(|e| PyValueError::new_err(e.to_string()))
    }

    fn bind<'py>(&self, py: Python<'py>, params_json: &str) -> PyResult<Bound<'py, PyBytes>> {
        let params =
            serde_json::from_str(params_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let nodes = self.proven.bind(&params).map_err(PyValueError::new_err)?;
        let raw = nodes[&self.mesh]
            .to_bytes(&self.facts)
            .map_err(|e| refused(py, e))?;
        Ok(PyBytes::new(py, &raw))
    }
}

/// Proof-based admission to the explicit L0 box; no direct compiler is consulted.
#[pyfunction]
#[pyo3(signature = (package, table, bindings, machine_json, cap=65536, mesh=0))]
fn template_certify(
    package: &[u8],
    table: &str,
    bindings: Vec<u64>,
    machine_json: &str,
    cap: usize,
    mesh: u32,
) -> PyResult<TemplateProof> {
    let facts = facts(machine_json, mesh)?;
    let template = F::template::Template {
        package: Package::from_bytes(package, &facts)
            .map_err(|e| PyValueError::new_err(e.to_string()))?,
        table: crate::python::template_table(table)
            .map_err(|e| PyValueError::new_err(e.to_string()))?,
    };
    let m = machine(machine_json)?;
    let proven = crate::templates::proof::prove(
        &[(mesh, template)].into(),
        &[(mesh, bindings)].into(),
        &m,
        cap,
    )
    .map_err(|e| PyValueError::new_err(format!("REFUSED {}: {}", e.property, e.reason)))?;
    Ok(TemplateProof {
        proven,
        facts,
        mesh,
    })
}

#[pyfunction]
#[pyo3(signature = (packages, machine_json, mesh=0))]
fn package_link<'py>(
    py: Python<'py>,
    packages: Vec<Vec<u8>>,
    machine_json: &str,
    mesh: u32,
) -> PyResult<Bound<'py, PyBytes>> {
    let f = facts(machine_json, mesh)?;
    let parts = packages
        .iter()
        .map(|raw| Package::from_bytes(raw, &f).map_err(|e| refused(py, e)))
        .collect::<PyResult<Vec<_>>>()?;
    let linked = ktpu_l0::link::bound(&parts).map_err(PyValueError::new_err)?;
    let m = machine(machine_json)?;
    let target = ktpu_verify::Target::from_machine(&m, None).map_err(PyValueError::new_err)?;
    let nodes = [(mesh, linked.clone())].into();
    ktpu_verify::gate(&[nodes], &target, ktpu_verify::Mode::Refuse, None)
        .map_err(|e| PyValueError::new_err(format!("{e:?}")))?;
    Ok(PyBytes::new(
        py,
        &linked.to_bytes(&f).map_err(|e| refused(py, e))?,
    ))
}

#[pyfunction]
fn package_signature(unit_words: Vec<u64>) -> u64 {
    F::signature(&unit_words)
}

#[pyfunction]
fn template_values(recipes_json: &str, shape_json: &str) -> PyResult<String> {
    let recipes =
        serde_json::from_str(recipes_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let shape =
        serde_json::from_str(shape_json).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let result = crate::templates::values::bind(&recipes, &shape).map_err(PyValueError::new_err)?;
    serde_json::to_string(&result).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Admit bound MOVER and ENGINE streams against the receiving board's policy.
#[pyfunction]
#[pyo3(signature = (package, machine_json, mesh=0, bindings=None, *, cached_context=true))]
fn package_admit(
    package: &[u8],
    machine_json: &str,
    mesh: u32,
    bindings: Option<Vec<u64>>,
    cached_context: bool,
) -> PyResult<()> {
    let (f, t) = admission_context(machine_json, mesh, cached_context)?;
    let p = Package::from_bytes(package, &f).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let p = p
        .bind(&bindings.unwrap_or_default())
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    admit_package(&p, mesh, &t)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(package_admit, m)?)?;
    m.add_function(wrap_pyfunction!(package_signature, m)?)?;
    m.add_function(wrap_pyfunction!(package_link, m)?)?;
    m.add_function(wrap_pyfunction!(template_values, m)?)?;
    m.add_class::<TemplateProof>()?;
    m.add_function(wrap_pyfunction!(template_certify, m)?)?;
    m.add_function(wrap_pyfunction!(template_verify, m)?)?;
    m.add_function(wrap_pyfunction!(template_certificate, m)?)?;
    m.add_function(wrap_pyfunction!(template_environment, m)?)?;
    m.add_function(wrap_pyfunction!(template_width, m)?)?;
    m.add_function(wrap_pyfunction!(template_points, m)?)?;
    m.add_function(wrap_pyfunction!(template_derive, m)?)?;
    m.add_function(wrap_pyfunction!(template_prove, m)?)?;
    m.add_function(wrap_pyfunction!(package_build, m)?)?;
    m.add_function(wrap_pyfunction!(package_read, m)?)?;
    m.add_function(wrap_pyfunction!(package_stream, m)?)?;
    m.add_function(wrap_pyfunction!(package_format, m)?)?;
    Ok(())
}
