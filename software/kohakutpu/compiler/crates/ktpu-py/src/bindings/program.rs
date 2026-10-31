//! Address-parametric eager launch programs with per-call form transitions.

use super::direct::{bump, DirectWire};
use super::mmu::NativeMMU;
use super::recipe::RecipeEpoch;
use super::submit::{allocate, healthy, release, NativeSubmit};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PySet};
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};

struct Address {
    kind: String,
    index: usize,
    key: Py<PyAny>,
    offset: u64,
}
impl Address {
    fn parse(v: &Bound<'_, PyAny>) -> PyResult<Self> {
        let (kind, index, key, offset) = v.extract()?;
        Ok(Self {
            kind,
            index,
            key,
            offset,
        })
    }
    fn get(&self, py: Python<'_>, forms: &[Bound<'_, PyDict>], temps: &[u64]) -> PyResult<u64> {
        let base = match self.kind.as_str() {
            "form" => forms[self.index]
                .get_item(self.key.bind(py))?
                .unwrap()
                .extract()?,
            "temp" => temps[self.index],
            _ => self.index as u64,
        };
        Ok(base + self.offset)
    }
}

#[pyclass(module = "kohakutpu.compiler._rs", get_all, set_all)]
struct NativeForm {
    key: Py<PyAny>,
    addr: u64,
    nbytes: u64,
    owned: bool,
    src_st: Py<PyAny>,
}
#[pymethods]
impl NativeForm {
    fn release(&self) {}
}

