//! Allocation-generation checked virtual handles and live address translation.

use super::arena::Arena;
use pyo3::exceptions::{PyKeyError, PyMemoryError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::{BTreeMap, VecDeque};

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct Allocation {
    #[pyo3(get)]
    pub virtual_address: u64,
    #[pyo3(get)]
    pub physical: u64,
    #[pyo3(get)]
    pub extent: u64,
    #[pyo3(get)]
    pub generation: u64,
    #[pyo3(get)]
    pub arena: Py<Arena>,
}

#[pymethods]
impl Allocation {
    #[getter]
    fn r#virtual(&self) -> u64 {
        self.virtual_address
    }
    #[getter]
    pub fn live(&self, py: Python<'_>) -> PyResult<bool> {
        let arena = self.arena.borrow(py);
        Ok(arena
            .generations
            .bind(py)
            .get_item(self.physical)?
            .map(|v| v.extract::<u64>())
            .transpose()?
            == Some(self.generation))
    }
}

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct NativeMMU {
    #[pyo3(get)]
    entries: Py<PyDict>,
    spans: BTreeMap<u64, Py<Allocation>>,
    next: BTreeMap<u64, u64>,
    retired: VecDeque<u64>,
    prune_at: usize,
    #[pyo3(get, set)]
    special_bit: u32,
    #[pyo3(get, set)]
    domain_shift: u32,
    #[pyo3(get, set)]
    virtual_offset: u64,
    #[pyo3(get, set)]
    virtual_align: u64,
    #[pyo3(get, set)]
    retired_kept: usize,
}

impl NativeMMU {
    fn sync(&mut self, py: Python<'_>) {
        if self.entries.bind(py).is_empty() && !self.spans.is_empty() {
            self.spans.clear();
            self.retired.clear();
            self.prune_at = self.virtual_align as usize;
        }
    }

    fn prune(&mut self, py: Python<'_>) -> PyResult<()> {
        let mut dead = Vec::new();
        for (&at, e) in &self.spans {
            if !e.borrow(py).live(py)? {
                dead.push(at);
            }
        }
        for at in dead {
            self.spans.remove(&at);
            self.retired.push_back(at);
        }
        while self.retired.len() > self.retired_kept {
            let old = self.retired.pop_front().unwrap();
            self.entries.bind(py).del_item(old)?;
        }
        self.prune_at = (self.spans.len() * 2).max(self.virtual_align as usize);
        Ok(())
    }
}

#[pymethods]
impl NativeMMU {
    #[new]
    fn new(py: Python<'_>) -> PyResult<Self> {
        let mmu = py.import("kohakuaccel.driver.runtime.mmu")?;
        let virtual_align: u64 = mmu.getattr("VIRTUAL_ALIGN")?.extract()?;
        Ok(Self {
            entries: PyDict::new(py).unbind(),
            spans: BTreeMap::new(),
            next: BTreeMap::new(),
            retired: VecDeque::new(),
            prune_at: virtual_align as usize,
            special_bit: mmu.getattr("SPECIAL_BIT")?.extract()?,
            domain_shift: mmu.getattr("DOMAIN_SHIFT")?.extract()?,
            virtual_offset: if mutant_on!(py, "bindings.mmu/own_offset") {
                1 << 32
            } else {
                mmu.getattr("VIRTUAL_OFFSET")?.extract()?
            },
            virtual_align,
            retired_kept: mmu.getattr("RETIRED_KEPT")?.extract()?,
        })
    }

    pub fn register(
        &mut self,
        py: Python<'_>,
        physical: u64,
        extent: u64,
        arena: Py<Arena>,
    ) -> PyResult<u64> {
        self.sync(py);
        if self.spans.len() >= self.prune_at {
            self.prune(py)?;
        }
        let domain = if physical & (1u64 << self.special_bit) != 0 {
            physical >> self.domain_shift
        } else {
            0
        };
        let base = domain << self.domain_shift;
        let start = base + self.virtual_offset;
        let at = if !self.retired.is_empty() && mutant_on!(py, "bindings.mmu/reuse_handle") {
            self.retired.pop_front().unwrap()
        } else {
            *self.next.get(&domain).unwrap_or(&start)
        };
        let size = extent.div_ceil(self.virtual_align).max(1) * self.virtual_align;
        if at + size >= base + (1u64 << self.domain_shift) {
            return Err(PyMemoryError::new_err(
                "RUL.023: virtual address domain exhausted",
            ));
        }
        let generation = arena
            .borrow(py)
            .generations
            .bind(py)
            .get_item(physical)?
            .ok_or_else(|| PyKeyError::new_err("missing allocation generation"))?
            .extract()?;
        let entry = Py::new(
            py,
            Allocation {
                virtual_address: at,
                physical,
                extent,
                generation,
                arena,
            },
        )?;
        self.next.insert(domain, at + size);
        self.entries.bind(py).set_item(at, &entry)?;
        self.spans.insert(at, entry);
        Ok(at)
    }

    #[pyo3(signature=(address,nbytes=1,owner=false))]
    pub fn resolve(
        &mut self,
        py: Python<'_>,
        address: u64,
        nbytes: u64,
        owner: bool,
    ) -> PyResult<(Py<Allocation>, u64)> {
        self.sync(py);
        if let Some((&base, entry)) = self.spans.range(..=address).next_back() {
            let e = entry.borrow(py);
            if address - base < e.extent {
                if !e.live(py)?
                    || (owner && e.arena.borrow(py).released.bind(py).contains(e.physical)?)
                {
                    return Err(PyValueError::new_err(
                        "RUL.023: allocation handle has no live owner",
                    ));
                }
                if nbytes > e.extent - (address - base) {
                    return Err(PyValueError::new_err(
                        "RUL.023: access exceeds allocation extent",
                    ));
                }
                return Ok((entry.clone_ref(py), e.physical + address - base));
            }
        }
        let domain = address >> self.domain_shift;
        if address >= (domain << self.domain_shift) + self.virtual_offset
            && address < *self.next.get(&domain).unwrap_or(&0)
        {
            Err(PyValueError::new_err(
                "RUL.023: allocation handle has no live owner",
            ))
        } else {
            Err(PyKeyError::new_err(format!(
                "RUL.023: unknown allocation handle {address:#x}"
            )))
        }
    }

    pub fn retire(&mut self, py: Python<'_>, address: u64) -> PyResult<()> {
        if let Some(entry) = self.spans.get(&address) {
            if !entry.borrow(py).live(py)? {
                self.spans.remove(&address);
                self.retired.push_back(address);
            }
        }
        while self.retired.len() > self.retired_kept {
            self.entries
                .bind(py)
                .del_item(self.retired.pop_front().unwrap())?;
        }
        Ok(())
    }

    fn translations(&mut self, py: Python<'_>) -> PyResult<Vec<(u64, u64, u64)>> {
        self.sync(py);
        self.prune(py)?;
        Ok(self
            .spans
            .values()
            .map(|e| {
                let e = e.borrow(py);
                (e.virtual_address, e.physical, e.extent)
            })
            .collect())
    }

    pub fn translations_for(
        &mut self,
        py: Python<'_>,
        handles: Vec<u64>,
    ) -> PyResult<Vec<(u64, u64, u64)>> {
        let mut seen = BTreeMap::new();
        for address in handles {
            let (e, _) = self.resolve(py, address, 1, false)?;
            let e = e.borrow(py);
            seen.insert(e.virtual_address, (e.virtual_address, e.physical, e.extent));
        }
        Ok(seen.into_values().collect())
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Allocation>()?;
    m.add_class::<NativeMMU>()
}
