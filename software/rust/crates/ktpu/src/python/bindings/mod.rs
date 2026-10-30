//! The `_rs` bindings that replace the Python compiler's library calls,
//! one module a group.
//! Each takes the machine file's text and an optional `mutant`.

/// Whether the bindings' mutant `$name` is on (`active_mutant`). Expanded at
/// the call site: a build without the `mutants` feature has `false` there
/// and no call, so a hot path pays nothing for its mutant.
macro_rules! mutant_on {
    ($py:expr, $name:literal) => {{
        #[cfg(feature = "mutants")]
        let on = super::active_mutant($py)?.as_deref() == Some($name);
        #[cfg(not(feature = "mutants"))]
        let on = {
            let _ = &$py;
            false
        };
        on
    }};
}

mod arena;
mod buffer;
mod card;
pub(crate) mod cores;
mod direct;
mod exchange;
mod graph;
mod image;
mod isa;
mod mmu;
mod package;
mod poll;
mod program;
mod queue;
mod recipe;
mod recipe_table;
mod spec;
mod submit;
mod wire_recipe;

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

pub(crate) fn active_mutant(py: Python<'_>) -> PyResult<Option<String>> {
    #[cfg(feature = "mutants")]
    {
        if let Some(m) = crate::mutant::active() {
            return Ok(Some(m.to_string()));
        }
        py.import("kohakutpu.language.dsl.kernel")?
            .getattr("_MUTANT")?
            .call_method0("get")?
            .extract()
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = py;
        Ok(None)
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
    arena::register(m)?;
    mmu::register(m)?;
    buffer::register(m)?;
    card::register(m)?;
    cores::register(m)?;
    image::register(m)?;
    graph::register(m)?;
    exchange::register(m)?;
    direct::register(m)?;
    isa::register(m)?;
    package::register(m)?;
    recipe::register(m)?;
    recipe_table::register(m)?;
    queue::register(m)?;
    submit::register(m)?;
    program::register(m)?;
    poll::register(m)?;
    spec::register(m)?;
    Ok(())
}
