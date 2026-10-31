//! The `l0.named` runner (RUL.042): whether the package format and a
//! firmware header each define a value whose name holds `like`, and whether
//! the two sets of values agree (`runners/l0.py:named`, module `format`).
//! Not in `ports()` until the firmware defines it (queue v4's landing).

use std::collections::BTreeSet;

use serde_json::{json, Value};

use super::Port;
use crate::format;

/// The package format's named values (`format/mod.rs`, and `unbounded`, the
/// machine's).
fn format_values(unbounded: u64) -> Vec<(&'static str, u64)> {
    vec![
        ("ALIGN", format::ALIGN as u64),
        ("HEADER_WORDS", format::HEADER_WORDS as u64),
        ("UNIT_BYTES", format::UNIT_BYTES as u64),
        ("BUFFER_BYTES", format::BUFFER_BYTES as u64),
        ("STEP_BYTES", format::STEP_BYTES as u64),
        ("RELOC_BYTES", format::RELOC_BYTES as u64),
        ("PAYLOAD_BYTES", format::PAYLOAD_BYTES as u64),
        ("FLAG_CHECKSUM", format::FLAG_CHECKSUM),
        ("FLAG_RX", format::FLAG_RX),
        ("CHECK_WORD", format::CHECK_WORD as u64),
        ("FNV_BASIS", format::FNV_BASIS),
        ("FNV_PRIME", format::FNV_PRIME),
        ("MOVER_SKIP", format::MOVER_SKIP),
        ("CREDIT_UNBOUNDED", format::CREDIT_UNBOUNDED),
        (
            "UNBOUNDED",
            if crate::mutant::on("l0.format/unbounded_drift") {
                unbounded >> 1
            } else {
                unbounded
            },
        ),
        ("UNBOUND", format::UNBOUND),
        ("F_POSTED", format::F_POSTED as u64),
        ("F_IMM", format::F_IMM as u64),
        ("F_REG", format::F_REG as u64),
        ("F_INDEXED", format::F_INDEXED as u64),
        ("F_CTRL", format::F_CTRL as u64),
        ("F_CONSUME", format::F_CONSUME as u64),
        ("F_MEM", format::F_MEM as u64),
        ("F_ACK", format::F_ACK as u64),
        ("ANY_UNIT", format::ANY_UNIT),
    ]
}

/// `{host, header, equal}` for the input's `module` (only `format`),
/// `header` and `like`.
pub fn run(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let m = m.ok_or("l0.named takes a machine")?;
    let module = input["module"].as_str().unwrap_or_default();
    if module != "format" {
        return Err(format!(
            "l0.named: module {module} is not one the L0 stack owns"
        ));
    }
    let like = input["like"].as_str().ok_or("l0.named takes like")?;
    let rel = input["header"].as_str().ok_or("l0.named takes header")?;
    let host: BTreeSet<u64> = format_values(m.fw.package.unbounded)
        .into_iter()
        .filter(|(n, _)| n.contains(like))
        .map(|(_, v)| v)
        .collect();
    let head: BTreeSet<u64> = super::pairs::defines(rel)?
        .into_iter()
        .filter(|(n, _)| n.contains(like))
        .map(|(_, v)| v)
        .collect();
    Ok(json!({
        "host": !host.is_empty(),
        "header": !head.is_empty(),
        "equal": !host.is_empty() && host == head,
    }))
}
