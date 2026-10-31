//! RUN entry publication with the driver's package lifetime and batching contract.

use super::exchange::QueueLayout;
use pyo3::exceptions::{PyRuntimeError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};

fn add(queue: &Bound<'_, PyAny>, name: &str, n: u64) -> PyResult<()> {
    let counts = queue.getattr("counters")?;
    let before: u64 = counts.get_item(name)?.extract()?;
    counts.set_item(name, before + n)
}

fn line<'py>(py: Python<'py>, line_bytes: usize, words: &[u64]) -> Bound<'py, PyBytes> {
    let mut raw = vec![0u8; line_bytes];
    for (i, w) in words.iter().enumerate() {
        raw[i * 8..i * 8 + 8].copy_from_slice(&w.to_le_bytes());
    }
    PyBytes::new(py, &raw)
}

pub(crate) fn flush(
    py: Python<'_>,
    queue: &Bound<'_, PyAny>,
    layout: &QueueLayout,
) -> PyResult<()> {
    let staged = queue.getattr("_staged")?.cast_into::<PyList>()?;
    if staged.is_empty() {
        return Ok(());
    }
    let entries: Vec<(u64, Bound<'_, PyBytes>)> = staged.extract()?;
    let base: u64 = queue.getattr("base")?.extract()?;
    let sq: u64 = queue.getattr("geo")?.get_item("sq")?.extract()?;
    let tail: u64 = queue.getattr("sq_tail")?.extract()?;
    let mem = queue.getattr("mem")?;
    let writes = || -> PyResult<()> {
        let mut start = entries[0].0;
        let mut previous = start;
        let mut blob = Vec::with_capacity(entries.len() * layout.sq_bytes);
        for (slot, entry) in entries {
            if slot != previous && slot != previous + 1 {
                mem.call_method1(
                    "write_block",
                    (
                        base + sq + start * layout.sq_bytes as u64,
                        PyBytes::new(py, &blob),
                    ),
                )?;
                start = slot;
                blob.clear();
            }
            blob.extend_from_slice(entry.as_bytes());
            previous = slot;
        }
        mem.call_method1(
            "write_block",
            (
                base + sq + start * layout.sq_bytes as u64,
                PyBytes::new(py, &blob),
            ),
        )?;
        mem.call_method1(
            "write_block",
            (base + layout.sq_tail, line(py, layout.line, &[tail])),
        )?;
        Ok(())
    };
    if let Err(error) = writes() {
        queue.setattr("_broken", true)?;
        return Err(error);
    }
    staged.call_method0("clear")?;
    Ok(())
}

fn run_with_layout(
    py: Python<'_>,
    queue: &Bound<'_, PyAny>,
    package: &Bound<'_, PyAny>,
    bindings: Vec<u64>,
    publishing: Option<&Bound<'_, PyAny>>,
    layout: &QueueLayout,
) -> PyResult<u64> {
    if queue.getattr("_broken")?.extract::<bool>()? {
        return Err(PyRuntimeError::new_err(
            "queue publication failed; reset before submitting",
        ));
    }
    let base: u64 = queue.getattr("base")?.extract()?;
    let sq_n: u64 = queue.getattr("sq_n")?.extract()?;
    let cached: bool = queue.getattr("head_cache")?.extract()?;
    let tail: u64 = queue.getattr("sq_tail")?.extract()?;
    let mem = queue.getattr("mem")?;
    let mut head: u64 = queue.getattr("_sq_head_seen")?.extract()?;
    if !cached || tail - head >= sq_n {
        head = mem
            .call_method1("read64", (base + layout.sq_head,))?
            .extract()?;
        queue.setattr("_sq_head_seen", head)?;
    }
    if tail - head >= sq_n {
        let start = queue.call_method0("_start")?;
        let timeout: f64 = queue.getattr("timeout")?.extract()?;
        let spin: f64 = queue.getattr("spin")?.extract()?;
        while !queue.call_method0("_sq_room")?.extract::<bool>()? {
            queue.call_method0("poll")?;
            let waited: f64 = queue
                .call_method1("_waited", (&start, "a full SQ"))?
                .extract()?;
            if waited > timeout {
                return Err(PyTimeoutError::new_err("SQ stayed full"));
            }
            if waited >= spin {
                queue.call_method0("_idle")?;
            }
        }
    }
    let slot = tail % sq_n;
    let tag = queue.getattr("_tag")?.extract::<u64>()? + 1;
    queue.setattr("_tag", tag)?;
    let entry = (|| -> PyResult<Bound<'_, PyBytes>> {
        let (at, nbytes, key) = if let Ok(raw) = package.cast::<PyBytes>() {
            (
                queue.call_method1("upload", (raw,))?.extract::<u64>()?,
                raw.as_bytes().len() as u64,
                raw.clone().into_any(),
            )
        } else {
            let generation: u64 = queue.getattr("_generation")?.extract()?;
            let key = package.getattr("key")?;
            let pins = queue.getattr("_package_pins")?.cast_into::<PyDict>()?;
            if !package.getattr("queue")?.is(queue)
                || package.getattr("closed")?.extract::<bool>()?
                || package.getattr("generation")?.extract::<u64>()? != generation
                || pins
                    .get_item(&key)?
                    .map(|p| p.extract::<u64>())
                    .transpose()?
                    .unwrap_or(0)
                    == 0
            {
                return Err(PyValueError::new_err(
                    "prepared package is closed or belongs to another queue epoch",
                ));
            }
            (
                package.getattr("address")?.extract()?,
                package.getattr("nbytes")?.extract()?,
                key,
            )
        };
        queue.getattr("_package_tags")?.set_item(tag, key)?;
        let nbind = bindings.len() as u64;
        let max_bindings: u64 = queue.getattr("binding_limit")?.extract()?;
        if nbind > max_bindings {
            return Err(PyValueError::new_err("too many bindings"));
        }
        let mut bind = 0;
        if nbind != 0 {
            let mut blob = vec![0u8; (bindings.len() * 8).div_ceil(layout.line) * layout.line];
            for (i, value) in bindings.iter().enumerate() {
                blob[i * 8..i * 8 + 8].copy_from_slice(&value.to_le_bytes());
            }
            bind = base + queue.getattr("bind_off")?.extract::<u64>()? + slot * max_bindings * 8;
            mem.call_method1("write_block", (bind, PyBytes::new(py, &blob)))?;
        }
        Ok(line(
            py,
            layout.sq_bytes,
            &layout.run_entry(tag, at, nbytes, bind, nbind, 0, 0),
        ))
    })();
    let entry = match entry {
        Ok(e) => e,
        Err(e) => {
            queue.call_method1("_retire_package", (tag,))?;
            return Err(e);
        }
    };
    queue.getattr("_outstanding")?.call_method1("add", (tag,))?;
    if let Some(publish) = publishing {
        publish.call0()?;
    }
    let staged = queue.getattr("_staged")?.cast_into::<PyList>()?;
    staged.append((slot, entry))?;
    queue.setattr("sq_tail", tail + 1)?;
    if staged.len() >= queue.getattr("batch_size")?.extract::<usize>()? {
        flush(py, queue, layout)?;
    }
    add(queue, "submitted", 1)?;
    Ok(tag)
}

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct QueueRun {
    #[pyo3(get)]
    layout: QueueLayout,
}

