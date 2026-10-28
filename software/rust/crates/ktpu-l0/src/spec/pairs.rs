//! The `l0.pairs.package` runner: a value the L0 stack owns (package format,
//! dispatch-engine lowering, boot block) beside the firmware header define it
//! copies and the machine file's fact, by name (`runners/l0.py:pairs` for
//! the `format.`, `engine.` and `args.` keys).

use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use super::Port;
use crate::boot::BootArgs;
use crate::engine;
use crate::facts::Facts;
use crate::format::Package;

/// The firmware's headers (`software/firmware/kohakuaccel/include/ka`).
fn include() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../firmware/kohakuaccel/include/ka")
}

/// `{host key, header key, machine key: value}` for the input's lists.
pub fn run(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let f = super::facts(m)?;
    let mut out = Map::new();
    for key in strs(input, "host")? {
        out.insert(key.to_string(), host(key, &f)?);
    }
    for key in strs(input, "header")? {
        let (rel, name) = key.split_once(':').ok_or(format!("header key {key}"))?;
        let v = match name.strip_prefix("struct ") {
            Some(s) => json!(struct_fields(rel, s)?),
            None => json!(
                defines(rel)?
                    .into_iter()
                    .rev()
                    .find(|(n, _)| n == name)
                    .ok_or(format!("{rel} defines no {name}"))?
                    .1
            ),
        };
        out.insert(key.to_string(), v);
    }
    for key in strs(input, "machine")? {
        let machine = m.ok_or("machine keys need the vector's machine")?;
        let v = machine
            .get(key)
            .cloned()
            .ok_or(format!("machine has no {key}"))?;
        out.insert(key.to_string(), v);
    }
    Ok(Value::Object(out))
}

fn strs<'a>(input: &'a Value, k: &str) -> Result<Vec<&'a str>, String> {
    match input.get(k) {
        None => Ok(Vec::new()),
        Some(v) => v
            .as_array()
            .ok_or(format!("{k}: not a list"))?
            .iter()
            .map(|x| x.as_str().ok_or(format!("{k}: not a string")))
            .collect(),
    }
}

/// The value the stack itself uses for host key `key`, through the code
/// that uses it (so its mutants reach it).
fn host(key: &str, f: &Facts) -> Result<Value, String> {
    let header = || -> Result<u64, String> {
        let empty = Package {
            units: Vec::new(),
            buffers: Vec::new(),
            steps: Vec::new(),
            relocs: Vec::new(),
            payloads: Vec::new(),
            signature: 0,
            ack_reserve: 0,
            checksum: false,
            buffer_names: Vec::new(),
        };
        let raw = empty.to_bytes(f).map_err(|e| e.to_string())?;
        Ok(u64::from_le_bytes(raw[..8].try_into().unwrap()))
    };
    let boot =
        || -> Result<Vec<u64>, String> { BootArgs::new(0, 0).words(f).map_err(|e| e.to_string()) };
    Ok(match key {
        "format.MAGIC" => json!(header()? & 0xFFFF_FFFF),
        "format.VERSION" => json!(header()? >> 32 & 0xFFFF),
        "format.HEADER_BYTES" => json!(header()? >> 48),
        "format.REGS" => json!(f.regs),
        "engine.WAIT" => json!(engine::wait_code()),
        "engine.REL" => json!(engine::rel()),
        "engine.CTR_MASK" => json!(f.ctr_mask()),
        "engine.SRC_MOVER" => json!(engine::src_mover(f)),
        "engine.UNITS" => json!(f.engine_units),
        "args.MAGIC" => json!(boot()?[0]),
        "args.VERSION" => json!(boot()?[1]),
        "args.MAX_UNITS" => json!(f.boot_max_units),
        "args.WORDS" => json!(boot()?.len()),
        _ => return Err(format!("host key {key}: not a value the L0 stack owns")),
    })
}

fn read(rel: &str) -> Result<String, String> {
    std::fs::read_to_string(include().join(rel)).map_err(|e| format!("{rel}: {e}"))
}

/// `text` with every `/* ... */` comment removed.
fn uncommented(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(i) = rest.find("/*") {
        out.push_str(&rest[..i]);
        match rest[i + 2..].find("*/") {
            Some(j) => rest = &rest[i + 2 + j + 2..],
            None => return out,
        }
    }
    out.push_str(rest);
    out
}

