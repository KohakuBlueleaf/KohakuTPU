//! Synchronous eager publication: one native-owned upload slot, no Python I/O.

use super::arena::Arena;
#[cfg(unix)]
use super::exchange::posix::Io;
use super::exchange::QueueLayout;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};
use std::collections::BTreeMap;

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct DirectWire {
    queue: Py<PyAny>,
    transport: Py<PyAny>,
    memory: Py<PyAny>,
    arena: Py<Arena>,
    near: Option<(u64, u64, u64)>,
    layout: QueueLayout,
    base: u64,
    dram_offset: u64,
    geometry: [u64; 5],
    fields: BTreeMap<String, (usize, usize)>,
    generation: u64,
    queue_generation: u64,
    #[pyo3(get)]
    slot: Option<u64>,
    capacity: usize,
    uncertain: bool,
    lock: Py<PyAny>,
    transport_lock: Py<PyAny>,
    #[cfg(unix)]
    io: Option<Io>,
}

pub(crate) fn bump(dict: &Bound<'_, PyDict>, key: &str, amount: u64) -> PyResult<()> {
    let value: u64 = dict
        .get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("missing counter key {key:?}")))?
        .extract()?;
    dict.set_item(key, value + amount)
}
fn word(bytes: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(bytes[at..at + 8].try_into().unwrap())
}

#[pymethods]
impl DirectWire {
    #[new]
    #[pyo3(signature=(queue,transport,memory,base,dram_offset,geometry,fields,near=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        queue: Py<PyAny>,
        transport: Py<PyAny>,
        memory: Py<PyAny>,
        base: u64,
        dram_offset: u64,
        geometry: [u64; 5],
        fields: BTreeMap<String, (usize, usize)>,
        near: Option<(u64, u64, u64)>,
    ) -> PyResult<Self> {
        let q = queue.bind(py);
        let layout = QueueLayout::from_py(&q.getattr("layout")?)?;
        let timeout: f64 = q.getattr("timeout")?.extract()?;
        if q.getattr("version")?.extract::<u32>()? != layout.version
            || std::time::Duration::try_from_secs_f64(timeout).is_err()
            || timeout <= 0.0
            || geometry[0] == 0
            || geometry[1] == 0
            || fields
                .values()
                .any(|&(off, width)| width > layout.rec_word || off + width > layout.rec_bytes)
        {
            return Err(PyValueError::new_err(
                "native direct queue needs v4 records, positive rings and timeout",
            ));
        }
        let t = transport.bind(py);
        Ok(Self {
            arena: memory.bind(py).getattr("dram")?.extract()?,
            near,
            layout,
            generation: t.getattr("fd_generation")?.extract()?,
            queue_generation: q.getattr("_generation")?.extract()?,
            #[cfg(unix)]
            io: Some(Io::new(
                t.getattr("_c2h")?.extract()?,
                t.getattr("_h2c")?.extract()?,
            )?),
            lock: q.getattr("_native_lock")?.unbind(),
            transport_lock: t.getattr("_native_lock")?.unbind(),
            queue,
            transport,
            memory,
            base,
            dram_offset,
            geometry,
            fields,
            slot: None,
            capacity: 0,
            uncertain: false,
        })
    }

    #[pyo3(signature=(shutdown=false))]
    fn close(&mut self, py: Python<'_>, shutdown: bool) -> PyResult<()> {
        if self.uncertain && !shutdown {
            return Err(PyRuntimeError::new_err(
                "uncertain direct owner requires card shutdown",
            ));
        }
        if let Some(at) = self.slot.take() {
            self.arena.borrow_mut(py).release(py, at)?;
            self.capacity = 0;
        }
        #[cfg(unix)]
        {
            self.io.take();
        }
        Ok(())
    }

    pub fn available(&self, py: Python<'_>) -> PyResult<bool> {
        let q = self.queue.bind(py);
        // A single-tag exchange must never overtake another queue owner.
        Ok(!q.getattr("_broken")?.extract::<bool>()?
            && q.getattr("_outstanding")?.len()? == 0
            && q.getattr("_staged")?.len()? == 0)
    }

    pub fn write_host(
        &self,
        py: Python<'_>,
        card: &Bound<'_, PyAny>,
        at: u64,
        raw: &[u8],
    ) -> PyResult<()> {
        self.lock.bind(py).call_method0("acquire")?;
        let result = (|| {
            self.transport_lock.bind(py).call_method0("acquire")?;
            let result = (|| {
                if self
                    .transport
                    .bind(py)
                    .getattr("fd_generation")?
                    .extract::<u64>()?
                    != self.generation
                    || self
                        .queue
                        .bind(py)
                        .getattr("_generation")?
                        .extract::<u64>()?
                        != self.queue_generation
                    || !self.available(py)?
                {
                    return Err(PyRuntimeError::new_err(
                        "native host write owner changed or busy",
                    ));
                }
                #[cfg(unix)]
                {
                    let line = self.layout.line;
                    let io = self
                        .io
                        .as_ref()
                        .ok_or_else(|| PyRuntimeError::new_err("native direct owner closed"))?;
                    py.detach(|| io.upload(self.dram_offset + at, raw, line))?;
                }
                #[cfg(not(unix))]
                return Err(PyRuntimeError::new_err("native host write requires POSIX"));
                let log = card.getattr("log")?;
                if !log.is_none() {
                    log.cast::<PyList>()?
                        .append(("write", at, PyBytes::new(py, raw)))?;
                }
                Ok(())
            })();
            self.transport_lock.bind(py).call_method0("release")?;
            result
        })();
        self.lock.bind(py).call_method0("release")?;
        result
    }

    pub fn submit<'py>(
        &mut self,
        py: Python<'py>,
        card: &Bound<'py, PyAny>,
        raw: &[u8],
    ) -> PyResult<Bound<'py, PyDict>> {
        let lock = self.lock.clone_ref(py);
        let transport_lock = self.transport_lock.clone_ref(py);
        lock.bind(py).call_method0("acquire")?;
        let result = (|| {
            transport_lock.bind(py).call_method0("acquire")?;
            let result = self.submit_owned(py, card, raw);
            transport_lock.bind(py).call_method0("release")?;
            result
        })();
        lock.bind(py).call_method0("release")?;
        result
    }
}