struct Form {
    key: Py<PyAny>,
    address: Address,
    bytes: u64,
    owned: bool,
}
struct Effect {
    storage: usize,
    address: Address,
    forms: Vec<Form>,
    valid: Vec<Py<PyAny>>,
    zeroed: Vec<Py<PyAny>>,
    dtype: Py<PyAny>,
    site: Py<PyAny>,
    host: Py<PyAny>,
    host_dtype: Py<PyAny>,
}
impl Effect {
    #[allow(clippy::type_complexity)]
    fn parse(v: &Bound<'_, PyAny>) -> PyResult<Self> {
        let (storage, address, forms, valid, zeroed, dtype, site, host, host_dtype): (
            usize,
            Bound<'_, PyAny>,
            Vec<(Py<PyAny>, Bound<'_, PyAny>, u64, bool)>,
            Vec<Py<PyAny>>,
            Vec<Py<PyAny>>,
            Py<PyAny>,
            Py<PyAny>,
            Py<PyAny>,
            Py<PyAny>,
        ) = v.extract()?;
        Ok(Self {
            storage,
            address: Address::parse(&address)?,
            forms: forms
                .into_iter()
                .map(|(key, a, bytes, owned)| {
                    Ok(Form {
                        key,
                        address: Address::parse(&a)?,
                        bytes,
                        owned,
                    })
                })
                .collect::<PyResult<_>>()?,
            valid,
            zeroed,
            dtype,
            site,
            host,
            host_dtype,
        })
    }
    fn apply(
        &self,
        py: Python<'_>,
        storages: &[Bound<'_, PyAny>],
        forms: &[Bound<'_, PyDict>],
        temps: &[u64],
        hosts: &[Bound<'_, PyAny>],
    ) -> PyResult<()> {
        let st = &storages[self.storage];
        let dest = PyDict::new(py);
        for f in &self.forms {
            dest.set_item(
                f.key.bind(py),
                Py::new(
                    py,
                    NativeForm {
                        key: f.key.clone_ref(py),
                        addr: f.address.get(py, forms, temps)?,
                        nbytes: f.bytes,
                        owned: f.owned,
                        src_st: py.None(),
                    },
                )?,
            )?;
        }
        st.setattr("addr", self.address.get(py, forms, temps)?)?;
        st.setattr("forms", dest)?;
        st.setattr(
            "valid",
            PySet::new(py, self.valid.iter().map(|x| x.bind(py)))?,
        )?;
        st.setattr(
            "zeroed",
            PySet::new(py, self.zeroed.iter().map(|x| x.bind(py)))?,
        )?;
        st.setattr("dtype", self.dtype.bind(py))?;
        st.setattr("site", self.site.bind(py))?;
        let host = self.host.bind(py);
        st.setattr(
            "host",
            if host.is_none() || host.is_instance_of::<PyBytes>() {
                host.clone()
            } else {
                hosts[host.extract::<usize>()?].clone()
            },
        )?;
        st.setattr("host_f16_dtype", self.host_dtype.bind(py))
    }
}

enum Op {
    Alloc(usize, (u64, String, Option<u32>, String)),
    Free(Address),
    Write(Address, Py<PyBytes>),
    Effects(Vec<Effect>),
    Run(Py<NativeSubmit>, Vec<Address>),
}

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct LaunchProgram {
    operations: Vec<Op>,
    effects: Vec<Effect>,
    out: usize,
    temps: usize,
    scope: (Arc<AtomicU64>, u64),
}

#[pymethods]
impl LaunchProgram {
    #[new]
    fn new(
        operations: Vec<Bound<'_, PyAny>>,
        effects: Vec<Bound<'_, PyAny>>,
        out: usize,
        scope: (PyRef<'_, RecipeEpoch>, u64),
    ) -> PyResult<Self> {
        let mut ops = Vec::new();
        let mut temps = 0;
        for operation in operations {
            let tag: String = operation.get_item(0)?.extract()?;
            ops.push(match tag.as_str() {
                "alloc" => {
                    let (_, index, size, space, node, owner): (
                        String,
                        usize,
                        u64,
                        String,
                        Option<u32>,
                        String,
                    ) = operation.extract()?;
                    temps = temps.max(index + 1);
                    Op::Alloc(index, (size, space, node, owner))
                }
                "free" => Op::Free(Address::parse(&operation.get_item(1)?)?),
                "write" => Op::Write(
                    Address::parse(&operation.get_item(1)?)?,
                    operation.get_item(2)?.extract()?,
                ),
                "effects" => Op::Effects(
                    operation
                        .get_item(1)?
                        .try_iter()?
                        .map(|e| Effect::parse(&e?))
                        .collect::<PyResult<_>>()?,
                ),
                "native" => {
                    let (_, submit, addresses): (String, Py<NativeSubmit>, Vec<Bound<'_, PyAny>>) =
                        operation.extract()?;
                    Op::Run(
                        submit,
                        addresses
                            .iter()
                            .map(Address::parse)
                            .collect::<PyResult<_>>()?,
                    )
                }
                _ => return Err(PyValueError::new_err("unknown launch program operation")),
            });
        }
        Ok(Self {
            operations: ops,
            effects: effects.iter().map(Effect::parse).collect::<PyResult<_>>()?,
            out,
            temps,
            scope: (scope.0.generation.clone(), scope.1),
        })
    }

    pub fn launch<'py>(
        &self,
        py: Python<'py>,
        dev: &Bound<'py, PyAny>,
        storages: Vec<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyList>> {
        let owner: Py<RecipeEpoch> = dev.getattr("recipe_epoch")?.extract()?;
        if !Arc::ptr_eq(&owner.borrow(py).generation, &self.scope.0)
            || self.scope.0.load(Ordering::Relaxed) != self.scope.1
        {
            return Err(PyValueError::new_err(
                "launch program invalidated by library/machine change",
            ));
        }
        healthy(dev)?;
        if dev.getattr("closed")?.extract::<bool>()?
            || dev.getattr("recording")?.extract::<bool>()?
        {
            return Err(PyRuntimeError::new_err(
                "native program requires an open non-recording owner",
            ));
        }
        let forms: Vec<_> = storages
            .iter()
            .map(|st| {
                let got = PyDict::new(py);
                for (key, f) in st.getattr("forms")?.cast_into::<PyDict>()?.iter() {
                    got.set_item(key, f.getattr("addr")?)?;
                }
                Ok(got)
            })
            .collect::<PyResult<_>>()?;
        let hosts = storages
            .iter()
            .map(|st| st.getattr("host"))
            .collect::<PyResult<Vec<_>>>()?;
        let mut temps = vec![0; self.temps];
        let runs = PyList::empty(py);
        let mut owned = vec![false; self.temps];
        let result = (|| -> PyResult<()> {
            for op in &self.operations {
                match op {
                    Op::Alloc(i, spec) => {
                        temps[*i] = allocate(py, dev, spec)?;
                        owned[*i] = true;
                    }
                    Op::Free(address) => {
                        release(py, dev, address.get(py, &forms, &temps)?)?;
                        if address.kind == "temp" {
                            owned[address.index] = false;
                        }
                    }
                    Op::Write(address, raw) => {
                        let at = address.get(py, &forms, &temps)?;
                        let card = dev.getattr("card")?;
                        let direct: Option<Bound<'_, DirectWire>> =
                            card.getattr("native_direct")?.extract()?;
                        let suspended: bool = card.getattr("native_direct_suspended")?.extract()?;
                        let line: u64 = card.getattr("line")?.extract()?;
                        let bytes = raw.bind(py).as_bytes();
                        let mut written = false;
                        if let Some(endpoint) = direct {
                            if !suspended
                                && (bytes.len() as u64).is_multiple_of(line)
                                && dev.getattr("inflight")?.len()? == 0
                            {
                                let mmu: Py<NativeMMU> = dev.getattr("mmu")?.extract()?;
                                let (entry, physical) =
                                    mmu.borrow_mut(py)
                                        .resolve(py, at, bytes.len() as u64, true)?;
                                let is_dram = entry
                                    .borrow(py)
                                    .arena
                                    .bind(py)
                                    .is(&dev.getattr("memory")?.getattr("dram")?);
                                if physical % line == 0 && is_dram {
                                    endpoint.borrow().write_host(py, &card, physical, bytes)?;
                                    bump(
                                        &dev.getattr("counters")?.cast_into::<PyDict>()?,
                                        "bytes_in",
                                        bytes.len() as u64,
                                    )?;
                                    written = true;
                                }
                            }
                        }
                        if !written {
                            dev.call_method1("write", (at, raw.bind(py)))?;
                        }
                    }
                    Op::Effects(effects) => {
                        for effect in effects {
                            effect.apply(py, &storages, &forms, &temps, &hosts)?;
                        }
                    }
                    Op::Run(submit, addresses) => {
                        let mut readonly = Vec::new();
                        let mut writable = Vec::new();
                        for (i, st) in storages.iter().enumerate() {
                            let target = if i == self.out {
                                &mut writable
                            } else {
                                &mut readonly
                            };
                            target.push(st.getattr("addr")?.extract()?);
                            let current = st.getattr("forms")?;
                            for key in st.getattr("valid")?.try_iter()? {
                                target.push(current.get_item(key?)?.getattr("addr")?.extract()?);
                            }
                        }
                        let handles = addresses
                            .iter()
                            .map(|a| a.get(py, &forms, &temps))
                            .collect::<PyResult<_>>()?;
                        let pending = submit
                            .borrow(py)
                            .launch(py, dev, handles, readonly, writable)?;
                        let asynchronous = dev.getattr("queued")?.extract::<bool>()?
                            && pending.bind(py).getattr("result")?.is_none();
                        let completed = pending.bind(py).getattr("run")?;
                        if !completed.is_none() {
                            runs.append(completed)?;
                        } else if asynchronous {
                            runs.append(pending)?;
                        } else {
                            runs.append(dev.call_method1("collect_through", (pending,))?)?;
                        }
                    }
                }
            }
            for effect in &self.effects {
                effect.apply(py, &storages, &forms, &temps, &hosts)?;
            }
            Ok(())
        })();
        if let Err(error) = result {
            if mutant_on!(py, "bindings.program/leaks_cancelled") {
                return Err(error);
            }
            let mut adopted = std::collections::BTreeSet::<u64>::new();
            for st in &storages {
                if st.getattr("owned")?.extract::<bool>()? {
                    adopted.insert(st.getattr("addr")?.extract()?);
                }
                for (_, form) in st.getattr("forms")?.cast_into::<PyDict>()?.iter() {
                    if form.getattr("owned")?.extract::<bool>()? {
                        adopted.insert(form.getattr("addr")?.extract()?);
                    }
                }
            }
            for (i, &address) in temps.iter().enumerate() {
                if owned[i] && !adopted.contains(&address) {
                    release(py, dev, address)?;
                }
            }
            return Err(error);
        }
        Ok(runs)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeForm>()?;
    m.add_class::<LaunchProgram>()
}