#[pymethods]
impl QueueRun {
    #[new]
    fn new(layout: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self {
            layout: QueueLayout::from_py(layout)?,
        })
    }

    #[pyo3(signature=(queue,package,bindings,publishing=None))]
    fn __call__(
        &self,
        py: Python<'_>,
        queue: &Bound<'_, PyAny>,
        package: &Bound<'_, PyAny>,
        bindings: Vec<u64>,
        publishing: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<u64> {
        run_owned(py, queue, package, bindings, publishing, &self.layout)
    }
}

/// A failed publication keeps its tag outstanding: the entry may be visible.
fn run_owned(
    py: Python<'_>,
    queue: &Bound<'_, PyAny>,
    package: &Bound<'_, PyAny>,
    bindings: Vec<u64>,
    publishing: Option<&Bound<'_, PyAny>>,
    layout: &QueueLayout,
) -> PyResult<u64> {
    let got = run_with_layout(py, queue, package, bindings, publishing, layout);
    if got.is_err() && mutant_on!(py, "bindings.queue/retires_failed") {
        queue.getattr("_outstanding")?.call_method0("clear")?;
    }
    got
}

#[pyfunction]
#[pyo3(signature=(queue,package,bindings,layout,publishing=None))]
pub(crate) fn queue_run(
    py: Python<'_>,
    queue: &Bound<'_, PyAny>,
    package: &Bound<'_, PyAny>,
    bindings: Vec<u64>,
    layout: &QueueLayout,
    publishing: Option<&Bound<'_, PyAny>>,
) -> PyResult<u64> {
    run_owned(py, queue, package, bindings, publishing, layout)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<QueueRun>()?;
    m.add_function(wrap_pyfunction!(queue_run, m)?)
}
