//! Offline differential derivation. Runtime binding is the L0 T1 binder.

pub mod compiler;
pub mod configuration;
pub mod confinement;
mod linear;
pub mod policy;
pub mod proof;
pub mod proof_mover;
pub mod values;
pub mod verification;

use std::collections::BTreeMap;

use ktpu_l0::format::template::{
    step_fields, step_value, Bounds, ParameterTable, Params, Template, ValuePatch,
};
use ktpu_l0::{Facts, Package};

pub type Class = BTreeMap<String, Bounds>;

/// A compiled point; launch labels are preserved in every diagnostic.
#[derive(Clone, Debug)]
pub struct Sample {
    pub label: String,
    pub params: Params,
    pub package: Package,
}

/// Exact integer coefficients. The supplied points must include the lower
/// corner and its unit-axis neighbours; all remaining points are witnesses.
pub fn fit(class: &Class, points: &[(Params, i128)]) -> Result<(i64, Params), String> {
    if class.values().any(|b| b.lo > b.hi) {
        return Err("invalid class bounds".into());
    }
    for (p, _) in points {
        if p.len() != class.len()
            || class
                .iter()
                .any(|(n, b)| !p.get(n).is_some_and(|v| (b.lo..=b.hi).contains(v)))
        {
            return Err(format!("parameters outside class: {p:?}"));
        }
    }
    let origin: Params = class.iter().map(|(n, b)| (n.clone(), b.lo)).collect();
    let value = |p: &Params| {
        points
            .iter()
            .find(|(q, _)| q == p)
            .map(|(_, v)| *v)
            .ok_or_else(|| format!("missing basis point {p:?}"))
    };
    let mut constant = value(&origin)?;
    let mut coefficients = Params::new();
    for (n, b) in class {
        if b.lo == b.hi {
            continue;
        }
        let mut axis = origin.clone();
        axis.insert(n.clone(), b.lo + 1);
        let c = value(&axis)?
            .checked_sub(value(&origin)?)
            .ok_or("affine overflow")?;
        constant = constant
            .checked_sub(c.checked_mul(b.lo as i128).ok_or("affine overflow")?)
            .ok_or("affine overflow")?;
        if c != 0 {
            coefficients.insert(
                n.clone(),
                i64::try_from(c).map_err(|_| "coefficient exceeds i64")?,
            );
        }
    }
    for (p, actual) in points {
        let mut expected = constant;
        for (n, c) in &coefficients {
            expected = expected
                .checked_add((*c as i128) * p[n] as i128)
                .ok_or("affine overflow")?;
        }
        if expected != *actual {
            return Err(format!(
                "non-affine at {p:?}: fitted {expected}, direct {actual}"
            ));
        }
    }
    Ok((
        i64::try_from(constant).map_err(|_| "constant exceeds i64")?,
        coefficients,
    ))
}

/// Corners, unit-axis basis, adjacent values and deterministic interior points.
/// A cap refuses an excessive box rather than silently dropping corners.
pub fn points(class: &Class, cap: usize) -> Result<Vec<Params>, String> {
    if class.values().any(|b| b.lo > b.hi) {
        return Err("invalid class bounds".into());
    }
    let origin: Params = class.iter().map(|(n, b)| (n.clone(), b.lo)).collect();
    let mut out = vec![origin.clone()];
    for (n, b) in class {
        if b.lo == b.hi {
            continue;
        }
        if out.len() > cap / 2 {
            return Err("class corner budget exceeded".into());
        }
        let more: Vec<_> = out
            .iter()
            .map(|p| {
                let mut p = p.clone();
                p.insert(n.clone(), b.hi);
                p
            })
            .collect();
        out.extend(more);
    }
    for (n, b) in class {
        if b.lo == b.hi {
            continue;
        }
        for v in [b.lo + 1, b.hi - 1] {
            let mut p = origin.clone();
            p.insert(n.clone(), v);
            out.push(p);
        }
    }
    for fraction in [1i128, 2, 3] {
        out.push(
            class
                .iter()
                .map(|(n, b)| {
                    (
                        n.clone(),
                        (b.lo as i128 + (b.hi as i128 - b.lo as i128) * fraction / 4) as i64,
                    )
                })
                .collect(),
        );
    }
    out.sort();
    out.dedup();
    if out.len() > cap {
        return Err("class sample budget exceeded".into());
    }
    Ok(out)
}

