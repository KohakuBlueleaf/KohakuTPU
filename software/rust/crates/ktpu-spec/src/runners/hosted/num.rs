//! The `num.*` runners (`software/tests/spec/runners/num.py`, tier 0): the
//! numerics a vector reads live in several crates (the lane format in
//! ktpu-isa, the layouts and images in ktpu-l1, the expression patterns in
//! ktpu-kernels, the planner's facts in ktpu-plan), so the suite is hosted
//! here. A mutant is switched in the crate that carries its knob.

use ktpu_machine::Machine;
use serde_json::{json, Value};

type Port = fn(&Value, Option<&Machine>) -> Result<Value, String>;

const PORTS: &[(&str, Port)] = &[
    ("num.expr.lanes", expr_lanes),
    ("num.reduce.identity", reduce_identity),
];

/// Every ported num runner.
pub fn num_runners() -> Vec<&'static str> {
    PORTS.iter().map(|(n, _)| *n).collect()
}

/// The num mutants each crate carries a knob for.
pub fn num_mutants() -> Vec<&'static str> {
    let mut out: Vec<&'static str> = Vec::new();
    for names in [
        ktpu_kernels::mutants::NAMES,
        ktpu_isa::mutant::NAMES,
        ktpu_l1::mutant::NAMES,
        ktpu_plan::spec::mutant::NAMES,
    ] {
        out.extend(names.iter().copied().filter(|n| n.starts_with("num.")));
    }
    out
}

pub fn num_skipped_mutants() -> Vec<(&'static str, &'static str)> {
    vec![]
}

/// `f` with `mutant` on in the crate whose knob it is.
fn under<T>(mutant: &str, f: impl FnOnce() -> T) -> Result<T, String> {
    let find = |names: &[&'static str]| names.iter().copied().find(|n| *n == mutant);
    if let Some(n) = find(ktpu_kernels::mutants::NAMES) {
        return Ok(ktpu_kernels::mutants::with(n, f));
    }
    if let Some(n) = find(ktpu_isa::mutant::NAMES) {
        return Ok(ktpu_isa::mutant::with(n, f));
    }
    if let Some(n) = find(ktpu_l1::mutant::NAMES) {
        return Ok(ktpu_l1::mutant::with(n, f));
    }
    if let Some(n) = find(ktpu_plan::spec::mutant::NAMES) {
        return Ok(ktpu_plan::spec::mutant::with(n, f));
    }
    Err(format!("no crate carries mutant {mutant}"))
}

/// Run num runner `runner` on `input`, under `mutant` when given.
pub fn num_run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    let port = PORTS
        .iter()
        .find(|(n, _)| *n == runner)
        .map(|(_, p)| *p)
        .ok_or_else(|| format!("runner {runner} has no Rust port"))?;
    match mutant {
        None => port(input, machine),
        Some(m) => under(m, || port(input, machine))?,
    }
}

/// The machine a vector names, else the base machine file (`rs.BASE`).
fn machine_or_base(m: Option<&Machine>) -> Result<Machine, String> {
    match m {
        Some(m) => Ok(m.clone()),
        None => Machine::named("multimesh_v9t6").map_err(|r| r.to_string()),
    }
}

/// A float as a vector writes it: a number, or `float.hex` / `inf` / `nan`
/// text (`_float`).
fn float(v: &Value) -> Result<f64, String> {
    if let Some(x) = v.as_f64() {
        return Ok(x);
    }
    let s = v.as_str().ok_or_else(|| format!("a float, not {v}"))?;
    from_hex(s).ok_or_else(|| format!("a float, not {s:?}"))
}

/// Python's `float.fromhex` (and `inf`, `nan`), for the forms vectors use.
fn from_hex(s: &str) -> Option<f64> {
    let (neg, t) = match s.strip_prefix('-') {
        Some(t) => (true, t),
        None => (false, s.strip_prefix('+').unwrap_or(s)),
    };
    let mag = match t {
        "inf" | "infinity" => f64::INFINITY,
        "nan" => f64::NAN,
        _ => {
            let t = t.strip_prefix("0x")?;
            let (mant, exp) = t.split_once('p')?;
            let (int, frac) = mant.split_once('.').unwrap_or((mant, ""));
            let mut m =
                u64::from_str_radix(if int.is_empty() { "0" } else { int }, 16).ok()? as f64;
            let mut scale = 1.0 / 16.0;
            for c in frac.chars() {
                m += c.to_digit(16)? as f64 * scale;
                scale /= 16.0;
            }
            m * 2f64.powi(exp.parse::<i32>().ok()?)
        }
    };
    Some(if neg { -mag } else { mag })
}

fn floats(v: &Value) -> Result<Vec<f64>, String> {
    v.as_array()
        .ok_or_else(|| format!("a list, not {v}"))?
        .iter()
        .map(float)
        .collect()
}

/// `x` as a lane holds it: its E8M15 word (`ktpu_isa::numerics::e8m15`)
/// read back.
fn lane_value(x: f64) -> f64 {
    let w = ktpu_isa::numerics::e8m15(x);
    let (sign, exp, mant) = (w >> 23 & 1, (w >> 15 & 0xFF) as i32, w & 0x7FFF);
    let mag = match exp {
        0 => 0.0,
        0xFF if mant != 0 => f64::NAN,
        0xFF => f64::INFINITY,
        e => (1.0 + mant as f64 / 32768.0) * 2f64.powi(e - 127),
    };
    if sign == 1 {
        -mag
    } else {
        mag
    }
}

/// `num.expr.lanes`: the expression lowering's round / trunc pattern run in
/// lane arithmetic, each result rounded to E8M15 (`e8m15.value`).
fn expr_lanes(inp: &Value, m: Option<&Machine>) -> Result<Value, String> {
    use ktpu_kernels::expr::emit::{round, trunc, Lanes};
    let m = machine_or_base(m)?;
    let mut lanes = Lanes {
        value: lane_value,
        sig_bits: ktpu_kernels::trace::Facts::of(&m).sig_bits,
    };
    let op = inp["op"].as_str().ok_or("no op")?;
    let mut out = Vec::new();
    for x in floats(&inp["x"])? {
        let x = lane_value(x);
        let got = match op {
            "round" => round(&mut lanes, x),
            "trunc" => trunc(&mut lanes, x),
            other => return Err(format!("no lane op {other}")),
        }
        .map_err(|e| e.to_string())?;
        out.push(json!(got));
    }
    Ok(Value::Array(out))
}

/// `num.reduce.identity`: what a reduction's padding lanes hold, as the
/// planner fills them.
fn reduce_identity(inp: &Value, _: Option<&Machine>) -> Result<Value, String> {
    let mut out = serde_json::Map::new();
    for op in inp["ops"].as_array().ok_or("no ops")? {
        let op = op.as_str().ok_or("an op name")?;
        out.insert(op.into(), json!(ktpu_plan::run::rows::reduce_identity(op)));
    }
    Ok(Value::Object(out))
}
