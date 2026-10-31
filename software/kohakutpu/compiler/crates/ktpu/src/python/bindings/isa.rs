//! The ISA encoders the RTL vector
//! scripts read (`scripts/py/sru/cfg.py`, `sru/core_vectors.py`,
//! `gen_prog.py`, `v2_flow.py`): the SRU configuration map and words, a
//! cluster payload, vector core words.

use super::{machine, mutated, Refused};
use ktpu_isa::unit::cluster::ClusterIsa;
use ktpu_isa::unit::sru::config::{self, Job, Pool};
use ktpu_isa::unit::vector::core::VWord;
use ktpu_isa::unit::vector::VecIsa;
use ktpu_isa::W256;
use ktpu_machine::{Machine, Refusal};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyInt};
use std::collections::BTreeMap;

/// A refusal as `Refused`, its `clause` and `reason` (the message) set.
fn refused(py: Python<'_>, r: Refusal) -> PyErr {
    let err = Refused::new_err(r.to_string());
    let _ = err.value(py).setattr("clause", &r.clause);
    let _ = err.value(py).setattr("reason", &r.message);
    let _ = err.value(py).setattr("stage", "binding");
    err
}

/// `got`, its refusal given for another reason under the same clause under
/// the `bindings.isa/reason` mutant.
fn reasoned<T>(got: Result<T, Refusal>) -> Result<T, Refusal> {
    match got {
        Err(r) if crate::mutant::active() == Some("bindings.isa/reason") => Err(Refusal::new(
            r.clause,
            "an address that does not fit the field",
        )),
        other => other,
    }
}

/// A 256-bit payload as a Python int.
fn wide(py: Python<'_>, w: W256) -> PyResult<Bound<'_, PyAny>> {
    py.get_type::<PyInt>()
        .call_method1("from_bytes", (w.to_le_bytes().to_vec(), "little"))
}

/// sru_config(machine_json, mutant=None) -> dict: the SRU configuration map
/// (`G`, `P`, `O`: field -> [word, low bit, width]; `F`: [low bit, width];
/// the source and finalise codes) and its depths and pool timing from the
/// machine's SR facts, under `isa/sru.py`'s names.
#[pyfunction]
#[pyo3(signature = (machine_json, mutant=None))]
fn sru_config<'py>(
    py: Python<'py>,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    let m = machine(machine_json)?;
    let (map, sr) = mutated(mutant, || (config::map(), m.sr_facts()))?;
    let sr = sr.ok_or_else(|| {
        refused(
            py,
            Refusal::new("ISA.SR.001", format!("{} has no SR units", m.name)),
        )
    })?;
    let d = PyDict::new(py);
    let slots = |t: &[(&str, config::Slot)]| -> PyResult<Bound<'py, PyDict>> {
        let o = PyDict::new(py);
        for (k, (a, lo, w)) in t {
            o.set_item(*k, vec![*a, *lo, *w])?;
        }
        Ok(o)
    };
    let codes = |t: &[(&str, i64)]| -> PyResult<Bound<'py, PyDict>> {
        let o = PyDict::new(py);
        for (k, v) in t {
            o.set_item(*k, *v)?;
        }
        Ok(o)
    };
    d.set_item("G", slots(&map.g)?)?;
    d.set_item("P", slots(&map.p)?)?;
    d.set_item("O", slots(&map.o)?)?;
    let f = PyDict::new(py);
    for (k, (lo, w)) in &map.f {
        f.set_item(*k, vec![*lo, *w])?;
    }
    d.set_item("F", f)?;
    d.set_item("ASRC", codes(&map.asrc)?)?;
    d.set_item("BSRC", codes(&map.bsrc)?)?;
    d.set_item("FOP", codes(&map.fop)?)?;
    d.set_item("SRC", codes(&map.src)?)?;
    for (k, v) in [
        ("ADDR_BITS", config::ADDR_BITS),
        ("PASS_BASE", map.pass_base),
        ("PASS_STRIDE", map.pass_stride),
        ("NPASS", sr.npass),
        ("OP_BASE", map.op_base),
        ("OP_STRIDE", map.op_stride),
        ("OP_DEPTH", sr.op_depth),
        ("FIN_BASE", map.fin_base),
        ("FIN_DEPTH", sr.fin_depth),
        ("LDEP", sr.ldep),
        ("WLEAD", sr.wlead),
        ("TAPS", sr.taps),
    ] {
        d.set_item(k, v)?;
    }
    Ok(d)
}

/// sru_words(job_json, f_lanes, h_taps, machine_json, mutant=None) ->
/// list[(address, value)]: every configuration word of the job
/// (`{"g", "passes", "ops", "fin"}`, `isa/sru.py`'s `words` inputs), each
/// pass's program scheduled on a pool of `f_lanes` lanes and `h_taps`
/// result taps with the machine's pool timing.
#[pyfunction]
#[pyo3(signature = (job_json, f_lanes, h_taps, machine_json, mutant=None))]
fn sru_words(
    py: Python<'_>,
    job_json: &str,
    f_lanes: u32,
    h_taps: u32,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Vec<(u32, u32)>> {
    let m = machine(machine_json)?;
    let job: Job = serde_json::from_str(job_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("a job: {e}")))?;
    let run = || -> Result<BTreeMap<u32, u32>, Refusal> {
        let pool = Pool {
            f_lanes,
            h_taps,
            ..Pool::of(&m)?
        };
        config::words(&job, &pool)
    };
    let got = mutated(mutant, run)?.map_err(|r| refused(py, r))?;
    Ok(got.into_iter().collect())
}

