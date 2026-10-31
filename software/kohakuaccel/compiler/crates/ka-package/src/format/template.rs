//! Host-only template metadata. Binding returns an ordinary wire package.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::{bind::bind_addresses, Alu, Op, Package, Step, F_IMM, F_REG};
use crate::error::{Refusal, Result};
use crate::mutant;
use crate::word::{mask64, WordOps};

pub type Params = BTreeMap<String, i64>;

/// Inclusive shape-class bounds for a named integer parameter.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Bounds {
    pub lo: i64,
    pub hi: i64,
}

/// One segment of an unsigned value, optionally a buffer-relative address.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValuePatch {
    pub name: String,
    #[serde(default)]
    pub payload: usize,
    /// A step index selects a whitelisted value field instead of a payload.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub step: Option<usize>,
    pub bit: u32,
    pub width: u32,
    #[serde(default)]
    pub shift: u32,
    #[serde(default)]
    pub constant: i64,
    #[serde(default)]
    pub coefficients: BTreeMap<String, i64>,
    #[serde(default)]
    pub buffer: Option<usize>,
}

/// Serialized beside package bytes, never inside the node's package format.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ParameterTable {
    #[serde(default)]
    pub parameters: BTreeMap<String, Bounds>,
    #[serde(default)]
    pub sites: Vec<ValuePatch>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub environment: Option<Vec<[i64; 3]>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub buffer_sizes: Option<Vec<u64>>,
}

/// A package and its host-side parameter table; only `bind` yields wire bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Template {
    pub package: Package,
    pub table: ParameterTable,
}

/// Node counts are either host-written or installed by package registers.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum CountPath {
    #[default]
    Step,
    Register,
}

/// Value fields only: opcode, routing, flags and payload topology stay fixed.
pub fn step_fields(op: Op) -> &'static [(u32, u32)] {
    match op {
        Op::Repeat => &[(96, 16)],
        Op::Alu | Op::Jump => &[(64, 64)],
        Op::Dispatch | Op::Fetch | Op::Await | Op::Mwait => &[(32, 32)],
        _ => &[],
    }
}

pub fn step_value(s: &Step, bit: u32, width: u32) -> u64 {
    let value = if bit < 64 { s.count } else { s.arg };
    let shift = if bit < 64 { bit - 32 } else { bit - 64 };
    (value >> shift) & mask64(width)
}

fn set_step(s: &mut Step, bit: u32, width: u32, value: u64) {
    let (field, shift) = if bit < 64 {
        (&mut s.count, bit - 32)
    } else {
        (&mut s.arg, bit - 64)
    };
    let mask = mask64(width) << shift;
    *field = (*field & !mask) | ((value << shift) & mask);
}

impl Template {
    /// Buffers with any launch-specific extent overrides applied and validated.
    pub fn effective_buffers(&self) -> Result<Vec<super::Buffer>> {
        let mut buffers = self.package.buffers.clone();
        if let Some(sizes) = &self.table.buffer_sizes {
            if sizes.len() != buffers.len() {
                return Err(Refusal::new(
                    "L0.BIND.039",
                    "buffer_sizes length must match package buffer table",
                ));
            }
            for (b, &size) in buffers.iter_mut().zip(sizes) {
                if size == 0 || (!mutant::on("l0.template/extent") && size > b.size) {
                    return Err(Refusal::new(
                        "L0.BIND.039",
                        format!(
                            "launch buffer size {size} exceeds certified class extent {}",
                            b.size
                        ),
                    ));
                }
                b.size = size;
            }
        }
        Ok(buffers)
    }

    /// Bind allocation handles through the driver's live translation snapshot.
    pub fn bind_handles(
        &self,
        handles: &[u64],
        translations: &[super::bind::Translation],
        params: &Params,
    ) -> Result<Package> {
        self.bind_handles_with_path(handles, translations, params, CountPath::Step)
    }

    /// [`Template::bind_handles`], the step counts written by `path`.
    pub fn bind_handles_with_path(
        &self,
        handles: &[u64],
        translations: &[super::bind::Translation],
        params: &Params,
        path: CountPath,
    ) -> Result<Package> {
        let buffers = self.effective_buffers()?;
        let addresses = super::bind::translate(&buffers, handles, translations)?;
        let mut resolved = self.clone();
        resolved.package.buffers = buffers;
        for (buffer, &address) in resolved.package.buffers.iter_mut().zip(&addresses) {
            buffer.default = address;
        }
        resolved.bind_with_path(&addresses, params, path)
    }

