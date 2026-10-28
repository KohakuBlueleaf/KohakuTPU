//! Numeric layout and image properties, through the L1 implementations.

use super::Port;
use crate::layout::{
    buffer::{Buffer, LayoutVal},
    tensor,
};
use crate::lower::image::{image, UNKNOWN};
use ktpu_machine::Machine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use ktpu_isa::spec::num::hex;

fn ints(v: &Value) -> Result<Vec<i128>, String> {
    v.as_array()
        .ok_or("an array")?
        .iter()
        .map(|v| {
            v.as_i64()
                .map(i128::from)
                .ok_or_else(|| "an integer".into())
        })
        .collect()
}

pub fn buffer(inp: &Value, m: &Machine) -> Port {
    let mut layout = std::collections::BTreeMap::new();
    for (k, v) in inp["layout"].as_object().into_iter().flatten() {
        let v = if v.is_array() {
            LayoutVal::Dims(ints(v)?)
        } else {
            LayoutVal::Int(v.as_i64().ok_or("a layout integer")? as i128)
        };
        layout.insert(k.clone(), v);
    }
    let b = Buffer {
        dtype: inp["dtype"].as_str().ok_or("no dtype")?.into(),
        shape: ints(&inp["shape"])?,
        space: "dram".into(),
        layout,
    };
    if b.check().is_err() {
        return Ok(json!({"refused": true}));
    }
    let mut out = json!({"nbytes": b.bytes(m).map_err(|e| e.to_string())?});
    let raw = if inp.get("bits").is_some() {
        let bits = ints(&inp["bits"])?
            .into_iter()
            .map(|n| n as u16)
            .collect::<Vec<_>>();
        b.pack(&bits, m)
    } else if let Some(values) = inp["values"].as_array() {
        fn flatten(v: &Value, out: &mut Vec<f64>) -> Result<(), String> {
            if let Some(a) = v.as_array() {
                for v in a {
                    flatten(v, out)?;
                }
            } else {
                out.push(v.as_f64().ok_or("a value")?);
            }
            Ok(())
        }
        let mut flat = Vec::new();
        for v in values {
            flatten(v, &mut flat)?;
        }
        let values = flat;
        b.pack_values(&values, m)
    } else {
        return Ok(out);
    }
    .map_err(|e| e.to_string())?;
    if inp["digest"] == true {
        out["sha256"] = json!(format!("{:x}", Sha256::digest(&raw)));
        out["len"] = json!(raw.len());
    } else {
        out["hex"] = json!(raw.iter().map(|b| format!("{b:02x}")).collect::<String>());
    }
    Ok(out)
}

pub fn fp16_values(inp: &Value, _: &Machine) -> Port {
    let bits = ints(&inp["bits"])?
        .into_iter()
        .map(|n| n as u16)
        .collect::<Vec<_>>();
    let values = tensor::fp16_values(&bits, inp["consumer"].as_str().ok_or("no consumer")?)
        .map_err(|e| e.to_string())?;
    Ok(json!(values.into_iter().map(hex).collect::<Vec<_>>()))
}

pub fn quantise(inp: &Value, m: &Machine) -> Port {
    let mut bits = ints(&inp["bits"])?
        .into_iter()
        .map(|n| n as u16)
        .collect::<Vec<_>>();
    let n = bits.len();
    let block = tensor::Mx7::of(m).map_err(|e| e.to_string())?.kblock;
    bits.resize(n.div_ceil(block) * block, 0);
    let (q, scales) = tensor::quantise(&bits, block).map_err(|e| e.to_string())?;
    let values = tensor::value(&bits, block).map_err(|e| e.to_string())?;
    Ok(
        json!({"q": q[..n], "es": scales.iter().map(|s| s.0).collect::<Vec<_>>(),
        "m8": scales.iter().map(|s| s.1).collect::<Vec<_>>(),
        "value": values[..n].iter().copied().map(hex).collect::<Vec<_>>()}),
    )
}

fn words(stmt: &str, m: &Machine) -> Result<Vec<u64>, String> {
    let text = format!("image k()\n    {stmt}\n    halt\n");
    let module = crate::text::read(&text, "<num.image>").map_err(|e| e.to_string())?;
    image(&module, m, "k", &[], UNKNOWN)
        .map(|(w, _)| w)
        .map_err(|e| e.to_string())
}

pub fn walk(inp: &Value, m: &Machine) -> Port {
    let Ok(words) = words(inp["stmt"].as_str().ok_or("no stmt")?, m) else {
        return Ok(json!({"refused": true}));
    };
    let w = words
        .into_iter()
        .find(|w| matches!((w >> 27) & 31, 0x14 | 0x15))
        .ok_or("no walk")?;
    Ok(
        json!({"op": if (w >> 27) & 31 == 0x14 { "vunpk" } else { "vpack" },
        "mode": (w >> 20) & 3, "n": ((w >> 17) & 7) + 1, "cb": (w >> 14) & 7}),
    )
}

pub fn seti(inp: &Value, m: &Machine) -> Port {
    let mut out = Vec::new();
    for stmt in inp["stmts"].as_array().ok_or("no stmts")? {
        let words = words(stmt.as_str().ok_or("a statement")?, m)?;
        let at = words
            .iter()
            .position(|w| (w >> 27) & 31 == 0x1a)
            .ok_or("no seti")?;
        out.push(format!("{:06x}", words[at + 1]));
    }
    Ok(json!(out))
}