impl DirectWire {
    fn submit_owned<'py>(
        &mut self,
        py: Python<'py>,
        card: &Bound<'py, PyAny>,
        raw: &[u8],
    ) -> PyResult<Bound<'py, PyDict>> {
        #[cfg(not(unix))]
        {
            let _ = (py, card, raw);
            Err(PyRuntimeError::new_err("native direct requires POSIX"))
        }
        #[cfg(unix)]
        {
            let io = self
                .io
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("native direct owner is closed"))?;
            let q = self.queue.bind(py);
            if self
                .transport
                .bind(py)
                .getattr("fd_generation")?
                .extract::<u64>()?
                != self.generation
            {
                return Err(PyRuntimeError::new_err(
                    "native direct transport generation changed; recreate owner",
                ));
            }
            if q.getattr("_generation")?.extract::<u64>()? != self.queue_generation {
                return Err(PyRuntimeError::new_err(
                    "native direct queue generation changed; recreate owner",
                ));
            }
            if !self.available(py)? {
                return Err(PyRuntimeError::new_err(
                    "native direct requires an idle healthy queue",
                ));
            }
            if raw.len() > self.capacity {
                if let Some(at) = self.slot.take() {
                    self.arena.borrow_mut(py).release(py, at)?;
                    self.capacity = 0;
                }
                let at =
                    self.arena
                        .borrow_mut(py)
                        .alloc(py, raw.len() as u64, "package", self.near)?;
                self.slot = Some(at);
                self.capacity = self
                    .arena
                    .borrow(py)
                    .live
                    .bind(py)
                    .get_item(at)?
                    .ok_or_else(|| PyRuntimeError::new_err("allocated slot missing from live map"))?
                    .extract()?;
            }
            let at = self
                .slot
                .ok_or_else(|| PyRuntimeError::new_err("missing direct upload slot"))?;
            let line = self.layout.line;
            let memory = self.memory.bind(py);
            let painted: u64 = memory.getattr("painted")?.extract()?;
            let end = at + self.capacity as u64;
            if !memory.getattr("paint")?.is_none() && end > painted {
                // New high-water DRAM must be written before widened ECC reads.
                let zeros = [0; 4096];
                let mut position = painted;
                while position < end {
                    let count = (end - position).min(4096) as usize;
                    io.upload(self.dram_offset + position, &zeros[..count], line)?;
                    position += count as u64;
                }
                memory.setattr("painted", end)?;
            }
            // Upload is not publication: a failed transfer can safely reuse this idle slot.
            py.detach(|| io.upload(self.dram_offset + at, raw, line))?;
            let tail: u64 = q.getattr("sq_tail")?.extract()?;
            let head: u64 = q.getattr("cq_head")?.extract()?;
            let stdout: u64 = q.getattr("so_rd")?.extract()?;
            let tag = q.getattr("_tag")?.extract::<u64>()? + 1;
            let timeout: f64 = q.getattr("timeout")?.extract()?;
            q.setattr("_tag", tag)?;
            let counters = q.getattr("counters")?.cast_into::<PyDict>()?;
            bump(&counters, "uploads", 1)?;
            bump(&counters, "upload_bytes", raw.len() as u64)?;
            let epoch: u64 = card.getattr("_publication_epoch")?.extract()?;
            card.setattr("_publication_epoch", epoch + 1)?;
            self.uncertain = true;
            let io = self
                .io
                .as_ref()
                .ok_or_else(|| PyRuntimeError::new_err("native direct owner is closed"))?;
            let layout = &self.layout;
            let base = self.base;
            let geometry = self.geometry;
            let entry = layout.run_entry(tag, at, raw.len() as u64, 0, 0, 0, 0);
            let out = py.detach(|| {
                io.exchange(layout, base, geometry, tail, head, stdout, &entry, timeout)
            });
            if out.published {
                q.setattr("sq_tail", tail + 1)?;
                bump(&counters, "submitted", 1)?;
            }
            q.setattr("cq_head", out.head)?;
            q.setattr("so_rd", out.stdout)?;
            bump(&counters, "polls", out.polls)?;
            if let Some(error) = out.error {
                q.setattr("_broken", true)?;
                return Err(PyOSError::new_err(error));
            }
            q.setattr("_sq_head_seen", tail + 1)?;
            let status = word(&out.completion, self.layout.cq_status) & self.layout.cq_status_mask;
            if status != self.layout.status_ok {
                q.setattr("_broken", true)?;
                let error = PyRuntimeError::new_err(format!(
                    "native direct completion status {status:#x}, tag {tag}"
                ));
                error
                    .value(py)
                    .setattr("completion_bytes", PyBytes::new(py, &out.completion))?;
                error
                    .value(py)
                    .setattr("record_bytes", PyBytes::new(py, &out.record))?;
                return Err(error);
            }
            self.uncertain = false;
            let phases = PyDict::new(py);
            for (key, &(offset, width)) in &self.fields {
                if key == "tag" || key == "so_wr" {
                    continue;
                }
                let mut bytes = [0; 8];
                bytes[..width].copy_from_slice(&out.record[offset..offset + width]);
                phases.set_item(key, u64::from_le_bytes(bytes))?;
            }
            let console: String = q.getattr("console")?.extract()?;
            q.setattr("console", console + &String::from_utf8_lossy(&out.console))?;
            let result = PyDict::new(py);
            result.set_item("cycles", word(&out.completion, self.layout.cq_cycles))?;
            result.set_item("phases", phases)?;
            let log = card.getattr("log")?;
            if !log.is_none() {
                let programs = PyDict::new(py);
                programs.set_item(
                    card.getattr("nodes")?.get_item(0)?,
                    (PyBytes::new(py, raw), PyTuple::empty(py)),
                )?;
                log.cast::<PyList>()?.append(("run", programs))?;
            }
            let runs: u64 = card.getattr("runs")?.extract()?;
            card.setattr("runs", runs + 1)?;
            Ok(result)
        }
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<DirectWire>()
}
