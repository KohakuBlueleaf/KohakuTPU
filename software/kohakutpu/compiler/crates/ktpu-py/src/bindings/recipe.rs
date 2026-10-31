//! Retained native launch preparation; no package or machine content keys at launch.

use super::{
    cores::Cores,
    machine,
    package::{admission_context, admit_package},
    wire_recipe::WireRecipe,
};
use ktpu_l0::format::bind::Translation;
use ktpu_l0::format::template::{ParameterTable, Params, Template};
use ktpu_l0::word::WordOps;
use ktpu_l0::{Facts, Op, Package, Word};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};

#[pyclass(module = "kohakutpu.compiler._rs")]
#[derive(Default)]
pub(crate) struct RecipeEpoch {
    pub(crate) generation: Arc<AtomicU64>,
}

#[pymethods]
impl RecipeEpoch {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    #[getter]
    fn generation(&self) -> u64 {
        self.generation.load(Ordering::Relaxed)
    }

    fn invalidate(&self) {
        self.generation.fetch_add(1, Ordering::Relaxed);
    }
}

/// A policy proof over every use of a payload, not just its relocation annotation.
fn invariant(template: &Template, pkg: &Package, target: &ktpu_verify::Target) -> bool {
    if !target.facts.vector_mx7_allowed {
        return false;
    }
    if pkg
        .steps
        .iter()
        .any(|s| matches!(s.op, Op::Load | Op::Alu | Op::Jump | Op::Patch | Op::Repeat))
    {
        return false;
    }
    let mut changed = vec![Word::ZERO; pkg.payloads.len()];
    for r in &template.package.relocs {
        changed[r.payload as usize].set(r.bit as u32, r.width as u32, u64::MAX);
    }
    for s in template.table.sites.iter().filter(|s| s.buffer.is_some()) {
        changed[s.payload].set(s.bit, s.width, u64::MAX);
    }
    // Admission reads mover opcodes, selectors and transform IDs, not address bits.
    // Every overlapping use is checked here; arbitrary relocation fields are not trusted.
    let safe_value = |reg, mask| match reg {
        0x10 => mask & !(((1u64 << 40) - 1) << 4) == 0,
        0x38 | 0x40 => true,
        _ => mask == 0,
    };
    for s in &pkg.steps {
        match s.op {
            Op::Mover => {
                for i in s.arg..s.arg + s.count {
                    let w = pkg.payloads[i as usize];
                    let mask = changed[i as usize];
                    for half in [0, 128] {
                        if mask.limb(half) != 0 || !safe_value(w.limb(half), mask.limb(half + 64)) {
                            return false;
                        }
                    }
                }
            }
            Op::Engine => {
                for k in 0..s.count {
                    let i = (s.arg + k / 3) as usize;
                    let j = (k % 3) as u32;
                    let code = pkg.payloads[i].get(8 * j, 8);
                    if changed[i].get(8 * j, 8) != 0 {
                        return false;
                    }
                    if (8..24).contains(&(code & !64))
                        && !safe_value(((code & !64) - 8) * 8, changed[i].limb(64 * (j + 1)))
                    {
                        return false;
                    }
                }
            }
            _ => {}
        }
    }
    true
}

#[pyclass(module = "kohakutpu.compiler._rs")]
pub(crate) struct LaunchRecipe {
    template: Template,
    params: Params,
    facts: Facts,
    target: Arc<ktpu_verify::Target>,
    mesh: u32,
    admission_invariant: bool,
    admitted: bool,
    wire_enabled: bool,
    wire: Option<WireRecipe>,
    epoch: Option<(Arc<AtomicU64>, u64)>,
}

