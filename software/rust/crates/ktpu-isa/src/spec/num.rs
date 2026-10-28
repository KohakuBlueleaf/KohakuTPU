//! Numeric format and converting-mover adapters.

use super::{KPort, Knobs};
use crate::unit::mover::{MoverIsa, MoverOp, R_DIM, R_HDR};
use serde_json::{json, Value};

pub const PORTS: &[(&str, KPort)] = &[("num.e8m15.value", values), ("num.mover.convert", convert)];

pub fn float(v: &Value) -> Result<f64, String> {
    if let Some(x) = v.as_f64() {
        return Ok(x);
    }
    let s = v.as_str().ok_or("a float")?;
    let (sign, s) = if let Some(s) = s.strip_prefix('-') {
        (-1.0, s)
    } else {
        (1.0, s.strip_prefix('+').unwrap_or(s))
    };
    let mag = match s {
        "inf" | "infinity" => f64::INFINITY,
        "nan" => f64::NAN,
        _ => {
            let (mant, exp) = s
                .strip_prefix("0x")
                .and_then(|s| s.split_once('p'))
                .ok_or("a hex float")?;
            let (int, frac) = mant.split_once('.').unwrap_or((mant, ""));
            let mut m = u64::from_str_radix(if int.is_empty() { "0" } else { int }, 16)
                .map_err(|e| e.to_string())? as f64;
            let mut scale = 1.0 / 16.0;
            for c in frac.chars() {
                m += c.to_digit(16).ok_or("a hex digit")? as f64 * scale;
                scale /= 16.0;
            }
            m * 2f64.powi(exp.parse::<i32>().map_err(|e| e.to_string())?)
        }
    };
    Ok(sign * mag)
}

/// Python's canonical hexadecimal representation of an IEEE double.
pub fn hex(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    let sign = if x.is_sign_negative() { "-" } else { "" };
    if x.is_infinite() {
        return format!("{sign}inf");
    }
    if x == 0.0 {
        return format!("{sign}0x0.0p+0");
    }
    let bits = x.to_bits();
    let exp = (bits >> 52 & 0x7ff) as i32;
    let frac = bits & ((1u64 << 52) - 1);
    let (lead, power) = if exp == 0 {
        (0, -1022)
    } else {
        (1, exp - 1023)
    };
    format!("{sign}0x{lead}.{frac:013x}p{power:+}")
}

fn values(inp: &Value, _: Option<&Knobs>) -> Result<Value, String> {
    inp["x"]
        .as_array()
        .ok_or("no x")?
        .iter()
        .map(|v| float(v).map(|x| json!(hex(crate::numerics::e8m15_value(x)))))
        .collect::<Result<Vec<_>, _>>()
        .map(Value::Array)
}

fn convert(inp: &Value, knobs: Option<&Knobs>) -> Result<Value, String> {
    let default;
    let m = match knobs {
        Some(k) => &k.machine,
        None => {
            default = crate::Machine::named("multimesh_v9t6").map_err(|e| e.to_string())?;
            &default
        }
    };
    let op = if inp["op"] == "gt4" {
        MoverOp::Transpose4 {
            src: 0x1000,
            dst: 0x8000,
            groups: inp["groups"].as_i64().ok_or("no groups")?,
        }
    } else {
        MoverOp::Quantise {
            src: 0x1000,
            dst: 0x8000,
            entries: inp["entries"].as_i64().ok_or("no entries")?,
            b: inp["b"].as_bool().unwrap_or(false),
        }
    };
    let writes = MoverIsa::new(m).writes(&op).map_err(|e| e.to_string())?;
    let hdr = writes
        .iter()
        .find(|(r, v)| *r == R_HDR && v & 1 == 0)
        .ok_or("no header")?
        .1;
    let levels = |sel| {
        writes
            .iter()
            .filter(|(r, v)| *r == R_DIM && v & 1 == sel)
            .map(|(_, v)| json!([(v >> 4) & 0xffff, ((v >> 20) as u32 as i32) as i64]))
            .collect::<Vec<_>>()
    };
    Ok(
        json!({"xform": (hdr >> 47) & 15, "mode": (hdr >> 55) & 15, "src": levels(0), "dst": levels(1)}),
    )
}