/// First structural difference, excluding whitelisted step value fields.
/// Buffer extents may vary, but names, kinds and the relocation topology cannot.
pub fn structure(a: &Package, b: &Package) -> Result<(), String> {
    macro_rules! same {
        ($field:ident) => {
            if a.$field != b.$field {
                return Err(stringify!($field).into());
            }
        };
    }
    same!(units);
    if a.steps.len() != b.steps.len() {
        return Err("steps".into());
    }
    for (x, y) in a.steps.iter().zip(&b.steps) {
        let mut x = *x;
        for &(bit, width) in step_fields(x.op) {
            if bit == 32 {
                x.count = y.count;
            } else {
                let mask = (if width == 64 {
                    u64::MAX
                } else {
                    (1u64 << width) - 1
                }) << (bit - 64);
                x.arg = (x.arg & !mask) | (y.arg & mask);
            }
        }
        if x != *y {
            return Err("steps".into());
        }
    }
    same!(signature);
    same!(ack_reserve);
    same!(checksum);
    if a.payloads.len() != b.payloads.len() {
        return Err("payloads.len".into());
    }
    if a.buffers.len() != b.buffers.len() {
        return Err("buffers.len".into());
    }
    if a.relocs.len() != b.relocs.len() {
        return Err("relocs.len".into());
    }
    same!(buffer_names);
    for (i, (x, y)) in a.buffers.iter().zip(&b.buffers).enumerate() {
        if x.name != y.name || x.kind != y.kind || x.mesh != y.mesh || x.default != y.default {
            return Err(format!("buffers[{i}] topology/default"));
        }
    }
    for (i, (x, y)) in a.relocs.iter().zip(&b.relocs).enumerate() {
        let mut x = *x;
        x.addend = y.addend;
        if x != *y {
            return Err(format!("relocs[{i}] topology"));
        }
    }
    Ok(())
}

fn site(
    class: &Class,
    samples: &[Sample],
    name: String,
    values: Vec<i128>,
    mapped: bool,
) -> Result<ValuePatch, String> {
    let rows: Vec<_> = samples
        .iter()
        .zip(values)
        .map(|(s, v)| (s.params.clone(), v))
        .collect();
    let (constant, coefficients) = if mapped {
        values::fit(&rows)
    } else {
        fit(class, &rows)
    }
    .map_err(|e| format!("{name}: {e}"))?;
    Ok(ValuePatch {
        name,
        payload: 0,
        step: None,
        bit: 0,
        width: 32,
        shift: 0,
        constant,
        coefficients,
        buffer: None,
    })
}

/// Derive from compiled samples. Required witnesses are checked even when
/// every payload is constant, so an underdetermined class never passes.
pub fn derive(class: Class, samples: &[Sample]) -> Result<Template, String> {
    derive_with_values(class, samples, &values::Recipes::new())
}

/// Verify shape-domain witnesses before mapping them into launch values.
pub fn derive_with_values(
    class: Class,
    samples: &[Sample],
    recipes: &values::Recipes,
) -> Result<Template, String> {
    derive_internal(class, samples, recipes, true)
}

pub fn derive_confined(
    class: Class,
    samples: &[Sample],
    recipes: &values::Recipes,
    machine: &ktpu_machine::Machine,
) -> Result<(Template, confinement::Certificate), String> {
    let t = derive_internal(class.clone(), samples, recipes, false)?;
    let certificate = confinement::derive(&t, machine)?;
    let mut reduced = class.clone();
    for (name, bounds) in &mut reduced {
        let hazardous = certificate.data_only.iter().any(|(value, data)| {
            !*data
                && (value == name
                    || recipes
                        .get(value)
                        .is_some_and(|e| e.dependencies().contains(name)))
        });
        if !hazardous {
            bounds.hi = bounds.lo;
        }
    }
    for point in points(&reduced, 4096)? {
        if !samples.iter().any(|s| s.params == point) {
            return Err(format!("missing hazard witness {point:?}"));
        }
    }
    Ok((t, certificate))
}

