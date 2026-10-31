//! Retained submission metadata, native allocation translation and run records.

use super::{
    arena::{Arena, OutOfMemory},
    cores::Cores,
    direct::{bump, DirectWire},
    mmu::NativeMMU,
    recipe::{LaunchRecipe, RecipeEpoch},
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use std::collections::BTreeSet;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};
use std::time::Instant;

#[pyclass(module = "kohakutpu.compiler._rs", get_all, set_all)]
pub(crate) struct Submitted {
    name: String,
    kind: String,
    info: Py<PyDict>,
    scratch: Py<PyList>,
    t0: f64,
    pending: Py<PyAny>,
    result: Py<PyAny>,
    run: Py<PyAny>,
    references: Py<PyList>,
    writes: Py<PyList>,
    nodes: Py<PyTuple>,
    error: Py<PyAny>,
}

#[pyclass(module = "kohakutpu.compiler._rs", get_all)]
struct NativeRun {
    name: String,
    kind: String,
    info: Py<PyDict>,
    cycles: u64,
    wall: f64,
}

pub(crate) fn healthy(dev: &Bound<'_, PyAny>) -> PyResult<()> {
    if dev.getattr("_uncertain")?.len()? != 0 || !dev.getattr("_failure")?.is_none() {
        return Err(PyRuntimeError::new_err(
            "queued failure or uncertain publication; close the device",
        ));
    }
    Ok(())
}

fn publication(card: &Bound<'_, PyAny>) -> PyResult<u64> {
    card.getattr("_publication_epoch")?.extract()
}

type AllocSpec = (u64, String, Option<u32>, String);
type Binding = (u32, usize, u64);
type Package = (u32, Py<LaunchRecipe>, Vec<usize>);

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct NativeSubmit {
    name: String,
    kind: String,
    info: Py<PyDict>,
    scratch: Vec<AllocSpec>,
    bindings: Vec<Binding>,
    packages: Vec<Package>,
    resident: bool,
    scope: (Arc<AtomicU64>, u64),
    monotonic: Py<PyAny>,
}

pub(crate) fn allocate(py: Python<'_>, dev: &Bound<'_, PyAny>, spec: &AllocSpec) -> PyResult<u64> {
    let &(size, ref space, node, ref owner) = spec;
    let line: u64 = dev.getattr("line_bytes")?.extract()?;
    let extent = size.div_ceil(line).max(1) * line;
    let memory = dev.getattr("memory")?;
    let arena: Py<Arena> = if space == "stage" {
        let node = node.unwrap_or(
            dev.getattr("card")?
                .getattr("nodes")?
                .get_item(0)?
                .extract()?,
        );
        memory.getattr("staging")?.get_item(node)?.extract()?
    } else {
        memory.getattr("dram")?.extract()?
    };
    let at = loop {
        let got = arena.borrow_mut(py).alloc(py, extent, owner, None);
        match got {
            Ok(at) => break at,
            Err(e) if e.is_instance_of::<OutOfMemory>(py) => {
                let inflight = dev.getattr("inflight")?;
                if inflight.len()? != 0 {
                    dev.call_method1("collect", (inflight.get_item(0)?,))?;
                } else {
                    dev.getattr("card")?.call_method0("drop_spilled")?;
                    break arena.borrow_mut(py).alloc(py, extent, owner, None)?;
                }
            }
            Err(e) => return Err(e),
        }
    };
    if space != "stage" {
        let end = at
            + arena
                .borrow(py)
                .live
                .bind(py)
                .get_item(at)?
                .unwrap()
                .extract::<u64>()?;
        let painted: u64 = memory.getattr("painted")?.extract()?;
        let paint = memory.getattr("paint")?;
        if !paint.is_none() && end > painted {
            // The only Python I/O here paints new DRAM high-water pages, never warm spans.
            const CHUNK: u64 = 16 << 20;
            let mut start = painted;
            while start < end {
                let bytes = (end - start).min(CHUNK) as usize;
                let zeros = PyBytes::new_with(py, bytes, |b| {
                    b.fill(0);
                    Ok(())
                })?;
                paint.call1((start, zeros))?;
                start += bytes as u64;
            }
            memory.setattr("painted", end)?;
        }
    }
    arena.borrow(py).requested.bind(py).set_item(at, size)?;
    let mmu: Py<NativeMMU> = dev.getattr("mmu")?.extract()?;
    let registered = mmu
        .borrow_mut(py)
        .register(py, at, extent, arena.clone_ref(py));
    if registered.is_err() {
        arena.borrow_mut(py).release(py, at)?;
    }
    registered
}

pub(crate) fn release(py: Python<'_>, dev: &Bound<'_, PyAny>, address: u64) -> PyResult<()> {
    let mmu: Py<NativeMMU> = dev.getattr("mmu")?.extract()?;
    let (entry, _) = mmu.borrow_mut(py).resolve(py, address, 1, true)?;
    let entry = entry.borrow(py);
    entry.arena.borrow_mut(py).release(py, entry.physical)?;
    mmu.borrow_mut(py).retire(py, address)?;
    Ok(())
}

