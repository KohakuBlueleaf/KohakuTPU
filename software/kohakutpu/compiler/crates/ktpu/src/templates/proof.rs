//! Whole-box proof by affine binder obligations and exhaustive checker quotients.
//! No periodic extrapolation is accepted without a state-stabilisation theorem.

use super::confinement::{self, FieldClass};
use ktpu_l0::format::template::{Params, Template, ValuePatch};
use ktpu_machine::Machine;
use ktpu_verify::{Nodes, Target};
use serde::Serialize;
use std::collections::BTreeMap;

pub const NO_RESIDUES: &str = "verify.template/no-residues";

#[derive(Debug, Clone, Serialize)]
pub struct Refusal {
    pub property: String,
    pub reason: String,
    pub counterexample: Option<Params>,
}

impl Refusal {
    pub(super) fn new(property: impl Into<String>, reason: impl Into<String>) -> Self {
        Self {
            property: property.into(),
            reason: reason.into(),
            counterexample: None,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct AffineConstraint {
    pub mesh: u32,
    pub site: String,
    pub field: String,
    pub class: FieldClass,
    pub form: ValuePatch,
    pub minimum: i128,
    pub maximum: i128,
    pub discharge: &'static str,
}

/// The modulus is injective on the box, not an inferred machine-state period.
#[derive(Debug, Clone, Serialize)]
pub struct Residues {
    pub parameter: String,
    pub lo: i64,
    pub hi: i64,
    pub modulus: u128,
    pub representatives: Vec<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Certificate {
    pub version: u32,
    pub method: &'static str,
    pub checker_version: String,
    pub target: String,
    pub class: super::Class,
    pub bindings: BTreeMap<u32, Vec<u64>>,
    pub buffer_sizes: BTreeMap<u32, Vec<u64>>,
    pub eliminated: Vec<String>,
    pub residues: Vec<Residues>,
    pub affine_constraints: Vec<AffineConstraint>,
    pub checker_bindings: usize,
    pub simulation_bindings: usize,
    pub mover_arguments: Vec<super::proof_mover::Certificate>,
    pub launches_per_binding: usize,
    pub stabilisation: &'static str,
}

/// Only `prove` constructs this capability; a serialized certificate is not one.
/// Physical addresses, extents and template contents cannot change after proof.
#[derive(Debug)]
pub struct ProvenBox {
    templates: BTreeMap<u32, Template>,
    bindings: BTreeMap<u32, Vec<u64>>,
    certificate: Certificate,
}

impl ProvenBox {
    pub fn certificate(&self) -> &Certificate {
        &self.certificate
    }

    /// Any in-box binding, without a checker run or a direct-compile byte proof.
    /// This proves checker safety, not equivalence to an independently supplied graph.
    pub fn bind(&self, params: &Params) -> Result<Nodes, String> {
        bind(&self.templates, &self.bindings, params)
    }
}

fn bind(
    templates: &BTreeMap<u32, Template>,
    bindings: &BTreeMap<u32, Vec<u64>>,
    params: &Params,
) -> Result<Nodes, String> {
    templates
        .iter()
        .map(|(&mesh, t)| {
            let addresses = bindings.get(&mesh).ok_or("missing mesh bindings")?;
            t.bind(addresses, params)
                .map(|p| (mesh, p))
                .map_err(|e| e.to_string())
        })
        .collect()
}

pub(super) fn extrema(
    t: &Template,
    site: &ValuePatch,
) -> Result<(i128, i128, Params, Params), Refusal> {
    let origin: Params = t
        .table
        .parameters
        .iter()
        .map(|(n, b)| (n.clone(), b.lo))
        .collect();
    let (mut low, mut high) = (origin.clone(), origin);
    let (mut lo, mut hi) = (site.constant as i128, site.constant as i128);
    for (name, &c) in &site.coefficients {
        let b = t
            .table
            .parameters
            .get(name)
            .ok_or_else(|| Refusal::new(&site.name, format!("undeclared parameter {name}")))?;
        let (a, z) = if c >= 0 { (b.lo, b.hi) } else { (b.hi, b.lo) };
        lo = lo
            .checked_add(c as i128 * a as i128)
            .ok_or_else(|| Refusal::new(&site.name, "affine minimum overflows i128"))?;
        hi = hi
            .checked_add(c as i128 * z as i128)
            .ok_or_else(|| Refusal::new(&site.name, "affine maximum overflows i128"))?;
        low.insert(name.clone(), a);
        high.insert(name.clone(), z);
    }
    Ok((lo, hi, low, high))
}

/// Finite universal quantification with certified independence and single-owner
/// mover summaries. Unsupported or excessive domains refuse, never sample.
pub fn prove(
    templates: &BTreeMap<u32, Template>,
    bindings: &BTreeMap<u32, Vec<u64>>,
    machine: &Machine,
    cap: usize,
) -> Result<ProvenBox, Refusal> {
    let class = &templates
        .values()
        .next()
        .ok_or_else(|| Refusal::new("domain", "empty template set"))?
        .table
        .parameters;
    if cap == 0
        || class.values().any(|b| b.lo > b.hi)
        || templates.values().any(|t| &t.table.parameters != class)
    {
        return Err(Refusal::new(
            "domain",
            "zero proof budget, invalid box or mesh boxes disagree",
        ));
    }
    if templates.keys().ne(bindings.keys()) {
        return Err(Refusal::new(
            "environment",
            "one binding vector is required per mesh",
        ));
    }
    let target = Target::from_machine(machine, None).map_err(|e| Refusal::new("target", e))?;
    let origin: Params = class.iter().map(|(n, b)| (n.clone(), b.lo)).collect();
    let mut independent: BTreeMap<_, _> = class.keys().map(|n| (n.clone(), true)).collect();
    let mut affine_constraints = Vec::new();
    let mut buffer_sizes = BTreeMap::new();
    let mut mover_arguments = Vec::new();
    for (&mesh, t) in templates {
        let bound = t
            .bind(&bindings[&mesh], &origin)
            .map_err(|e| Refusal::new("binding structure", e.to_string()))?;
        let certificate = confinement::derive(t, machine)
            .map_err(|e| Refusal::new(format!("mesh {mesh} confinement"), e))?;
        if certificate.version != 2 {
            return Err(Refusal::new("confinement", "certificate version must be 2"));
        }
        let buffers = t
            .effective_buffers()
            .map_err(|e| Refusal::new("buffer extents", e.to_string()))?;
        buffer_sizes.insert(mesh, buffers.iter().map(|b| b.size).collect());
        for (i, site) in t.table.sites.iter().enumerate() {
            let kind = certificate.classes[i];
            let variable = site
                .coefficients
                .iter()
                .any(|(n, c)| *c != 0 && class[n].lo != class[n].hi);
            if variable && matches!(kind, FieldClass::Opcode | FieldClass::Sync) {
                return Err(Refusal::new(
                    &certificate.sites[i],
                    "parameter-valued opcode/synchronisation field is not a fixed-control proof",
                ));
            }
            let (minimum, maximum, low, high) = extrema(t, site)?;
            // The binder's unsigned width/offset inequalities are intervals. Running
            // both exact extrema also validates split fields and structural writes.
            for point in [low, high] {
                t.bind(&bindings[&mesh], &point).map_err(|e| Refusal {
                    property: format!("mesh {mesh} affine {}", site.name),
                    reason: e.to_string(),
                    counterexample: Some(point),
                })?;
            }
            affine_constraints.push(AffineConstraint {
                mesh, site: site.name.clone(), field: certificate.sites[i].clone(),
                class: kind, form: site.clone(), minimum, maximum,
                discharge: "checked affine extrema: unsigned field width, split carry and buffer-relative offset",
            });
        }
        let mover = super::proof_mover::derive(mesh, t, &bound, &target);
        let single_stream = mover.as_ref().is_some_and(|m| m.single_stream.is_some());
        if let Some(mover) = mover {
            mover_arguments.push(mover);
        }
        for (name, flag) in &mut independent {
            *flag &= certificate.data_only.get(name) == Some(&true) || single_stream;
            // REPEAT additions can carry out of an XB operand's field.
            if t.package.steps.iter().any(|s| s.op == ktpu_l0::Op::Repeat)
                && t.table
                    .sites
                    .iter()
                    .any(|s| s.coefficients.get(name).is_some_and(|c| *c != 0))
                && !ktpu_verify::mutant::on("verify.template/repeat-data")
            {
                *flag = false;
            }
        }
    }
    let mut points = vec![Params::new()];
    let mut residues = Vec::new();
    let mut eliminated = Vec::new();
    for (name, b) in class {
        let width = (b.hi as i128 - b.lo as i128 + 1) as u128;
        let values = if independent[name] {
            eliminated.push(name.clone());
            vec![b.lo]
        } else {
            if width > cap as u128 || points.len() as u128 * width > cap as u128 {
                return Err(Refusal::new(format!("parameter {name}"), format!(
                    "exhaustive residue product exceeds budget {cap}; no proven stabilisation or affine-hazard summary for this field")));
            }
            let representatives = if ktpu_verify::mutant::on(NO_RESIDUES) {
                if b.lo == b.hi {
                    vec![b.lo]
                } else {
                    vec![b.lo, b.hi]
                }
            } else {
                (b.lo..=b.hi).collect()
            };
            residues.push(Residues {
                parameter: name.clone(),
                lo: b.lo,
                hi: b.hi,
                modulus: width,
                representatives: representatives.clone(),
            });
            representatives
        };
        points = points
            .into_iter()
            .flat_map(|p| {
                values.iter().map(move |v| {
                    let mut p = p.clone();
                    p.insert(name.clone(), *v);
                    p
                })
            })
            .collect();
    }
    let incremental_mover = mover_arguments.len() == templates.len()
        && !ktpu_verify::mutant::on(super::proof_mover::NO_PROJECTION);
    let mut simulation_bindings = 0;
    for point in &points {
        let nodes = bind(templates, bindings, point).map_err(|e| Refusal {
            property: "binding".into(),
            reason: e,
            counterexample: Some(point.clone()),
        })?;
        let findings = if incremental_mover && simulation_bindings != 0 {
            // Only the line checker consumes changed DIM operands. All other
            // checker state and dispatch order equal the first quotient point.
            nodes
                .iter()
                .flat_map(|(&mesh, p)| ktpu_verify::lines::check(p, mesh, &target))
                .collect()
        } else {
            simulation_bindings += 1;
            let first = ktpu_verify::schedule(&nodes, &ktpu_verify::Held::new(), &target)
                .map_err(|e| Refusal::new("scheduling", e.to_string()))?;
            let second = ktpu_verify::schedule(&nodes, &first.held, &target)
                .map_err(|e| Refusal::new("rerun scheduling", e.to_string()))?;
            if first.packages != nodes || second.packages != nodes {
                return Err(Refusal {
                    property: "dispatch order".into(),
                    reason: "binding requires rescheduling; emitted package order is not proven"
                        .into(),
                    counterexample: Some(point.clone()),
                });
            }
            ktpu_verify::check_scheduled(&first, &[nodes], &target)
        };
        if !findings.is_empty() {
            return Err(Refusal {
                property: "checker".into(),
                reason: format!("{findings:?}"),
                counterexample: Some(point.clone()),
            });
        }
    }
    Ok(ProvenBox {
        templates: templates.clone(), bindings: bindings.clone(),
        certificate: Certificate {
            version: 2, method: "affine-binder/certified-mover/exhaustive-checker-quotient",
            checker_version: ktpu_verify::VERSION.into(), target: target.digest_text(),
            class: class.clone(), bindings: bindings.clone(), buffer_sizes,
            eliminated, residues, affine_constraints, checker_bindings: points.len(),
            simulation_bindings, mover_arguments,
            launches_per_binding: 2,
            stabilisation: "none assumed: injective count residues execute every count in the box; no extrapolation",
        },
    })
}
