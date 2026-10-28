//! One L1 image encoded for constant
//! arguments, and its DESC / IMEM / RUN payloads, for tools/core.py's V2
//! replay bench.

use super::{fail, machine, mutated};
use ktpu_isa::unit::vector::VecCu;
use ktpu_l1::lower::image::{reset, CoreFacts, UNKNOWN};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::collections::BTreeMap;

/// A 256-bit payload as a Python int.
fn int<'py>(py: Python<'py>, w: ktpu_isa::W256) -> PyResult<Bound<'py, PyAny>> {
    let bytes: Vec<u8> = w.0.iter().flat_map(|l| l.to_le_bytes()).collect();
    py.get_type::<pyo3::types::PyInt>()
        .call_method1("from_bytes", (bytes, "little"))
}

/// image(l1_text, name, args, machine_json, entry="unknown", mutant=None)
/// -> (words, descs): the image `name` of the L1 module `l1_text` for
/// constant `args`, entered in an unknown state or (`entry="reset"`) the
/// core's reset state; `descs` maps `(ad, field)` to its value.
#[pyfunction]
#[pyo3(signature = (l1_text, name, args, machine_json, entry="unknown", mutant=None))]
fn image<'py>(
    py: Python<'py>,
    l1_text: &str,
    name: &str,
    args: Vec<i128>,
    machine_json: &str,
    entry: &str,
    mutant: Option<&str>,
) -> PyResult<(Vec<u64>, Bound<'py, PyDict>)> {
    let m = machine(machine_json)?;
    let run = || {
        let module = ktpu_l1::text::read(l1_text, "<image>")?;
        let state = match entry {
            "reset" => reset(CoreFacts::of(&m, module.vregs).vlmax),
            _ => UNKNOWN,
        };
        ktpu_l1::lower::image::image(&module, &m, name, &args, state)
    };
    if !["unknown", "reset"].contains(&entry) {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "entry={entry:?}: \"unknown\" or \"reset\""
        )));
    }
    let (words, descs) = mutated(mutant, run)?.map_err(|e| fail(py, e))?;
    let d = PyDict::new(py);
    for ((ad, field), v) in &descs {
        d.set_item((ad, field), v)?;
    }
    Ok((words, d))
}

/// vc_flits(descs, words, pc, machine_json, mutant=None) -> list[int]: the
/// CU_INST payloads that load an image and run it: each DESC field in
/// `(ad, field)` order, each IMEM word from address 0, then RUN at `pc`.
/// A flit is `kohakuaccel.driver.device.header(T_CU_INST, txn, last)` OR
/// the payload.
#[pyfunction]
#[pyo3(signature = (descs, words, pc, machine_json, mutant=None))]
fn vc_flits<'py>(
    py: Python<'py>,
    descs: BTreeMap<(u32, u32), u64>,
    words: Vec<u64>,
    pc: u32,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let m = machine(machine_json)?;
    let run = || {
        let isa = ktpu_l1::lower::image::isa_for(&m, m.vc.regs)?;
        let mut cu: Vec<VecCu> = descs
            .iter()
            .map(|(&(ad, fld), &value)| VecCu::Desc { ad, fld, value })
            .collect();
        cu.extend(words.iter().enumerate().map(|(i, &word)| VecCu::Imem {
            addr: i as u32,
            word,
        }));
        cu.push(VecCu::Run {
            pc: match ktpu_l1::mutant::on("l1.img/flit-pc") {
                true => pc + 1,
                false => pc,
            },
        });
        cu.iter()
            .map(|c| c.encode(&isa).map_err(ktpu_l1::Error::from))
            .collect::<Result<Vec<_>, _>>()
    };
    let got = mutated(mutant, run)?.map_err(|e| fail(py, e))?;
    got.into_iter().map(|w| int(py, w)).collect()
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(image, m)?)?;
    m.add_function(wrap_pyfunction!(vc_flits, m)?)?;
    Ok(())
}
