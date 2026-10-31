//! An L1 parameter's buffer
//! (`ktpu_l1::layout::buffer::Buffer`) for the runtime and tools/l1.py.

use super::{fail, machine, mutated};
use ktpu_l1::layout::buffer::{Buffer, LayoutVal};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};

/// The buffer a parameter type's text names, as `fn f.l1(p: TYPE)` reads it.
fn of(type_text: &str) -> Result<Buffer, ktpu_l1::Error> {
    let text = format!("target ktpu.v9\nfn f.l1(p: {type_text})\n    halt\n");
    let module = ktpu_l1::text::read(&text, "<buffer>")?;
    let body = module.body("f", "l1")?;
    Buffer::of(&body.params[0].1)
}

/// buffer(type_text, machine_json, mutant=None) -> dict: `{bytes, space,
/// node, dtype, host_shape, host_dtype, tile}` (`bytes` the machine's).
#[pyfunction]
#[pyo3(signature = (type_text, machine_json, mutant=None))]
fn buffer<'py>(
    py: Python<'py>,
    type_text: &str,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    let m = machine(machine_json)?;
    let run = || {
        let b = of(type_text)?;
        let bytes = b.bytes(&m)?;
        Ok((b, bytes))
    };
    let (b, bytes) = mutated(mutant, run)?.map_err(|e| fail(py, e))?;
    let d = PyDict::new(py);
    d.set_item("bytes", bytes)?;
    d.set_item("space", &b.space)?;
    d.set_item("node", b.layout.get("node").and_then(LayoutVal::int))?;
    d.set_item("dtype", &b.dtype)?;
    d.set_item("host_shape", b.host_shape())?;
    d.set_item("host_dtype", b.host_dtype())?;
    d.set_item("tile", b.tile())?;
    Ok(d)
}

/// buffer_pack(type_text, host, machine_json, mutant=None) -> bytes: the
/// host value's bytes (`host_dtype`, little-endian, `host_shape` order) as
/// the buffer holds them.
#[pyfunction]
#[pyo3(signature = (type_text, host, machine_json, mutant=None))]
fn buffer_pack<'py>(
    py: Python<'py>,
    type_text: &str,
    host: &[u8],
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    let m = machine(machine_json)?;
    let got = mutated(mutant, || of(type_text)?.pack_host(host, &m))?;
    Ok(PyBytes::new(py, &got.map_err(|e| fail(py, e))?))
}

/// buffer_unpack(type_text, raw, machine_json, mutant=None) -> bytes: the
/// buffer's bytes read back as little-endian float64 values in `host_shape`
/// order.
#[pyfunction]
#[pyo3(signature = (type_text, raw, machine_json, mutant=None))]
fn buffer_unpack<'py>(
    py: Python<'py>,
    type_text: &str,
    raw: &[u8],
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyBytes>> {
    machine(machine_json)?;
    let got = mutated(mutant, || of(type_text)?.unpack(raw))?;
    let v = got.map_err(|e| fail(py, e))?;
    let bytes: Vec<u8> = v.iter().flat_map(|x| x.to_le_bytes()).collect();
    Ok(PyBytes::new(py, &bytes))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(buffer, m)?)?;
    m.add_function(wrap_pyfunction!(buffer_pack, m)?)?;
    m.add_function(wrap_pyfunction!(buffer_unpack, m)?)?;
    Ok(())
}
