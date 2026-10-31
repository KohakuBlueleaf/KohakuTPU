//! Bounded exact AST/layout lookup and native execution in one Python entry.

use super::program::LaunchProgram;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyTuple};

#[pyclass(module = "kohakutpu.compiler._rs")]
struct RecipeMiss {
    key: Py<PyTuple>,
}

fn key<'py>(
    py: Python<'py>,
    ast: &Bound<'py, PyAny>,
    buffers: &Bound<'py, PyDict>,
    context: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyTuple>> {
    let mut metadata = Vec::new();
    let mut objects = Vec::new();
    let mut aliases = Vec::new();
    let mut spans = Vec::new();
    for (slot, st) in buffers.iter() {
        let pointer = st.as_ptr() as usize;
        aliases.push(
            objects
                .iter()
                .position(|&p| p == pointer)
                .unwrap_or(objects.len()),
        );
        objects.push(pointer);
        let forms = st.getattr("forms")?.cast_into::<PyDict>()?;
        let mut layouts = Vec::new();
        for (kind, form) in forms.iter() {
            let bytes: u64 = form.getattr("nbytes")?.extract()?;
            spans.push((form.getattr("addr")?.extract::<u64>()?, bytes));
            layouts.push(PyTuple::new(
                py,
                [
                    kind,
                    bytes.into_pyobject(py)?.into_any(),
                    form.getattr("owned")?,
                ],
            )?);
        }
        let valid = PyFrozenSet::new(
            py,
            st.getattr("valid")?
                .try_iter()?
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        let zeroed = PyFrozenSet::new(
            py,
            st.getattr("zeroed")?
                .try_iter()?
                .collect::<PyResult<Vec<_>>>()?,
        )?;
        metadata.push(PyTuple::new(
            py,
            [
                slot,
                st.getattr("nbytes")?,
                st.getattr("dtype")?,
                st.getattr("owned")?,
                (!st.getattr("host")?.is_none())
                    .into_pyobject(py)?
                    .to_owned()
                    .into_any(),
                st.getattr("host_f16_dtype")?,
                PyTuple::new(py, layouts)?.into_any(),
                valid.into_any(),
                zeroed.into_any(),
                st.getattr("site")?,
            ],
        )?);
    }
    let mut overlap = Vec::new();
    for (i, &(a, n)) in spans.iter().enumerate() {
        for (j, &(b, m)) in spans[..i].iter().enumerate() {
            if a < b + m && b < a + n {
                overlap.push((i, j, b as i128 - a as i128));
            }
        }
    }
    PyTuple::new(
        py,
        [
            ast.clone(),
            PyTuple::new(py, metadata)?.into_any(),
            PyTuple::new(py, aliases)?.into_any(),
            PyTuple::new(py, overlap)?.into_any(),
            context.clone(),
        ],
    )
}

#[pyclass(module = "kohakutpu.compiler._rs")]
struct RecipeTable {
    entries: Py<PyDict>,
    limit: usize,
    #[pyo3(get)]
    hits: u64,
    #[pyo3(get)]
    misses: u64,
}

#[pymethods]
impl RecipeTable {
    #[new]
    fn new(py: Python<'_>, limit: usize) -> Self {
        Self {
            entries: PyDict::new(py).unbind(),
            limit,
            hits: 0,
            misses: 0,
        }
    }

    fn clear(&mut self, py: Python<'_>) -> PyResult<()> {
        for (_, entry) in self.entries.bind(py).iter() {
            entry.call_method0("close")?;
        }
        self.entries.bind(py).clear();
        Ok(())
    }

    fn launch<'py>(
        &mut self,
        py: Python<'py>,
        dev: &Bound<'py, PyAny>,
        ast: &Bound<'py, PyAny>,
        buffers: &Bound<'py, PyDict>,
        context: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let key = key(py, ast, buffers, context)?;
        if let Some(entry) = self.entries.bind(py).get_item(&key)? {
            self.hits += 1;
            let program: Py<LaunchProgram> = entry.getattr("program")?.extract()?;
            let storages = buffers.values().iter().collect();
            let result = program.borrow(py).launch(py, dev, storages)?.into_any();
            return Ok(result);
        }
        self.misses += 1;
        Ok(Py::new(py, RecipeMiss { key: key.unbind() })?
            .into_bound(py)
            .into_any())
    }

    fn insert(
        &mut self,
        py: Python<'_>,
        miss: &RecipeMiss,
        entry: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let entries = self.entries.bind(py);
        if entries.len() == self.limit {
            let key = entries.keys().get_item(0)?;
            entries.get_item(&key)?.unwrap().call_method0("close")?;
            entries.del_item(key)?;
        }
        entries.set_item(miss.key.bind(py), entry)
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RecipeMiss>()?;
    m.add_class::<RecipeTable>()
}
