//! First-fit arenas with deferred frees for outstanding submissions.

use pyo3::create_exception;
use pyo3::exceptions::{PyKeyError, PyMemoryError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet};

create_exception!("kohakutpu.compiler._rs", OutOfMemory, PyMemoryError);

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct Arena {
    #[pyo3(get)]
    pub lo: u64,
    #[pyo3(get)]
    pub hi: u64,
    #[pyo3(get)]
    pub align: u64,
    #[pyo3(get)]
    pub closed: bool,
    #[pyo3(get)]
    pub allocations: u64,
    #[pyo3(get)]
    pub peak: u64,
    #[pyo3(get)]
    pub high_water: u64,
    #[pyo3(get)]
    pub live: Py<PyDict>,
    #[pyo3(get)]
    pub owners: Py<PyDict>,
    #[pyo3(get)]
    pub generations: Py<PyDict>,
    #[pyo3(get)]
    pub requested: Py<PyDict>,
    #[pyo3(get)]
    pub references: Py<PyDict>,
    #[pyo3(get)]
    pub released: Py<PySet>,
    free_spans: Vec<(u64, u64)>,
    used_bytes: u64,
}

/// Where span `(at, n)` holds `size` bytes starting on a home, `near` being
/// `(stripe bytes, homes, home)`: the bytes that fit one stripe stay on it, so
/// a size past the stripe starts at one (`arena.home_start`).
fn home_start(at: u64, n: u64, size: u64, near: (u64, u64, u64)) -> Option<u64> {
    let (stripe, homes, home) = near;
    let period = stripe * homes;
    let mut base = at / period * period + home * stripe;
    if at + size.min(stripe) > base + stripe {
        base += period;
    }
    let start = at.max(base);
    (start + size <= at + n).then_some(start)
}

impl Arena {
    /// Takes `size` bytes on `near`'s home, or from the first span that holds
    /// them (`arena.place`).
    fn place(&mut self, size: u64, near: Option<(u64, u64, u64)>) -> Option<u64> {
        let on_home = near.and_then(|near| {
            self.free_spans
                .iter()
                .enumerate()
                .find_map(|(i, &(at, n))| home_start(at, n, size, near).map(|s| (i, s)))
        });
        let (i, start) = on_home.or_else(|| {
            self.free_spans
                .iter()
                .position(|&(_, n)| n >= size)
                .map(|i| (i, self.free_spans[i].0))
        })?;
        let (at, n) = self.free_spans[i];
        let mut rest = Vec::new();
        if start > at {
            rest.push((at, start - at));
        }
        if at + n > start + size {
            rest.push((start + size, at + n - start - size));
        }
        self.free_spans.splice(i..i + 1, rest);
        Some(start)
    }

    fn track(
        &mut self,
        py: Python<'_>,
        at: u64,
        size: u64,
        requested: u64,
        owner: &str,
    ) -> PyResult<u64> {
        self.live.bind(py).set_item(at, size)?;
        self.requested.bind(py).set_item(at, requested)?;
        self.owners.bind(py).set_item(at, owner)?;
        self.references.bind(py).set_item(at, 0)?;
        self.allocations += 1;
        self.generations.bind(py).set_item(at, self.allocations)?;
        self.used_bytes += size;
        self.peak = self.peak.max(self.used_bytes);
        self.high_water = self.high_water.max(at + size);
        Ok(at)
    }

    fn remove(&mut self, py: Python<'_>, at: u64) -> PyResult<()> {
        let size: u64 = self.live.bind(py).get_item(at)?.unwrap().extract()?;
        for table in [
            &self.live,
            &self.requested,
            &self.owners,
            &self.references,
            &self.generations,
        ] {
            table.bind(py).del_item(at)?;
        }
        self.released.bind(py).discard(at)?;
        self.used_bytes -= size;
        let mut i = self.free_spans.partition_point(|&(a, _)| a < at);
        self.free_spans.insert(i, (at, size));
        if i > 0 && self.free_spans[i - 1].0 + self.free_spans[i - 1].1 == at {
            let (_, n) = self.free_spans.remove(i);
            i -= 1;
            self.free_spans[i].1 += n;
        }
        if i + 1 < self.free_spans.len()
            && self.free_spans[i].0 + self.free_spans[i].1 == self.free_spans[i + 1].0
        {
            let (_, n) = self.free_spans.remove(i + 1);
            self.free_spans[i].1 += n;
        }
        Ok(())
    }
}