#[pymethods]
impl LaunchRecipe {
    #[new]
    #[pyo3(signature = (package, machine_json, mesh=0, template_json=None, params=None, images_json=None, scope=None, wire=false))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        package: &[u8],
        machine_json: &str,
        mesh: u32,
        template_json: Option<&str>,
        params: Option<Params>,
        images_json: Option<&str>,
        scope: Option<(PyRef<'_, RecipeEpoch>, u64)>,
        wire: bool,
    ) -> PyResult<Self> {
        let (facts, target) = admission_context(machine_json, mesh, true)?;
        let package = Package::from_bytes(package, &facts)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let table: ParameterTable = template_json
            .map(super::super::template_table)
            .transpose()
            .map_err(|e| PyValueError::new_err(e.to_string()))?
            .unwrap_or_default();
        let images: Option<serde_json::Value> =
            images_json
                .map(serde_json::from_str)
                .transpose()
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let embedded = template_json
            .map(serde_json::from_str::<serde_json::Value>)
            .transpose()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        if let Some(images) = images
            .as_ref()
            .or_else(|| embedded.as_ref().and_then(|v| v.get("images")))
            .filter(|v| !v.is_null())
        {
            let m = machine(machine_json)?;
            let isa = ktpu_isa::unit::vector::VecIsa::new(&m)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            ktpu_l1::lower::manifest::validate(&package, &isa, images)
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
        }
        let template = Template { package, table };
        Ok(Self {
            template,
            params: params.unwrap_or_default(),
            facts,
            target,
            mesh,
            // Set only after bind has validated every metadata index and field.
            admission_invariant: false,
            admitted: false,
            wire_enabled: wire,
            wire: None,
            epoch: scope.map(|(epoch, generation)| (epoch.generation.clone(), generation)),
        })
    }

    #[getter]
    fn admission_invariant(&self) -> bool {
        self.admission_invariant
    }

    #[pyo3(signature = (handles, translations=None))]
    pub fn plain<'py>(
        &self,
        py: Python<'py>,
        handles: Vec<u64>,
        translations: Option<Vec<Translation>>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let pkg = match translations {
            Some(tr) => self.template.bind_handles(&handles, &tr, &self.params),
            None => self.template.bind(&handles, &self.params),
        }
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let raw = pkg
            .to_bytes(&self.facts)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        Ok(PyBytes::new(py, &raw))
    }

    #[getter]
    fn wire_ready(&self) -> bool {
        self.wire.is_some()
    }

    #[pyo3(signature = (handles, translations=None, cores=None))]
    pub fn launch<'py>(
        &mut self,
        py: Python<'py>,
        handles: Vec<u64>,
        translations: Option<Vec<Translation>>,
        cores: Option<PyRefMut<'_, Cores>>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        if self
            .epoch
            .as_ref()
            .is_some_and(|(epoch, generation)| epoch.load(Ordering::Relaxed) != *generation)
        {
            return Err(PyValueError::new_err(
                "launch recipe invalidated by library/machine change",
            ));
        }
        if cores.is_none() {
            if let Some(wire) = &mut self.wire {
                return wire.bind(py, &handles, translations.as_deref());
            }
        }
        let pkg = match translations {
            Some(tr) => self.template.bind_handles(&handles, &tr, &self.params),
            None => self.template.bind(&handles, &self.params),
        }
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let independent = self.admitted && self.admission_invariant;
        let proof = if self.admitted {
            self.admission_invariant
        } else if mutant_on!(py, "bindings.recipe/unconditional_admission") {
            true
        } else {
            invariant(&self.template, &pkg, &self.target)
        };
        let mut next = cores.as_ref().map(|c| c.inner.clone());
        let pkg = if next.is_some() && pkg.steps.iter().any(|s| s.op == Op::Engine) {
            ktpu_l1::lower::engine::lift(&pkg, &self.facts)
                .map_err(|e| PyValueError::new_err(e.to_string()))?
        } else {
            pkg
        };
        let pkg = match next.as_mut() {
            Some(state) => ktpu_l1::lower::residency::package(&pkg, state)
                .map_err(|e| PyValueError::new_err(e.to_string()))?,
            None => pkg,
        };
        let low = ktpu_l0::engine::lower(&pkg, &self.facts)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        ktpu_l0::replay::check(&pkg, &low, &self.facts)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let low = if next.is_some() {
            ktpu_l1::lower::engine::compact(&low)
                .map_err(|e| PyValueError::new_err(e.to_string()))?
        } else {
            low
        };
        if !independent {
            admit_package(&low, self.mesh, &self.target)?;
        }
        let raw = low
            .to_bytes(&self.facts)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        if self.wire_enabled && proof && cores.is_none() && self.wire.is_none() {
            self.wire = WireRecipe::build(&self.template, &self.params, &pkg, &low, &raw);
        }
        if let (Some(mut cores), Some(state)) = (cores, next) {
            cores.inner = state;
        }
        self.admitted = true;
        self.admission_invariant = proof;
        Ok(PyBytes::new(py, &raw))
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<LaunchRecipe>()?;
    m.add_class::<RecipeEpoch>()?;
    Ok(())
}