    /// Validate the class, evaluate in checked i128, then bind and write fields.
    /// An error leaves both the template and the caller's bindings unchanged.
    pub fn bind(&self, bindings: &[u64], params: &Params) -> Result<Package> {
        let table = &self.table;
        let fail = |clause, site: &str, message: &str| {
            Refusal::new(clause, format!("site {site}, params {params:?}: {message}"))
        };
        let names = table
            .sites
            .iter()
            .map(|s| s.name.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        for (name, bounds) in &table.parameters {
            let Some(&value) = params.get(name) else {
                return Err(fail(
                    "L0.BIND.038",
                    &names,
                    &format!("missing parameter {name}"),
                ));
            };
            if bounds.lo > bounds.hi
                || (!mutant::on("l0.template/class") && !(bounds.lo..=bounds.hi).contains(&value))
            {
                return Err(fail(
                    "L0.BIND.038",
                    &names,
                    &format!(
                        "parameter {name}={value} outside {}..{}",
                        bounds.lo, bounds.hi
                    ),
                ));
            }
        }
        if let Some(name) = params.keys().find(|n| !table.parameters.contains_key(*n)) {
            return Err(fail(
                "L0.BIND.038",
                &names,
                &format!("unknown parameter {name}"),
            ));
        }
        let buffers = self.effective_buffers()?;
        let addresses = bind_addresses(&buffers, bindings);
        if !mutant::on("l0.template/environment") {
            if let Some(expected) = &table.environment {
                let actual = super::bind::environment(&buffers, &addresses)?;
                if &actual != expected {
                    return Err(Refusal::new(
                        "L0.BIND.044",
                        format!(
                            "translation environment {actual:?} breaks certified aliasing/overlap pattern {expected:?}"
                        ),
                    ));
                }
            }
        }
        let mut values = Vec::with_capacity(table.sites.len());
        for (index, s) in table.sites.iter().enumerate() {
            let bad =
                |clause, message: &str| fail(clause, &format!("{}[{index}]", s.name), message);
            if s.name.is_empty()
                || (s.step.is_none() && s.payload >= self.package.payloads.len())
                || !(1..=64).contains(&s.width)
                || s.bit > 256 - s.width
                || s.shift >= 64
                || s.width > 64 - s.shift
            {
                return Err(bad("L0.BIND.037", "invalid payload, field, name or shift"));
            }
            if let Some(index) = s.step {
                let valid = self.package.steps.get(index).is_some_and(|step| {
                    s.payload == 0
                        && s.buffer.is_none()
                        && s.shift == 0
                        && step_fields(step.op).contains(&(s.bit, s.width))
                        && (!matches!(step.op, Op::Alu | Op::Jump)
                            || step.flags & super::F_IMM != 0)
                });
                if !valid {
                    return Err(bad("L0.BIND.041", "invalid step value field"));
                }
            }
            let overlaps = |payload: usize, bit: u64, width: u64| {
                payload == s.payload
                    && (s.bit as u64) < bit.saturating_add(width)
                    && bit < (s.bit + s.width) as u64
            };
            if table.sites[..index]
                .iter()
                .any(|o| o.step == s.step && overlaps(o.payload, o.bit as u64, o.width as u64))
                || self
                    .package
                    .relocs
                    .iter()
                    .any(|r| s.step.is_none() && overlaps(r.payload as usize, r.bit, r.width))
            {
                return Err(bad("L0.BIND.037", "patch overlaps another write"));
            }
            let mut value = s.constant as i128;
            for (name, coefficient) in &s.coefficients {
                let Some(&param) = params.get(name) else {
                    return Err(bad("L0.BIND.038", &format!("undeclared parameter {name}")));
                };
                if !mutant::on("l0.template/affine") {
                    value = value
                        .checked_add((*coefficient as i128) * param as i128)
                        .ok_or_else(|| bad("L0.BIND.037", "affine arithmetic overflow"))?;
                }
            }
            if let Some(buffer) = s.buffer {
                let Some(b) = buffers.get(buffer) else {
                    return Err(bad("L0.BIND.039", "buffer index outside table"));
                };
                if !mutant::on("l0.template/extent") && (value < 0 || value >= b.size as i128) {
                    return Err(bad("L0.BIND.039", "address offset outside buffer extent"));
                }
                value = value
                    .checked_add(addresses[buffer] as i128)
                    .ok_or_else(|| bad("L0.BIND.037", "address arithmetic overflow"))?;
            }
            // Matching adjacent source segments form a split field, as in L0.BIND.004.
            let continued = table.sites.iter().any(|o| {
                o.payload == s.payload
                    && o.step == s.step
                    && o.buffer == s.buffer
                    && o.constant == s.constant
                    && o.coefficients == s.coefficients
                    && o.shift == s.shift + s.width
            });
            if !mutant::on("l0.template/width")
                && (value < 0
                    || value > u64::MAX as i128
                    || (!continued && (value >> s.shift) > mask64(s.width) as i128))
            {
                return Err(bad(
                    "L0.BIND.037",
                    &format!(
                        "value {value} does not fit field width {} shift {}",
                        s.width, s.shift
                    ),
                ));
            }
            values.push(value as u64);
        }
        let mut populated = self.package.clone();
        populated.buffers = buffers;
        if !mutant::on("l0.template/late-repeat-check") {
            for (site, value) in table.sites.iter().zip(&values) {
                if let Some(index) = site.step {
                    if !mutant::on("l0.template/step")
                        && !(populated.steps[index].op == Op::Repeat && *value == 0)
                    {
                        set_step(&mut populated.steps[index], site.bit, site.width, *value);
                    }
                } else if let Some(buffer) =
                    site.buffer.filter(|_| !mutant::on("l0.template/extent"))
                {
                    populated.relocs.push(super::Reloc {
                        payload: site.payload as u64,
                        bit: site.bit as u64,
                        width: site.width as u64,
                        shift: site.shift as u64,
                        buffer: buffer as u64,
                        addend: *value as i128 - addresses[buffer] as i128,
                    });
                } else {
                    populated.payloads[site.payload].set(
                        site.bit,
                        site.width,
                        *value >> site.shift,
                    );
                }
            }
        }
        let mut out = populated.bind(bindings)?;
        for (s, value) in table.sites.iter().zip(values) {
            let shift = if mutant::on("l0.template/split") {
                0
            } else {
                s.shift
            };
            let value = (value >> shift) & mask64(s.width);
            if let Some(index) = s.step {
                if !mutant::on("l0.template/step") {
                    set_step(&mut out.steps[index], s.bit, s.width, value);
                }
            } else {
                out.payloads[s.payload].set(s.bit, s.width, value);
            }
        }
        if table.parameters.is_empty()
            && table.sites.is_empty()
            && mutant::on("l0.template/identity")
        {
            out.signature ^= 1;
        }
        for s in &table.sites {
            if let Some(index) = s.step {
                let step = &out.steps[index];
                if !mutant::on("l0.template/step_extent")
                    && ((step.op == Op::Dispatch
                        && step
                            .arg
                            .checked_add(step.count)
                            .is_none_or(|end| end > out.payloads.len() as u64))
                        || (step.op == Op::Repeat && step_value(step, 96, 16) == 0))
                {
                    return Err(fail("L0.BIND.042", &s.name, "invalid bound step count"));
                }
            }
        }
        if !mutant::on("l0.template/late-repeat-check") {
            populated.steps.clone_from(&out.steps);
            populated.check_repeat_bindings(&out.payloads)?;
        }
        Ok(out)
    }

    /// Register mode reserves r15 and adds MOV/PATCH before the original steps.
    /// Existing register/control-flow users refuse rather than aliasing r15.
    pub fn bind_with_path(
        &self,
        bindings: &[u64],
        params: &Params,
        path: CountPath,
    ) -> Result<Package> {
        let mut out = self.bind(bindings, params)?;
        if path == CountPath::Step || !self.table.sites.iter().any(|s| s.step.is_some()) {
            return Ok(out);
        }
        if out.steps.iter().any(|s| {
            matches!(s.op, Op::Load | Op::Alu | Op::Jump | Op::Patch) || s.flags & F_REG != 0
        }) {
            return Err(Refusal::new(
                "L0.BIND.043",
                "register count path requires a register-free straight-line package",
            ));
        }
        let sites: Vec<_> = self
            .table
            .sites
            .iter()
            .filter(|s| s.step.is_some())
            .collect();
        let prefix = sites
            .len()
            .checked_mul(2)
            .ok_or_else(|| Refusal::new("L0.BIND.043", "step count overflow"))?;
        let mut steps = Vec::with_capacity(prefix + out.steps.len());
        for site in sites {
            let index = site.step.unwrap();
            let value = step_value(&out.steps[index], site.bit, site.width);
            steps.push(Step::new(Op::Alu, 15, Alu::Mov as u64, value, F_IMM));
            let target = if mutant::on("l0.template/register_target") {
                index
            } else {
                prefix + index
            };
            steps.push(Step::new(
                Op::Patch,
                15,
                target as u64,
                site.bit as u64 | (site.width as u64) << 8 | 1 << 24,
                0,
            ));
            set_step(&mut out.steps[index], site.bit, site.width, 0);
        }
        steps.extend(out.steps);
        out.steps = steps;
        Ok(out)
    }
}
