//! Group 2: the runtime's machine topology and address operations.
use super::{machine, mutated};
use ktpu_machine::{card::Card, Coord, Units};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::BTreeMap;

type UnitTable = BTreeMap<String, Vec<[u32; 2]>>;

fn units(table: UnitTable) -> PyResult<Units> {
    if table
        .keys()
        .any(|k| !["MG", "VC", "SR", "SIMT"].contains(&k.as_str()))
    {
        return Err(PyValueError::new_err("unknown unit kind"));
    }
    let get = |k: &str| {
        table
            .get(k)
            .into_iter()
            .flatten()
            .map(|&[x, y]| Coord(x, y))
            .collect()
    };
    Ok(Units {
        mg: get("MG"),
        vc: get("VC"),
        sr: get("SR"),
        simt: get("SIMT"),
    })
}

#[pyfunction]
#[pyo3(signature = (machine_json, nodes=None, units=None, mutant=None))]
fn machine_view<'py>(
    py: Python<'py>,
    machine_json: &str,
    nodes: Option<u32>,
    units: Option<UnitTable>,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    let m = machine(machine_json)?;
    let enumeration = units.map(self::units).transpose()?;
    let c = mutated(mutant, || Card::of(&m, nodes, enumeration))?
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let d = PyDict::new(py);
    d.set_item("board", c.board)?;
    d.set_item("name", c.name)?;
    d.set_item("fetch", (c.fetch.0, c.fetch.1))?;
    d.set_item("links", c.links)?;
    d.set_item("inst_depth", c.inst_depth)?;
    d.set_item("stage_flits", c.stage_flits)?;
    d.set_item("stage_bytes", c.stage_bytes)?;
    let params = PyDict::new(py);
    let vc = PyDict::new(py);
    vc.set_item("regs", c.regs)?;
    vc.set_item("copies", c.rf_copies)?;
    vc.set_item("sig_bits", m.vc.sig_bits)?;
    params.set_item("VC", vc)?;
    let mover = PyDict::new(py);
    mover.set_item("one_word_runs", c.one_word_runs)?;
    params.set_item("MOVER", mover)?;
    let sr = PyDict::new(py);
    if let Some(p) = c.sr.profile {
        for (k, v) in [
            ("F_LANES", p.f_lanes),
            ("H_TAPS", p.h_taps),
            ("HAS_FN", p.has_fn),
            ("HAS_MANT", p.has_mant),
            ("FIN_RCP", p.fin_rcp),
            ("F16_IEEE", p.f16_ieee),
            ("HAS_MX7", p.has_mx7),
        ] {
            sr.set_item(k, v)?;
        }
        sr.set_item("TAB", p.tab)?;
    }
    params.set_item("SR", sr)?;
    d.set_item("params", params)?;
    let meshes = PyList::empty(py);
    for mesh in c.meshes {
        let view = PyDict::new(py);
        view.set_item("index", mesh.index)?;
        view.set_item("agent", (mesh.agent.0, mesh.agent.1))?;
        view.set_item(
            "mem_ports",
            mesh.mem_ports
                .iter()
                .map(|p| (p.0, p.1))
                .collect::<Vec<_>>(),
        )?;
        let units = PyDict::new(py);
        for (kind, coords) in [
            ("MG", mesh.units.mg),
            ("VC", mesh.units.vc),
            ("SR", mesh.units.sr),
            ("SIMT", mesh.units.simt),
        ] {
            if !coords.is_empty() {
                units.set_item(kind, coords.iter().map(|p| (p.0, p.1)).collect::<Vec<_>>())?;
            }
        }
        view.set_item("units", units)?;
        meshes.append(view)?;
    }
    d.set_item("meshes", meshes)?;
    Ok(d)
}

#[pyfunction]
#[pyo3(signature = (machine_json, offset, mesh, mutant=None, *, nodes=None))]
fn stage_addr(
    machine_json: &str,
    offset: u64,
    mesh: u32,
    mutant: Option<&str>,
    nodes: Option<u32>,
) -> PyResult<u64> {
    let m = machine(machine_json)?;
    mutated(mutant, || {
        Card::of(&m, nodes, None)?.stage_addr(offset, mesh)
    })?
    .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pyfunction]
#[pyo3(signature = (machine_json, base, mesh, mutant=None, *, nodes=None))]
fn global_addr(
    machine_json: &str,
    base: u64,
    mesh: u32,
    mutant: Option<&str>,
    nodes: Option<u32>,
) -> PyResult<u64> {
    let m = machine(machine_json)?;
    mutated(mutant, || {
        Card::of(&m, nodes, None)?.global_addr(base, mesh)
    })?
    .map_err(|e| PyValueError::new_err(e.to_string()))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(machine_view, m)?)?;
    m.add_function(wrap_pyfunction!(stage_addr, m)?)?;
    m.add_function(wrap_pyfunction!(global_addr, m)?)?;
    Ok(())
}
