//! The `l0.boot` runner: the boot block's words, and the order a node is
//! loaded or restarted in (`driver/node/boot/loader.py`, L0.BOOT.002-003).

use serde_json::{json, Value};

use super::json::{arr, flag, hx, u};
use super::Port;
use crate::boot::{BootArgs, Region};

/// `BootArgs(...).pack()` as words; with `load` (or `restart`) what the
/// loader does to the node's load window, in order.
pub fn run(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let f = super::facts(m)?;
    let a = &input["args"];
    let timeout = a.get("timeout").map(u).transpose()?;
    let Ok(mut args) = BootArgs::named(u(&a["queue"])?, timeout) else {
        return Ok(json!({"refused": "TypeError"}));
    };
    for (k, slot) in [
        ("mesh", &mut args.mesh),
        ("scan", &mut args.scan),
        ("cq_depth", &mut args.cq_depth),
        ("flags", &mut args.flags),
    ] {
        if let Some(v) = a.get(k) {
            *slot = u(v)?;
        }
    }
    if let Some(us) = a.get("units") {
        args.units = arr(us)?.iter().map(u).collect::<Result<_, _>>()?;
    }
    if let Some(rs) = a.get("regions") {
        args.regions = arr(rs)?
            .iter()
            .map(|r| {
                Ok(Region {
                    base: u(&r[0])?,
                    size: u(&r[1])?,
                    use_: u(&r[2])?,
                })
            })
            .collect::<Result<_, String>>()?;
    }
    let words = match args.words(&f) {
        Ok(w) => w,
        Err(_) => return Ok(json!({"refused": "ValueError"})),
    };
    let mut out = json!({
        "bytes": words.len() * 8,
        "words": words.iter().map(|&w| hx(w as i128)).collect::<Vec<_>>(),
    });
    let block = json!(["write_spad", 0, words.len() * 8]);
    let abi = json!(["abi_check", f.boot_version]);
    if flag(input.get("load"), false) {
        out["load"] = if crate::mutant::on("l0.boot/args_after_boot") {
            json!([abi, ["stop"], ["load_elf"], ["boot"], block])
        } else {
            json!([abi, ["stop"], ["load_elf"], block, ["boot"]])
        };
    }
    if flag(input.get("restart"), false) {
        out["restart"] = json!([abi, ["stop"], ["reload_spad"], block, ["boot"]]);
    }
    Ok(out)
}
