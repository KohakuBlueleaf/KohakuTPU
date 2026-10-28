//! The `l0.pkg.*` runners: bytes, parse, bind, lower, inflight.

use serde_json::{json, Value};

use super::json::{describe, engine_entries, hex_bytes, hx, lines, package, refused, step_dict, u};
use super::Port;
use crate::engine;
use crate::facts::Facts;
use crate::format::{Op, Package};

fn facts(m: Option<&ktpu_machine::Machine>) -> Result<Facts, String> {
    super::facts(m)
}

/// `Package.to_bytes` of a described package, as 32-byte lines.
pub fn bytes(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let f = facts(m)?;
    Ok(match package(&input["package"])?.to_bytes(&f) {
        Ok(raw) => json!({"length": raw.len(), "lines": lines(&raw)}),
        Err(e) => refused(&e),
    })
}

/// `raw` with its five tables written in reverse order, the header's offsets
/// moved to match.
pub fn reorder(raw: &[u8]) -> Vec<u8> {
    let q = |at: usize| u64::from_le_bytes(raw[at..at + 8].try_into().unwrap());
    let mut h: Vec<u64> = (0..12).map(|k| q(8 * k)).collect();
    let (npay, nrel) = (h[3] & 0xFFFF_FFFF, h[3] >> 32);
    let (nbuf, nstep, nunit) = (h[4] & 0xFFFF, (h[4] >> 16) & 0xFFFF, (h[4] >> 32) & 0xFFFF);
    let sizes = [nunit * 16, nbuf * 32, nstep * 16, nrel * 16, npay * 32];
    let tables: Vec<Vec<u8>> = (0..5)
        .map(|i| raw[h[5 + i] as usize..(h[5 + i] + sizes[i]) as usize].to_vec())
        .collect();
    let mut out = vec![0u8; 96];
    for i in (0..5).rev() {
        h[5 + i] = out.len() as u64;
        out.extend_from_slice(&tables[i]);
        out.resize(out.len().div_ceil(32) * 32, 0);
    }
    h[2] = out.len() as u64 | (h[2] >> 32) << 32;
    for (k, w) in h.iter().enumerate() {
        out[8 * k..8 * k + 8].copy_from_slice(&w.to_le_bytes());
    }
    out
}

/// `Package.from_bytes` of a described package's bytes, reordered when asked.
pub fn parse(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let f = facts(m)?;
    let mut raw = package(&input["package"])?
        .to_bytes(&f)
        .map_err(|e| e.to_string())?;
    if input
        .get("reorder")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        raw = reorder(&raw);
    }
    Ok(match Package::from_bytes(&raw, &f) {
        Ok(p) => describe(&p),
        Err(e) => refused(&e),
    })
}

/// `Package.bind(bindings)` of a described package.
pub fn bind(input: &Value, _: Option<&ktpu_machine::Machine>) -> Port {
    let src = package(&input["package"])?;
    let binds = input
        .get("bindings")
        .and_then(Value::as_array)
        .map(|a| a.iter().map(u).collect::<Result<Vec<_>, _>>())
        .transpose()?
        .unwrap_or_default();
    Ok(match src.bind(&binds) {
        Ok(got) => json!({
            "payloads": got.payloads.iter().map(|w| w.to_hex()).collect::<Vec<_>>(),
            "relocs": got.relocs.len(),
            "buffers": got.buffers.len(),
            "units_same": got.units == src.units,
            "steps_same": got.steps == src.steps,
            "signature": hx(got.signature as i128),
            "ack_reserve": got.ack_reserve,
            "checksum": got.checksum,
        }),
        Err(e) => refused(&e),
    })
}

/// What `engine.lower` returned, its ENGINE steps decoded.
pub fn lowered(p: &Package, src: &Package) -> Value {
    let steps: Vec<Value> = p
        .steps
        .iter()
        .map(|s| {
            if s.op == Op::Engine {
                let mut e = engine_entries(p, s);
                e["op"] = json!("ENGINE");
                e
            } else {
                step_dict(s)
            }
        })
        .collect();
    let n = src.payloads.len();
    json!({
        "steps": steps,
        "kept": p.payloads.len() >= n && p.payloads[..n] == src.payloads[..],
        "units": describe(p)["units"],
        "buffers": p.buffers.len(),
        "relocs": p.relocs.len(),
        "signature": hx(p.signature as i128),
        "ack_reserve": p.ack_reserve,
        "checksum": p.checksum,
    })
}

/// `engine.lower` of a described package.
pub fn lower(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let f = facts(m)?;
    let src = package(&input["package"])?;
    Ok(match engine::lower(&src, &f) {
        Ok(p) => lowered(&p, &src),
        Err(e) => refused(&e),
    })
}

/// The most words a lowered package can have in flight on each unit: its
/// entries replayed with every WAIT met exactly, the counter-width wants
/// unwrapped against the words sent so far (L0.DE.006, L0.DE.007).
pub fn inflight(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let f = facts(m)?;
    let bits = f.counter_bits;
    let pkg = engine::lower(&package(&input["package"])?, &f).map_err(|e| e.to_string())?;
    let n = pkg.units.len();
    let (mut sent, mut done, mut most) = (vec![0i64; n], vec![0i64; n], vec![0i64; n]);
    let mut dst_unit = std::collections::HashMap::new();
    for (i, x) in pkg.units.iter().enumerate() {
        dst_unit.insert(1 << 24 | (x.fetch & 0xFFFF), i);
        dst_unit.insert(x.y << 8 | x.x, i);
    }
    let (mut dst, mut pending) = (None, 0i64);
    let span = 1i64 << bits;
    for s in pkg.steps.iter().filter(|s| s.op == Op::Engine) {
        for (code, v) in engine::entries(&pkg, s) {
            if code == engine::WAIT && ((v >> 56) as usize) < n {
                let x = (v >> 56) as usize;
                let mut want = (v & (span as u64 - 1)) as i64;
                let base = sent[x] - (span >> 1);
                want += (base - want + span - 1).div_euclid(span) * span;
                done[x] = done[x].max(want);
            } else if code == engine::DST {
                dst = Some(v);
            } else if code & 0x3F == engine::ARG3 {
                pending = (v & 0xFF) as i64;
            } else if code == engine::GO {
                if let Some(d) = dst {
                    let x = *dst_unit.get(&d).ok_or("GO to no unit")?;
                    sent[x] += if d >> 24 != 0 { pending } else { 1 };
                    most[x] = most[x].max(sent[x] - done[x]);
                }
            }
        }
    }
    Ok(json!({"most_in_flight": most, "half_window": span >> 1}))
}

/// `{"bytes": hex}` of a package's bytes, or the refusal.
pub fn bytes_of(p: &Package, f: &Facts) -> Value {
    match p.to_bytes(f) {
        Ok(b) => json!({ "bytes": hex_bytes(&b) }),
        Err(e) => json!({ "refused": e.to_string() }),
    }
}