#[pymethods]
impl Arena {
    #[new]
    #[pyo3(signature=(lo,hi,align=256))]
    fn new(py: Python<'_>, lo: u64, hi: u64, align: u64) -> PyResult<Self> {
        if hi <= lo || align == 0 {
            return Err(PyValueError::new_err("empty or unaligned arena"));
        }
        let start = lo
            .div_ceil(align)
            .checked_mul(align)
            .filter(|&at| at <= hi)
            .ok_or_else(|| PyValueError::new_err("arena has no aligned start"))?;
        Ok(Self {
            lo,
            hi,
            align,
            closed: false,
            allocations: 0,
            peak: 0,
            high_water: start,
            live: PyDict::new(py).unbind(),
            owners: PyDict::new(py).unbind(),
            generations: PyDict::new(py).unbind(),
            requested: PyDict::new(py).unbind(),
            references: PyDict::new(py).unbind(),
            released: PySet::empty(py)?.unbind(),
            free_spans: vec![(start, hi - start)],
            used_bytes: 0,
        })
    }

    #[pyo3(signature=(nbytes,owner="runtime",near=None))]
    pub fn alloc(
        &mut self,
        py: Python<'_>,
        nbytes: u64,
        owner: &str,
        near: Option<(u64, u64, u64)>,
    ) -> PyResult<u64> {
        if self.closed {
            return Err(PyRuntimeError::new_err("arena is closed"));
        }
        let size = nbytes.div_ceil(self.align).max(1) * self.align;
        let Some(at) = self.place(size, near) else {
            return Err(OutOfMemory::new_err(format!(
                "{nbytes} bytes do not fit: {} free",
                self.free()
            )));
        };
        self.track(py, at, size, nbytes, owner)
    }

    #[pyo3(signature=(nbytes,owner="reservation"))]
    fn alloc_top(&mut self, py: Python<'_>, nbytes: u64, owner: &str) -> PyResult<u64> {
        if self.closed {
            return Err(PyRuntimeError::new_err("arena is closed"));
        }
        let size = nbytes.div_ceil(self.align).max(1) * self.align;
        for i in (0..self.free_spans.len()).rev() {
            let (at, n) = self.free_spans[i];
            if n < size {
                continue;
            }
            let top = (at + n - size) / self.align * self.align;
            if top < at {
                continue;
            }
            let mut replacement = Vec::new();
            if top > at {
                replacement.push((at, top - at));
            }
            if at + n > top + size {
                replacement.push((top + size, at + n - top - size));
            }
            self.free_spans.splice(i..i + 1, replacement);
            return self.track(py, top, size, nbytes, owner);
        }
        Err(OutOfMemory::new_err(format!(
            "{nbytes} bytes do not fit: {} free",
            self.free()
        )))
    }

    pub fn containing(&self, py: Python<'_>, address: u64) -> PyResult<u64> {
        let live = self.live.bind(py);
        if live.contains(address)? {
            return Ok(address);
        }
        for (key, value) in live.iter() {
            let at: u64 = key.extract()?;
            let n: u64 = value.extract()?;
            if address >= at && address - at < n {
                return Ok(at);
            }
        }
        Err(PyKeyError::new_err(format!(
            "{address:#x} was never allocated"
        )))
    }

    pub fn retain(&mut self, py: Python<'_>, address: u64) -> PyResult<u64> {
        let at = self.containing(py, address)?;
        if self.released.bind(py).contains(at)? {
            return Err(PyValueError::new_err("allocation has no live owner"));
        }
        let refs: u64 = self.references.bind(py).get_item(at)?.unwrap().extract()?;
        self.references.bind(py).set_item(at, refs + 1)?;
        Ok(at)
    }