#[pymethods]
impl NativeSubmit {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        name: String,
        kind: String,
        info: Py<PyDict>,
        scratch: Vec<AllocSpec>,
        bindings: Vec<Binding>,
        packages: Vec<Package>,
        resident: bool,
        scope: (PyRef<'_, RecipeEpoch>, u64),
    ) -> PyResult<Self> {
        Ok(Self {
            name,
            kind,
            info,
            scratch,
            bindings,
            packages,
            resident,
            scope: (scope.0.generation.clone(), scope.1),
            monotonic: py.import("time")?.getattr("monotonic")?.unbind(),
        })
    }

    #[pyo3(signature=(dev,handles,readonly=Vec::new(),writable=Vec::new()))]
    pub fn launch(
        &self,
        py: Python<'_>,
        dev: &Bound<'_, PyAny>,
        handles: Vec<u64>,
        readonly: Vec<u64>,
        writable: Vec<u64>,
    ) -> PyResult<Py<Submitted>> {
        let owner: Py<RecipeEpoch> = dev.getattr("recipe_epoch")?.extract()?;
        if !Arc::ptr_eq(&owner.borrow(py).generation, &self.scope.0)
            || self.scope.0.load(Ordering::Relaxed) != self.scope.1
        {
            return Err(PyValueError::new_err(
                "native submission has a foreign or invalidated owner",
            ));
        }
        healthy(dev)?;
        if dev.getattr("closed")?.extract::<bool>()?
            || dev.getattr("recording")?.extract::<bool>()?
        {
            return Err(PyRuntimeError::new_err(
                "native submission requires an open non-recording owner",
            ));
        }
        let inflight = dev.getattr("inflight")?.cast_into::<PyList>()?;
        let depth: Option<usize> = dev.getattr("max_inflight")?.extract()?;
        while depth.is_some_and(|n| inflight.len() >= n) {
            dev.call_method1("collect", (inflight.get_item(0)?,))?;
        }
        let card = dev.getattr("card")?;
        let direct: Option<Py<DirectWire>> = card.getattr("native_direct")?.extract()?;
        let suspended: bool = card.getattr("native_direct_suspended")?.extract()?;
        let python_publication = mutant_on!(py, "bindings.submit/python_publication");
        let direct = direct.filter(|_| {
            !suspended && !python_publication && self.packages.len() == 1 && inflight.is_empty()
        });
        let direct = match direct {
            Some(d) if d.borrow(py).available(py)? => Some(d),
            _ => None,
        };
        let started = Instant::now();
        let scratch = PyList::empty(py);
        let refs = PyList::empty(py);
        let writes = PyList::empty(py);
        let nodes = PyTuple::new(py, self.packages.iter().map(|p| p.0))?;
        let mmu: Py<NativeMMU> = dev.getattr("mmu")?.extract()?;
        let epoch = publication(&card)?;
        let pending = Py::new(
            py,
            Submitted {
                name: self.name.clone(),
                kind: self.kind.clone(),
                info: self.info.bind(py).copy()?.unbind(),
                scratch: scratch.clone().unbind(),
                t0: if direct.is_some() {
                    0.0
                } else {
                    self.monotonic.bind(py).call0()?.extract()?
                },
                pending: py.None(),
                result: py.None(),
                run: py.None(),
                references: refs.clone().unbind(),
                writes: writes.clone().unbind(),
                nodes: nodes.clone().unbind(),
                error: py.None(),
            },
        )?;
        let execute = || -> PyResult<()> {
            let mut temporary = Vec::new();
            for spec in &self.scratch {
                let at = allocate(py, dev, spec)?;
                temporary.push(at);
                scratch.append(at)?;
            }
            let full: Vec<u64> = self
                .bindings
                .iter()
                .map(|&(kind, index, off)| {
                    if kind == 0 {
                        handles[index] + off
                    } else {
                        temporary[index] + off
                    }
                })
                .collect();
            let mut seen = BTreeSet::new();
            let mut readonly_set = BTreeSet::new();
            for address in readonly {
                let (entry, _) = mmu.borrow_mut(py).resolve(py, address, 1, false)?;
                readonly_set.insert(entry.borrow(py).physical);
            }
            for address in writable {
                let (entry, _) = mmu.borrow_mut(py).resolve(py, address, 1, false)?;
                readonly_set.remove(&entry.borrow(py).physical);
            }
            for &address in &full {
                let (entry, _) = mmu.borrow_mut(py).resolve(py, address, 1, true)?;
                let entry = entry.borrow(py);
                if seen.insert(entry.virtual_address) {
                    entry.arena.borrow_mut(py).retain(py, entry.physical)?;
                    let reference = (&entry.arena, entry.physical);
                    refs.append(reference)?;
                    if !readonly_set.contains(&entry.physical) {
                        writes.append(reference)?;
                    }
                }
            }
            if nodes.len() > 1
                || inflight.iter().any(|s| {
                    s.getattr("nodes")
                        .and_then(|n| n.eq(&nodes))
                        .is_ok_and(|eq| !eq)
                })
            {
                dev.call_method0("drain")?;
            }
            if direct.is_some() {
                let mut held = 0_u64;
                for reference in refs.iter() {
                    let (arena, at): (Py<Arena>, u64) = reference.extract()?;
                    held += arena
                        .borrow(py)
                        .live
                        .bind(py)
                        .get_item(at)?
                        .unwrap()
                        .extract::<u64>()?;
                }
                if held > dev.getattr("queue_bytes")?.extract::<u64>()? {
                    return Err(OutOfMemory::new_err(
                        "one run exceeds the queued pinned-byte budget",
                    ));
                }
            } else {
                dev.call_method1("_admit", (&nodes, &refs))?;
            }
            let raw = PyDict::new(py);
            let debug = PyDict::new(py);
            let mut predicted = Vec::new();
            for (mesh, recipe, slots) in &self.packages {
                let bound: Vec<u64> = slots.iter().map(|&i| full[i]).collect();
                let translations = mmu.borrow_mut(py).translations_for(py, bound.clone())?;
                let cores: Option<Py<Cores>> = if self.resident {
                    Some(card.getattr("residents")?.get_item(mesh)?.extract()?)
                } else {
                    let state: Py<Cores> = card.getattr("residents")?.get_item(mesh)?.extract()?;
                    for core in state.borrow_mut(py).inner.cores.values_mut() {
                        core.forget();
                    }
                    None
                };
                let next = cores
                    .as_ref()
                    .map(|c| Py::new(py, c.borrow(py).clone()))
                    .transpose()?;
                debug.set_item(mesh, (recipe, bound.clone(), translations.clone()))?;
                let bytes = recipe.borrow_mut(py).launch(
                    py,
                    bound,
                    Some(translations),
                    next.as_ref().map(|c| c.borrow_mut(py)),
                )?;
                raw.set_item(mesh, (bytes, PyTuple::empty(py)))?;
                if let (Some(cores), Some(next)) = (cores, next) {
                    predicted.push((cores, next));
                }
            }
            let result = if let Some(direct) = &direct {
                let data = raw.get_item(self.packages[0].0)?.unwrap().get_item(0)?;
                direct
                    .borrow_mut(py)
                    .submit(py, &card, data.cast::<PyBytes>()?.as_bytes())?
                    .into_any()
            } else {
                card.call_method1("submit_wire", (raw, debug))?
            };
            for (cores, next) in predicted {
                cores.borrow_mut(py).inner = next.borrow(py).inner.clone();
            }
            let mut s = pending.borrow_mut(py);
            if result.is_instance_of::<PyDict>() {
                s.result = result.unbind();
            } else {
                s.pending = result.unbind();
            }
            Ok(())
        };
        if let Err(error) = execute() {
            let published = publication(&card)?;
            if published != epoch {
                dev.getattr("_uncertain")?
                    .call_method1("append", (&pending,))?;
            } else {
                for address in scratch.iter() {
                    release(py, dev, address.extract()?)?;
                }
                for reference in refs.iter() {
                    let (arena, at): (Py<Arena>, u64) = reference.extract()?;
                    arena.borrow_mut(py).unretain(py, at)?;
                }
            }
            return Err(error);
        }
        if direct.is_some() {
            let mut s = pending.borrow_mut(py);
            for reference in refs.iter() {
                let (arena, at): (Py<Arena>, u64) = reference.extract()?;
                arena.borrow_mut(py).unretain(py, at)?;
            }
            for address in scratch.iter() {
                release(py, dev, address.extract()?)?;
            }
            s.references = PyList::empty(py).unbind();
            s.scratch = PyList::empty(py).unbind();
            let cycles: u64 = s.result.bind(py).get_item("cycles")?.extract()?;
            let run = Py::new(
                py,
                NativeRun {
                    name: self.name.clone(),
                    kind: self.kind.clone(),
                    info: s.info.clone_ref(py),
                    cycles,
                    wall: started.elapsed().as_secs_f64(),
                },
            )?;
            dev.getattr("runs")?.cast::<PyList>()?.append(&run)?;
            let counters = dev.getattr("counters")?.cast_into::<PyDict>()?;
            bump(&counters, "runs", 1)?;
            bump(&counters, "cycles", cycles)?;
            s.run = run.into_any();
        } else {
            inflight.append(&pending)?;
        }
        Ok(pending)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeRun>()?;
    m.add_class::<Submitted>()?;
    m.add_class::<NativeSubmit>()
}
