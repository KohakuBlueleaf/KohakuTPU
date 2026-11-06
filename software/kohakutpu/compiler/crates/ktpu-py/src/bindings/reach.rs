//! Offline unit-read reach over a relocated compiled package and memory image.

use std::rc::Rc;

use ktpu_isa::{Isa, UnitKind};
use ktpu_l0::{Facts, Package};
use ktpu_sim::l0::{self, Units};
use ktpu_sim::machine::Machine;
use ktpu_sim::Dram;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::json;

/// The memory image starts at address zero; bindings use that address space.
/// Unsupported execution is reported as incomplete, never as zero reach.
#[pyfunction]
#[pyo3(signature = (package, bindings, machine_json, memory, mesh=0))]
fn f014_reach(
    package: &[u8],
    bindings: Vec<u64>,
    machine_json: &str,
    memory: &[u8],
    mesh: u32,
) -> PyResult<String> {
    let card = super::machine(machine_json)?;
    let facts = Facts::of_mesh(&*card, mesh).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let pkg = Package::from_bytes(package, &facts)
        .and_then(|p| p.bind(&bindings))
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let coords = |kind| {
        let mut out: Vec<_> = card
            .units(kind)
            .iter()
            .map(|u| (u.x() as u64, u.y() as u64))
            .collect();
        out.sort_unstable();
        out
    };
    let units = Units {
        vc: coords(UnitKind::Vc),
        mg: coords(UnitKind::Mg),
        src_mover: facts.src_mover(),
        counter_bits: facts.counter_bits,
    };
    let mut mem = Dram::new(0);
    mem.bytes = memory.to_vec();
    mem.reach = Some(Default::default());
    let mut machine = Machine::new(
        mem,
        units.vc.len(),
        units.mg.len(),
        card.vc.regs as usize,
        1,
    );
    machine.isa = Some(Rc::new(
        Isa::reference(&card).map_err(|e| PyValueError::new_err(e.to_string()))?,
    ));
    let stopped = l0::run(&pkg, &units, &mut machine).err();
    let reach = machine
        .mem
        .reach
        .take()
        .ok_or_else(|| PyValueError::new_err("missing read census"))?;
    let reads: Vec<_> = reach
        .reads
        .iter()
        .map(|r| {
            json!({
            "class": r.class, "unit": r.unit, "ordinal": r.ordinal,
            "address": r.address, "bytes": r.bytes, "single_beats": r.single_beats,
            })
        })
        .collect();
    let unresolved: Vec<_> = reach
        .unresolved
        .iter()
        .map(|r| {
            json!({
                "class": r.class, "unit": r.unit, "ordinal": r.ordinal,
                "address": r.address, "bytes": r.bytes,
                "reason": "mover issue trace and DRAM_AR_MAX required",
            })
        })
        .collect();
    Ok(json!({
        "mesh": mesh,
        "execution_complete": stopped.is_none(),
        "complete": stopped.is_none() && reach.unresolved.is_empty(),
        "stopped": stopped,
        "counts": reach.counts,
        "reads": reads,
        "unresolved": unresolved,
        "count_columns": ["requests", "affected_requests", "single_beat_pieces"],
        "scope": "executed unit reads; CPU and instruction fetch excluded",
        "mover_coalescing": "maximal source runs only; no guessed MAG single-beat counts",
    })
    .to_string())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(f014_reach, m)?)
}
