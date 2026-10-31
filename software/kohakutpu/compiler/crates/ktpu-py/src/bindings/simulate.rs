//! The value simulator of L2 on a Python memory: the device double's run.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashMap;

/// simulate_l2(text, name, binds, read, write, seed=1): `name`'s L2 of `text`
/// run with each parameter at its address, its bytes through `read(address,
/// count) -> bytes` and `write(address, bytes)`.
#[pyfunction]
#[pyo3(signature = (text, name, binds, read, write, seed=1))]
fn simulate_l2(
    text: &str,
    name: &str,
    binds: HashMap<String, i64>,
    read: Bound<'_, PyAny>,
    write: Bound<'_, PyAny>,
    seed: u64,
) -> PyResult<()> {
    let fetch = |at: i64, n: usize| {
        let raw = read.call1((at, n)).and_then(|b| b.extract::<Vec<u8>>());
        raw.map_err(|e| e.to_string())
    };
    let store = |at: i64, raw: &[u8]| {
        let done = write.call1((at, raw.to_vec()));
        done.map(|_| ()).map_err(|e| e.to_string())
    };
    ktpu_sim::l2::hosted::hosted(text, name, &binds, seed, fetch, store)
        .map_err(PyValueError::new_err)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(simulate_l2, m)?)
}
