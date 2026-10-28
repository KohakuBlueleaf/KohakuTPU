//! The `_rs` bindings that replace the Python compiler's library calls,
//! one module a group.
//! Each takes the machine file's text and an optional `mutant`.

mod buffer;
mod card;
pub(crate) mod cores;
mod image;
mod isa;
mod package;
mod spec;

use super::{machine, Refused};
use pyo3::prelude::*;

/// `f()` with the spec-vector mutant `mutant` on (`ktpu::mutant::with`),
/// or as is; a build without the mutant raises `ValueError`.
fn mutated<T: Send>(mutant: Option<&str>, f: impl FnOnce() -> T + Send) -> PyResult<T> {
    match mutant {
        Some(n) => crate::mutant::with(n, f).map_err(pyo3::exceptions::PyValueError::new_err),
        None => Ok(f()),
    }
}

/// A ktpu-l1 refusal as `Refused`, its `clause` set.
fn fail(py: Python<'_>, e: ktpu_l1::Error) -> PyErr {
    let err = Refused::new_err(e.to_string());
    let _ = err.value(py).setattr("clause", &e.clause);
    let _ = err.value(py).setattr("stage", "binding");
    err
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    buffer::register(m)?;
    card::register(m)?;
    cores::register(m)?;
    image::register(m)?;
    isa::register(m)?;
    package::register(m)?;
    spec::register(m)?;
    Ok(())
}