/// mg_encode(op, fields, machine_json, mutant=None) -> int: the cluster
/// payload of `op` (FILL, GEMM, DRAIN or POST) with `fields` by name, an
/// `addr` split into its low and high fields; a FILL past `mg.max_fill`
/// entries is refused.
#[pyfunction]
#[pyo3(signature = (op, fields, machine_json, mutant=None))]
fn mg_encode<'py>(
    py: Python<'py>,
    op: &str,
    fields: BTreeMap<String, i64>,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Bound<'py, PyAny>> {
    let m = machine(machine_json)?;
    let run = || reasoned(encode(&m, op, &fields));
    let w = mutated(mutant, run)?.map_err(|r| refused(py, r))?;
    wide(py, w)
}

/// `mg_encode`'s payload.
fn encode(m: &Machine, op: &str, fields: &BTreeMap<String, i64>) -> Result<W256, Refusal> {
    let isa = ClusterIsa::new(m)?;
    let format = match op {
        "FILL" => &isa.fill,
        "GEMM" => &isa.gemm,
        "DRAIN" => &isa.drain,
        "POST" => &isa.post,
        other => {
            return Err(Refusal::new(
                "ISA.MG.002",
                format!("no cluster instruction {other}"),
            ))
        }
    };
    let n = fields.get("n").copied().unwrap_or(0);
    if op == "FILL" && n > isa.cfg.max_fill as i64 {
        return Err(Refusal::new(
            "ISA.MG.018",
            format!(
                "FILL of {n} entries exceeds the {}-entry streaming count; split it",
                isa.cfg.max_fill
            ),
        ));
    }
    let mut kv: Vec<(&str, i64)> = fields
        .iter()
        .filter(|(k, _)| k.as_str() != "addr")
        .map(|(k, v)| (k.as_str(), *v))
        .collect();
    if let Some(addr) = fields.get("addr") {
        kv.extend(isa.split_addr(*addr as u64)?);
    }
    format.encode(&kv)
}

/// vc_encode(words_json, machine_json, mutant=None) -> list[int]: vector
/// core words (`ktpu_isa::unit::vector::core::VWord`s as JSON) encoded for
/// the machine's core.
#[pyfunction]
#[pyo3(signature = (words_json, machine_json, mutant=None))]
fn vc_encode(
    py: Python<'_>,
    words_json: &str,
    machine_json: &str,
    mutant: Option<&str>,
) -> PyResult<Vec<u64>> {
    let m = machine(machine_json)?;
    let words: Vec<VWord> = serde_json::from_str(words_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("vector words: {e}")))?;
    let run = || -> Result<Vec<u64>, Refusal> {
        let isa = VecIsa::new(&m)?;
        words.iter().map(|w| w.encode(&isa)).collect()
    };
    mutated(mutant, run)?.map_err(|r| refused(py, r))
}

#[pyfunction]
fn sru_pack(
    py: Python<'_>,
    fields: BTreeMap<String, [u32; 3]>,
    values: BTreeMap<String, i64>,
    machine_json: &str,
) -> PyResult<BTreeMap<u32, u32>> {
    machine(machine_json)?;
    if fields.values().any(|&[_, lo, width]| {
        width == 0 || width > 32 || lo.checked_add(width).is_none_or(|n| n > 32)
    }) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "SRU fields must fit a 32-bit word",
        ));
    }
    let slots: Vec<_> = fields
        .iter()
        .map(|(k, &[a, lo, w])| (k.as_str(), (a, lo, w)))
        .collect();
    config::pack(&slots, &values).map_err(|r| refused(py, r))
}

#[pyfunction]
fn sru_schedule(
    py: Python<'_>,
    program_json: &str,
    f_lanes: u32,
    h_taps: u32,
    machine_json: &str,
) -> PyResult<String> {
    let m = machine(machine_json)?;
    let program: Vec<config::PoolOp> = serde_json::from_str(program_json)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let pool = Pool {
        f_lanes,
        h_taps,
        ..Pool::of(&m).map_err(|r| refused(py, r))?
    };
    let s = config::schedule(&program, &pool).map_err(|r| refused(py, r))?;
    serde_json::to_string(&(s.fields, s.ii, s.sigma))
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sru_pack, m)?)?;
    m.add_function(wrap_pyfunction!(sru_schedule, m)?)?;
    m.add_function(wrap_pyfunction!(sru_config, m)?)?;
    m.add_function(wrap_pyfunction!(sru_words, m)?)?;
    m.add_function(wrap_pyfunction!(mg_encode, m)?)?;
    m.add_function(wrap_pyfunction!(vc_encode, m)?)?;
    Ok(())
}