    pub fn retain_pinned(&mut self, py: Python<'_>, at: u64) -> PyResult<()> {
        let refs: u64 = self
            .references
            .bind(py)
            .get_item(at)?
            .ok_or_else(|| PyValueError::new_err("allocation has no pin"))?
            .extract()?;
        if refs == 0 {
            return Err(PyValueError::new_err("allocation has no pin"));
        }
        self.references.bind(py).set_item(at, refs + 1)
    }

    fn reuse_pinned(&mut self, py: Python<'_>, at: u64) -> PyResult<()> {
        if self.closed
            || !self.released.bind(py).contains(at)?
            || self
                .references
                .bind(py)
                .get_item(at)?
                .map(|v| v.extract::<u64>())
                .transpose()?
                != Some(1)
        {
            return Err(PyValueError::new_err("not a released pinned allocation"));
        }
        self.released.bind(py).discard(at)?;
        self.allocations += 1;
        self.generations.bind(py).set_item(at, self.allocations)
    }

    pub fn release(&mut self, py: Python<'_>, at: u64) -> PyResult<()> {
        if !self.live.bind(py).contains(at)? || self.released.bind(py).contains(at)? {
            return Err(PyKeyError::new_err("allocation has no live owner"));
        }
        self.released.bind(py).add(at)?;
        let refs: u64 = self.references.bind(py).get_item(at)?.unwrap().extract()?;
        if refs == 0 {
            self.remove(py, at)?;
        }
        Ok(())
    }

    pub fn unretain(&mut self, py: Python<'_>, at: u64) -> PyResult<()> {
        if self.closed {
            return Ok(());
        }
        let refs: u64 = self
            .references
            .bind(py)
            .get_item(at)?
            .ok_or_else(|| PyValueError::new_err("allocation has no reference"))?
            .extract()?;
        if refs == 0 {
            return Err(PyValueError::new_err("allocation has no reference"));
        }
        self.references.bind(py).set_item(at, refs - 1)?;
        if refs == 1 && self.released.bind(py).contains(at)? {
            self.remove(py, at)?;
        }
        Ok(())
    }

    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        self.closed = true;
        let keys: Vec<u64> = self.live.bind(py).keys().extract()?;
        for at in keys {
            self.remove(py, at)?;
        }
        Ok(())
    }

    fn free_spans(&self) -> Vec<u64> {
        self.free_spans.iter().map(|&(_, n)| n).collect()
    }

    fn fits(&self, sizes: Vec<u64>) -> bool {
        let mut spans = self.free_spans();
        for size in sizes {
            let Some(i) = spans.iter().position(|&free| free >= size) else {
                return false;
            };
            spans[i] -= size;
        }
        true
    }

    #[getter]
    fn free_list(&self) -> Vec<(u64, u64)> {
        self.free_spans.clone()
    }
    #[getter]
    fn used(&self) -> u64 {
        self.used_bytes
    }
    #[getter]
    fn free(&self) -> u64 {
        self.free_spans.iter().map(|(_, n)| n).sum()
    }
    #[getter]
    fn largest(&self) -> u64 {
        self.free_spans.iter().map(|&(_, n)| n).max().unwrap_or(0)
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let out = PyDict::new(py);
        let owners = PyDict::new(py);
        let mut requested = 0u64;
        for (key, size) in self.live.bind(py).iter() {
            let owner = self.owners.bind(py).get_item(&key)?.unwrap();
            let before = owners
                .get_item(&owner)?
                .map(|v| v.extract::<u64>())
                .transpose()?
                .unwrap_or(0);
            owners.set_item(owner, before + size.extract::<u64>()?)?;
            requested += self
                .requested
                .bind(py)
                .get_item(&key)?
                .unwrap()
                .extract::<u64>()?;
        }
        for (key, value) in [
            ("used", self.used()),
            ("requested", requested),
            ("peak", self.peak),
            ("allocations", self.allocations),
            ("free", self.free()),
            ("largest", self.largest()),
            ("free_spans", self.free_spans.len() as u64),
            ("fragmented", self.free() - self.largest()),
        ] {
            out.set_item(key, value)?;
        }
        out.set_item("owners", owners)?;
        Ok(out)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("OutOfMemory", m.py().get_type::<OutOfMemory>())?;
    m.add_class::<Arena>()
}
