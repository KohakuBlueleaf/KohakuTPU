//! L2 and card-package value execution on Python memory for the device double.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::{BTreeMap, HashMap};
use std::rc::Rc;

use ktpu_isa::{Isa, UnitKind};
use ktpu_l1::build::{programs, Builder, Opts};
use ktpu_l1::lower::imem::Cores;
use ktpu_sim::l0;

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

/// Build and run L1 card packages on rebased buffers, preserving parameter aliases.
#[pyfunction]
#[pyo3(signature = (text, name, binds, read, write, machine_json, seed=1))]
fn simulate_l1(
    text: &str,
    name: &str,
    binds: HashMap<String, i64>,
    read: Bound<'_, PyAny>,
    write: Bound<'_, PyAny>,
    machine_json: &str,
    seed: u64,
) -> PyResult<()> {
    let card = super::super::machine(machine_json)?;
    let module =
        ktpu_l1::text::read(text, name).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let body = module
        .body(name, "l1")
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let mut ranges = Vec::new();
    for (port, ty) in &body.params {
        let at = *binds
            .get(port)
            .ok_or_else(|| PyValueError::new_err(format!("missing binding {port}")))?;
        let buffer = ktpu_sim::l2::view::Buf::of(ty, at).map_err(PyValueError::new_err)?;
        let end = at
            .checked_add(buffer.bytes())
            .ok_or_else(|| PyValueError::new_err("buffer address overflow"))?;
        if at < 0 || end < at {
            return Err(PyValueError::new_err("invalid buffer extent"));
        }
        ranges.push((at, end));
    }
    ranges.sort_unstable();
    let mut spans: Vec<(i64, i64, i64)> = Vec::new();
    for (lo, hi) in ranges {
        if let Some(last) = spans.last_mut() {
            if lo <= last.1 {
                last.1 = last.1.max(hi);
                continue;
            }
        }
        spans.push((lo, hi, 0));
    }
    let mut size = 4096i64;
    for (lo, hi, base) in &mut spans {
        *base = size + *lo % 4096;
        size = base
            .checked_add(*hi - *lo)
            .and_then(|n| n.checked_add(4095))
            .map(|n| n / 4096 * 4096)
            .ok_or_else(|| PyValueError::new_err("host buffer size overflow"))?;
    }
    let mut memory = ktpu_sim::Dram::new(
        usize::try_from(size).map_err(|e| PyValueError::new_err(e.to_string()))?,
    );
    let mut local = BTreeMap::new();
    for (lo, hi, base) in &spans {
        let raw = read.call1((*lo, hi - lo))?.extract::<Vec<u8>>()?;
        if raw.len() as i64 != hi - lo {
            return Err(PyValueError::new_err("short host memory read"));
        }
        memory.write(*base, &raw).map_err(PyValueError::new_err)?;
        for (port, at) in &binds {
            if lo <= at && at < hi {
                local.insert(port.clone(), i128::from(base + at - lo));
            }
        }
    }
    let node_programs = programs(&module, name, &local, &card, Some(card.nodes()), None)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let builder = Builder::new(&card).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let mut cores = Cores::new(&card).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let coords = |kind| {
        let mut coords: Vec<_> = card
            .units(kind)
            .iter()
            .map(|u| (u.x() as u64, u.y() as u64))
            .collect();
        coords.sort();
        coords
    };
    // ENGINE packages execute in node-program order on one mesh and shared DRAM.
    let mut machine = ktpu_sim::machine::Machine::new(
        memory,
        card.units(UnitKind::Vc).len(),
        card.units(UnitKind::Mg).len(),
        card.vc.regs as usize,
        seed,
    );
    machine.isa = Some(Rc::new(
        Isa::reference(&card).map_err(|e| PyValueError::new_err(e.to_string()))?,
    ));
    for np in &node_programs {
        let facts = ktpu_l0::Facts::of_mesh(&*card, np.mesh)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let opts = Opts {
            fetch: Some((card.die.fetch_port.x(), card.die.fetch_port.y())),
            cores: Some(&mut cores),
            relocate: false,
        };
        let (package, _) = builder
            .package(np, opts)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let units = l0::Units {
            vc: coords(UnitKind::Vc),
            mg: coords(UnitKind::Mg),
            src_mover: facts.src_mover(),
            counter_bits: facts.counter_bits,
        };
        l0::run(&package, &units, &mut machine).map_err(PyValueError::new_err)?;
    }
    for (lo, hi, base) in spans {
        let raw = machine
            .mem
            .read(base, (hi - lo) as usize)
            .map_err(PyValueError::new_err)?;
        write.call1((lo, raw.to_vec()))?;
    }
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(simulate_l1, m)?)?;
    m.add_function(wrap_pyfunction!(simulate_l2, m)?)
}