fn derive_internal(
    mut class: Class,
    samples: &[Sample],
    recipes: &values::Recipes,
    full: bool,
) -> Result<Template, String> {
    samples.first().ok_or("empty sample set")?;
    for point in if full {
        points(&class, 4096)?
    } else {
        Vec::new()
    } {
        if !samples.iter().any(|s| s.params == point) {
            return Err(format!("missing verification point {point:?}"));
        }
    }
    fit(
        &class,
        &samples
            .iter()
            .map(|s| (s.params.clone(), 0))
            .collect::<Vec<_>>(),
    )?;
    let mapped = !recipes.is_empty();
    let expanded = samples
        .iter()
        .map(|s| {
            Ok(Sample {
                label: s.label.clone(),
                package: s.package.clone(),
                params: values::bind(recipes, &s.params)?,
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    let samples = expanded.as_slice();
    for name in recipes.keys() {
        class.insert(
            name.clone(),
            Bounds {
                lo: samples.iter().map(|s| s.params[name]).min().unwrap(),
                hi: samples.iter().map(|s| s.params[name]).max().unwrap(),
            },
        );
    }
    let first = &samples[0];
    for s in samples {
        structure(&first.package, &s.package)
            .map_err(|e| format!("structure: {}: {e}", s.label))?;
    }
    let mut package = first.package.clone();
    // The template describes a class allocation; the direct bound package
    // discards buffer metadata, and each launch proof checks its own extents.
    for (i, b) in package.buffers.iter_mut().enumerate() {
        b.size = samples
            .iter()
            .map(|s| s.package.buffers[i].size)
            .max()
            .unwrap();
    }
    let mut sites = Vec::new();
    for (index, step) in package.steps.iter().enumerate() {
        for &(bit, width) in step_fields(step.op) {
            let values: Vec<_> = samples
                .iter()
                .map(|s| step_value(&s.package.steps[index], bit, width) as i128)
                .collect();
            if values.iter().all(|v| *v == values[0]) {
                continue;
            }
            let mut p = site(
                &class,
                samples,
                format!("steps[{index}][{bit}+{width}]"),
                values,
                mapped,
            )?;
            p.step = Some(index);
            p.bit = bit;
            p.width = width;
            sites.push(p);
        }
    }
    let mut retained = Vec::new();
    for (i, r) in first.package.relocs.iter().enumerate() {
        if samples
            .iter()
            .all(|s| s.package.relocs[i].addend == r.addend)
        {
            retained.push(*r);
        } else {
            let mut p = site(
                &class,
                samples,
                format!("relocs[{i}].addend"),
                samples.iter().map(|s| s.package.relocs[i].addend).collect(),
                mapped,
            )?;
            p.payload = r.payload as usize;
            p.bit = r.bit as u32;
            p.width = r.width as u32;
            p.shift = r.shift as u32;
            p.buffer = Some(r.buffer as usize);
            sites.push(p);
        }
    }
    package.relocs = retained;
    for word in 0..package.payloads.len() {
        // Field boundaries include relocation edges and 32-bit word lanes.
        let mut edges = vec![0u32, 32, 64, 96, 128, 160, 192, 224, 256];
        if first.package.steps.iter().any(|s| {
            s.op == ktpu_l0::Op::Mover && word as u64 >= s.arg && (word as u64) < s.arg + s.count
        }) {
            for base in [0u32, 128] {
                if samples
                    .iter()
                    .all(|s| s.package.payloads[word].get(base, 64) == ktpu_l0::mover::R_DIM)
                {
                    edges.retain(|b| *b != base + 96);
                    edges.extend([base + 68, base + 84, base + 116]);
                }
            }
        }
        for r in &first.package.relocs {
            if r.payload as usize == word {
                edges.extend([r.bit as u32, (r.bit + r.width) as u32]);
            }
        }
        edges.sort();
        edges.dedup();
        for w in edges.windows(2) {
            let (bit, width) = (w[0], w[1] - w[0]);
            if first.package.relocs.iter().any(|r| {
                r.payload as usize == word
                    && (bit as u64) < r.bit + r.width
                    && r.bit < (bit + width) as u64
            }) {
                continue;
            }
            let values: Vec<_> = samples
                .iter()
                .map(|s| s.package.payloads[word].get(bit, width) as i128)
                .collect();
            if values.iter().all(|v| *v == values[0]) {
                continue;
            }
            let mut p = site(
                &class,
                samples,
                format!("payloads[{word}][{bit}+{width}]"),
                values,
                mapped,
            )?;
            p.payload = word;
            p.bit = bit;
            p.width = width;
            sites.push(p);
        }
    }
    Ok(Template {
        package,
        table: ParameterTable {
            environment: None,
            buffer_sizes: None,
            parameters: class,
            sites,
        },
    })
}

/// Bind independently on both sides, serialize and compare every wire byte.
pub fn prove(
    template: &Template,
    direct: &Sample,
    bindings: &[u64],
    facts: &Facts,
) -> Result<(), String> {
    let bound = template
        .bind(bindings, &direct.params)
        .map_err(|e| format!("{}: {e}", direct.label))?;
    let expected = direct
        .package
        .bind(bindings)
        .map_err(|e| format!("{}: {e}", direct.label))?;
    let a = bound.to_bytes(facts).map_err(|e| e.to_string())?;
    let b = expected.to_bytes(facts).map_err(|e| e.to_string())?;
    if a == b {
        return Ok(());
    }
    let field = structure(&bound, &expected).err().unwrap_or_else(|| {
        bound
            .payloads
            .iter()
            .zip(&expected.payloads)
            .enumerate()
            .find(|(_, (a, b))| a != b)
            .map(|(i, _)| format!("payloads[{i}]"))
            .unwrap_or_else(|| "wire metadata".into())
    });
    let byte = a
        .iter()
        .zip(&b)
        .position(|(a, b)| a != b)
        .unwrap_or(a.len().min(b.len()));
    Err(format!(
        "{} {:?}: first difference {field}, wire byte {byte} (lengths {}, {})",
        direct.label,
        direct.params,
        a.len(),
        b.len()
    ))
}
