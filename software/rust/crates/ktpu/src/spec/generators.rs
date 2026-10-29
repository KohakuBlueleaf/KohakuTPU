//! Generator properties that span the kernel, numerical and pipeline crates.

use crate::{Backend, Stack};
use ktpu_machine::Machine;
use serde_json::{json, Map, Value};

pub const RUNNERS: &[&str] = &["isa.cluster.mm", "num.expr.lanes", "num.reduce.identity"];

pub fn run(name: &str, input: &Value, machine: Option<&Machine>) -> Result<Value, String> {
    match name {
        "isa.cluster.mm" => cluster_mm(input, machine.ok_or("mm needs a machine")?),
        "num.expr.lanes" => expr_lanes(input, machine),
        "num.reduce.identity" => {
            let mut out = Map::new();
            for op in input["ops"].as_array().ok_or("no ops")? {
                let op = op.as_str().ok_or("an op name")?;
                out.insert(op.into(), json!(ktpu_plan::run::rows::reduce_identity(op)));
            }
            Ok(Value::Object(out))
        }
        _ => Err(format!("no generator port for {name}")),
    }
}

fn cluster_mm(inp: &Value, m: &Machine) -> Result<Value, String> {
    let p = inp["plan"]
        .as_array()
        .ok_or("no plan")?
        .iter()
        .map(|x| x.as_i64().ok_or("a plan entry"))
        .collect::<Result<Vec<_>, _>>()?;
    let [bt, rows, n, k] = p[..] else {
        return Err("a plan needs four entries".into());
    };
    let mm = ktpu_kernels::gen::matmul::plan(bt, rows, n, k, true, 4, ktpu_machine::PAIRING, m)
        .map_err(|e| e.to_string())?;
    let out = ktpu_kernels::Operand::full(mm.n, mm.bt * mm.m);
    let kernel = ktpu_kernels::mm_kernel(&mm, &ktpu_kernels::expr::load(0), &[], &out, m)
        .map_err(|e| e.to_string())?;
    let graph = serde_json::to_string(&kernel.graph).map_err(|e| e.to_string())?;
    let b = Stack;
    let l2 = b.graph_to_l2(&graph, m).map_err(|e| e.to_string())?;
    let name = l2.name.clone();
    let l1 = b
        .passes(&l2, &name, m)
        .and_then(|p| b.lower(&p, &name, m))
        .and_then(|l| b.schedule(&l, m))
        .map_err(|e| e.to_string())?;
    let re = regex::Regex::new(r"(?m)^\s*send (mg\[\d+\])").unwrap();
    let mut sends = Map::new();
    for c in re.captures_iter(&b.print_l1(&l1)) {
        let n = sends.get(&c[1]).and_then(Value::as_i64).unwrap_or(0);
        sends.insert(c[1].to_string(), Value::from(n + 1));
    }
    Ok(Value::Object(sends))
}

fn float(v: &Value) -> Result<f64, String> {
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

fn lane_value(x: f64) -> f64 {
    let w = ktpu_isa::numerics::e8m15(x);
    let (sign, exp, mant) = (w >> 23 & 1, (w >> 15 & 0xff) as i32, w & 0x7fff);
    let mag = match exp {
        0 => 0.0,
        0xff if mant != 0 => f64::NAN,
        0xff => f64::INFINITY,
        e => (1.0 + mant as f64 / 32768.0) * 2f64.powi(e - 127),
    };
    if sign == 1 {
        -mag
    } else {
        mag
    }
}

fn expr_lanes(inp: &Value, machine: Option<&Machine>) -> Result<Value, String> {
    use ktpu_kernels::expr::emit::{round, trunc, Lanes};
    let default;
    let m = match machine {
        Some(m) => m,
        None => {
            default = Machine::named("multimesh_v9t6").map_err(|e| e.to_string())?;
            &default
        }
    };
    let mut lanes = Lanes {
        value: lane_value,
        sig_bits: ktpu_kernels::trace::Facts::of(m).sig_bits,
    };
    let mut out = Vec::new();
    for x in inp["x"].as_array().ok_or("no x")? {
        let x = lane_value(float(x)?);
        let got = match inp["op"].as_str().ok_or("no op")? {
            "round" => round(&mut lanes, x),
            "trunc" => trunc(&mut lanes, x),
            op => return Err(format!("no lane op {op}")),
        }
        .map_err(|e| e.to_string())?;
        out.push(json!(got));
    }
    Ok(Value::Array(out))
}
