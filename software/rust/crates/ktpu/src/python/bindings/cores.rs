//! The vector cores' resident programs
//! and descriptors (`ktpu_l1::lower::imem`), as the card runtime keeps them
//! across submissions, with snapshots for transactional binding and replay.

use super::{machine, mutated, Refused};
use ktpu_isa::W256;
use ktpu_l1::lower::imem::{self, Resident};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyInt};
use sha2::{Digest, Sha256};

/// A Python int as a 256-bit payload.
fn word(x: &Bound<'_, PyAny>) -> PyResult<W256> {
    let raw: Vec<u8> = x.call_method1("to_bytes", (32, "little"))?.extract()?;
    let mut b = [0u8; 32];
    b.copy_from_slice(&raw);
    Ok(W256::from_le_bytes(b))
}

fn wide(py: Python<'_>, w: W256) -> PyResult<Bound<'_, PyAny>> {
    py.get_type::<PyInt>()
        .call_method1("from_bytes", (w.to_le_bytes().to_vec(), "little"))
}

/// Every vector core's instruction memory and descriptors, made on first
/// use, sized by the machine (`vc.imem_words`, `vc.run_reach`).
#[pyclass(module = "kohakutpu.compiler._rs")]
#[derive(Clone)]
pub(crate) struct Cores {
    pub(crate) inner: imem::Cores,
}

#[pymethods]
impl Cores {
    #[new]
    fn new(machine_json: &str) -> PyResult<Cores> {
        let m = machine(machine_json)?;
        let inner = imem::Cores::new(&m).map_err(|e| Refused::new_err(e.to_string()))?;
        Ok(Cores { inner })
    }

    /// Instruction words a core holds.
    #[getter]
    fn words(&self) -> u32 {
        self.inner.words
    }

    /// Assume nothing is resident on any core (after a run, or a fault).
    fn forget(&mut self) {
        for r in self.inner.cores.values_mut() {
            r.forget();
        }
    }

    /// sha256 of every core's whole state, in coordinate order: its slots
    /// `(base, length)` in eviction order, the last base, its descriptors
    /// and IMEM words sorted.
    fn digest<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        let mut h = Sha256::new();
        for (&(x, y), r) in &self.inner.cores {
            h.update([x, y].map(u32::to_le_bytes).concat());
            let slots = r.slots();
            h.update((slots.len() as u64).to_le_bytes());
            for (base, len) in slots {
                h.update([base, len].map(u32::to_le_bytes).concat());
            }
            h.update(r.last.to_le_bytes());
            let mut desc: Vec<_> = r.desc.iter().collect();
            desc.sort();
            h.update((desc.len() as u64).to_le_bytes());
            for ((ad, fld), (v, hi)) in desc {
                h.update([*ad, *fld, *v, *hi].map(i64::to_le_bytes).concat());
            }
            let mut mem: Vec<_> = r.mem.iter().collect();
            mem.sort();
            h.update((mem.len() as u64).to_le_bytes());
            for (a, w) in mem {
                h.update(a.to_le_bytes());
                h.update(w.to_le_bytes());
            }
        }
        PyBytes::new(py, &h.finalize())
    }

    /// A copy of every core's state, for `restore`.
    fn snapshot(&self) -> Cores {
        self.clone()
    }

    /// Every core's state as `snap` holds it.
    fn restore(&mut self, snap: &Cores) {
        self.inner = snap.inner.clone();
    }

    /// `(base, length)` of each program core `coord` holds, oldest use
    /// first.
    fn resident(&self, coord: (u32, u32)) -> Vec<(u32, u32)> {
        self.inner
            .cores
            .get(&coord)
            .map(Resident::slots)
            .unwrap_or_default()
    }

    /// The core `coord`'s state: `slots`, `last`, `desc` as `[[ad, field],
    /// [value, value_hi]]` and `mem` as `[address, hex word]`, both sorted.
    fn state<'py>(&self, py: Python<'py>, coord: (u32, u32)) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        let fresh = Resident::new(self.inner.words, self.inner.run_reach);
        let r = self.inner.cores.get(&coord).unwrap_or(&fresh);
        d.set_item("slots", r.slots())?;
        d.set_item("last", r.last)?;
        let mut desc: Vec<_> = r.desc.iter().map(|(k, v)| (*k, *v)).collect();
        desc.sort();
        d.set_item("desc", desc)?;
        let mut mem: Vec<(u32, String)> =
            r.mem.iter().map(|(a, w)| (*a, format!("{w:#x}"))).collect();
        mem.sort();
        d.set_item("mem", mem)?;
        Ok(d)
    }

    /// The CU payloads of one RUN-terminated vector kernel sent to core
    /// `coord`, against what it holds (`imem::rewrite`): a resident
    /// program's IMEM words and a DESC its field already holds dropped.
    #[pyo3(signature = (coord, words, mutant=None))]
    fn rewrite<'py>(
        &mut self,
        py: Python<'py>,
        coord: (u32, u32),
        words: Vec<Bound<'py, PyAny>>,
        mutant: Option<&str>,
    ) -> PyResult<Vec<Bound<'py, PyAny>>> {
        let words: Vec<W256> = words.iter().map(word).collect::<PyResult<_>>()?;
        let inner = &mut self.inner;
        let run = move || {
            let (n, reach) = (inner.words, inner.run_reach);
            let memory = inner
                .cores
                .entry(coord)
                .or_insert_with(|| Resident::new(n, reach));
            match imem::rewrite(&inner.isa, &words, memory) {
                Err(e) if crate::mutant::active() == Some("bindings.cores/reason") => Err(
                    ktpu_l1::Error::new(e.clause, "a program longer than the IMEM"),
                ),
                got => got,
            }
        };
        let got = mutated(mutant, run)?.map_err(|e| {
            let err = Refused::new_err(e.to_string());
            let _ = err.value(py).setattr("clause", &e.clause);
            let _ = err.value(py).setattr("reason", &e.message);
            err
        })?;
        got.into_iter().map(|w| wide(py, w)).collect()
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Cores>()?;
    Ok(())
}
