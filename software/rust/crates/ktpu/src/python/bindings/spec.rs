//! Tier-0 runner adapters on the same Rust stack as compilation.

use pyo3::prelude::*;

#[pyfunction]
#[pyo3(signature = (runner, input_json, machine=None, mutant=None, vector_id=None, expect_json=None))]
fn spec_run(
    runner: &str,
    input_json: &str,
    machine: Option<&str>,
    mutant: Option<&str>,
    vector_id: Option<&str>,
    expect_json: Option<&str>,
) -> PyResult<String> {
    let input = serde_json::from_str(input_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let expect = serde_json::from_str(expect_json.unwrap_or("null"))
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    crate::spec::run(runner, &input, machine, mutant, vector_id, &expect)
        .map(|out| {
            if vector_id.is_some() {
                out.to_string()
            } else {
                out["result"].to_string()
            }
        })
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(spec_run, m)?)?;
    m.add("SPEC_RUNNERS", crate::spec::runners())?;
    Ok(())
}