/// A C integer literal with its `uUlL` suffix, or None.
fn literal(s: &str) -> Option<u64> {
    let s = s.trim_end_matches(['u', 'U', 'l', 'L']);
    match s.strip_prefix("0x") {
        Some(h) if !h.is_empty() && h.chars().all(|c| c.is_ascii_hexdigit()) => {
            u64::from_str_radix(h, 16).ok()
        }
        Some(_) => None,
        None if !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()) => s.parse().ok(),
        None => None,
    }
}

/// A define's value: `N`, `(N)`, or `(N << S)` with decimal N and S.
fn define_value(v: &str) -> Option<(u64, bool)> {
    let v = v.trim();
    if let Some(n) = literal(v) {
        return Some((n, false));
    }
    let inner = v.strip_prefix('(')?.strip_suffix(')')?.trim();
    if let Some(n) = literal(inner) {
        return Some((n, false));
    }
    let (a, b) = inner.split_once("<<")?;
    let (a, b) = (a.trim(), b.trim());
    let digits = |s: &str| {
        let s = s.trim_end_matches(['u', 'U', 'l', 'L']);
        (!s.is_empty() && s.chars().all(|c| c.is_ascii_digit())).then(|| s.parse::<u64>().ok())
    };
    if !b.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    Some((digits(a)?? << b.parse::<u32>().ok()?, true))
}

/// `[(NAME, value)]` of a header's integer defines, then its shifted
/// defines, then its enum members (a member with no value is the previous
/// one plus one), in the order a later one overrides an earlier.
pub(super) fn defines(rel: &str) -> Result<Vec<(String, u64)>, String> {
    let text = uncommented(&read(rel)?);
    let (mut plain, mut shifted) = (Vec::new(), Vec::new());
    for line in text.lines() {
        let Some(rest) = line.trim_start().strip_prefix("#define") else {
            continue;
        };
        if !rest.starts_with([' ', '\t']) {
            continue;
        }
        let rest = rest.trim_start();
        let name_end = rest
            .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
            .unwrap_or(rest.len());
        let (name, value) = rest.split_at(name_end);
        if name.is_empty() || !value.starts_with([' ', '\t']) {
            continue;
        }
        match define_value(value) {
            Some((v, false)) => plain.push((name.to_string(), v)),
            Some((v, true)) => shifted.push((name.to_string(), v)),
            None => {}
        }
    }
    let mut out = plain;
    out.extend(shifted);
    let mut rest = text.as_str();
    while let Some(i) = rest.find("enum") {
        let after = &rest[i + 4..];
        let Some(open) = after.find('{') else { break };
        let head = after[..open].trim();
        if !head.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
            rest = after;
            continue;
        }
        let body = &after[open + 1..];
        let Some(close) = body.find('}') else { break };
        let mut next = 0u64;
        for item in body[..close].split(',') {
            let item = item.trim_start();
            let end = item
                .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
                .unwrap_or(item.len());
            let name = &item[..end];
            if name.is_empty() {
                continue;
            }
            if let Some(v) = item[end..].trim_start().strip_prefix('=') {
                let v = v.trim();
                let lit = v
                    .find(|c: char| !(c.is_ascii_alphanumeric()))
                    .map_or(v, |k| &v[..k]);
                if let Some(n) = literal(lit) {
                    next = n;
                }
            }
            out.push((name.to_string(), next));
            next += 1;
        }
        rest = &body[close..];
    }
    Ok(out)
}

/// The field names of `struct name` in a header, in order.
fn struct_fields(rel: &str, name: &str) -> Result<Vec<String>, String> {
    let text = read(rel)?;
    let mut at = 0;
    let body = loop {
        let i = text[at..]
            .find("struct")
            .ok_or(format!("{rel}: no struct {name}"))?
            + at;
        let after = text[i + 6..].trim_start();
        let rest = after
            .strip_prefix(name)
            .filter(|r| r.trim_start().starts_with('{'));
        match rest {
            Some(r) if after.len() > name.len() => {
                let r = r.trim_start();
                let end = r
                    .find("};")
                    .ok_or(format!("{rel}: struct {name} not closed"))?;
                break uncommented(&r[1..end]);
            }
            _ => at = i + 6,
        }
    };
    let mut out = Vec::new();
    for stmt in body.split(';').take(body.matches(';').count()) {
        let mut s = stmt.trim_end();
        if s.ends_with(']') {
            if let Some(k) = s.rfind('[') {
                s = s[..k].trim_end();
            }
        }
        let start = s
            .rfind(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
            .map_or(0, |k| k + 1);
        if start < s.len() {
            out.push(s[start..].to_string());
        }
    }
    Ok(out)
}
