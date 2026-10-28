//! `l1.machine_of`, `l1.ruled`: where a ruling puts one fact (RUL.001,
//! .004, .042, .063), asked of the Rust stack's behaviour.

use std::collections::BTreeMap;

use ktpu_l0::Facts;
use ktpu_machine::{machines_dir, Machine};
use serde_json::{json, Value};

use super::{Port, DEFAULT_MACHINE};
use crate::build::{programs, Builder, Opts};
use crate::text::read;

/// The vector core a board's machine file gives (RUL.001): its register
/// count, read copies and instruction depth, or that it is refused. Units
/// alone, or a board with no machine file, are refused: the facts come from
/// the board's file, never from its units.
pub fn machine_of(inp: &Value, _: &Machine) -> Port {
    let board = inp["board"].as_str().unwrap_or("");
    match Machine::named(board) {
        Ok(m) => Ok(json!({
            "vregs": m.vc.regs,
            "vcopies": m.vc.rf_copies,
            "inst_depth": m.unit.inst_depth,
        })),
        Err(_) => Ok(json!({"refused": true})),
    }
}

/// A depth no machine file has, for the inst_depth checks.
const DEPTH: u32 = 300;

/// The one-node body whose package credits its vector core.
const BODY: &str =
    "target ktpu.v9\nimage k()\n    halt\nfn f.l1()\n    send vc[0]\n        run k()\n";

/// Whether the stack holds its own fact in place of the ruled one:
/// - `inst_depth` (RUL.004): the facts' depth not the machine's
///   (`target_constant`), a machine file without `unit.inst_depth` read
///   (`spec_default`), a package crediting other than its facts' depth
///   (`builder_default`), on a machine whose depth is `DEPTH`;
/// - `unbounded` (RUL.042): the format names the unbounded credit;
/// - `timing` (RUL.063): the scheduler's own timing constants, by name.
pub fn ruled(inp: &Value, m: &Machine) -> Port {
    match inp["check"].as_str() {
        Some("inst_depth") => inst_depth(m),
        Some("unbounded") => {
            // `l1.ruled/before`: the format as before the ruling, the
            // unbounded credit the zero credit field.
            let unbounded = match crate::mutant::on("l1.ruled/before") {
                true => ktpu_l0::format::CREDIT_UNBOUNDED,
                false => ktpu_l0::format::UNBOUNDED,
            };
            Ok(json!({"format_names_it": unbounded > ktpu_l0::format::CREDIT_UNBOUNDED}))
        }
        Some("timing") => Ok(json!({"schedule_constants": timing_constants()})),
        other => Err(format!("no check {other:?}")),
    }
}

fn inst_depth(m: &Machine) -> Port {
    let e = |e: &dyn std::fmt::Display| e.to_string();
    let v = m
        .with_fact("unit.inst_depth", json!(DEPTH))
        .map_err(|x| e(&x))?;
    let f = Facts::of(&v);
    let path = machines_dir().join(format!("{DEFAULT_MACHINE}.json"));
    let mut doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).map_err(|x| e(&x))?)
        .map_err(|x| e(&x))?;
    doc["machine"]["unit"]
        .as_object_mut()
        .ok_or("the machine file has no unit")?
        .remove("inst_depth");
    let module = read(BODY, "<ruled>").map_err(|x| e(&x))?;
    let progs = programs(&module, "f", &BTreeMap::new(), &v, None, None).map_err(|x| e(&x))?;
    let b = Builder::new(&v).map_err(|x| e(&x))?;
    let credits: Vec<u64> = progs
        .iter()
        .map(|np| b.build(np, Opts::default()).map(|p| p.build(false, false)))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|x| e(&x))?
        .iter()
        .flat_map(|p| p.units.iter().map(|u| u.credit))
        .collect();
    if credits.is_empty() {
        return Err("the body's package credits no unit".into());
    }
    Ok(json!({
        "target_constant": f.inst_depth != u64::from(DEPTH),
        "spec_default": Machine::from_json(&doc.to_string()).is_ok(),
        "builder_default": credits.iter().any(|c| *c != f.inst_depth),
    }))
}

/// The timing constants RUL.063 moves into the timing module, among the
/// scheduler's own sources (`sched/schedule.rs`, `sched/model.rs`).
fn timing_constants() -> Vec<&'static str> {
    const OWN: [&str; 4] = ["MATH_LATENCY", "QUEUE", "WALK", "CFG_COST"];
    // `l1.ruled/before`: the scheduler as before the ruling, holding its own
    // math latency.
    let before = match crate::mutant::on("l1.ruled/before") {
        true => "const MATH_LATENCY: i64 = 20;",
        false => "",
    };
    let sources = [
        include_str!("../sched/schedule.rs"),
        include_str!("../sched/model.rs"),
        before,
    ];
    OWN.into_iter()
        .filter(|n| sources.iter().any(|s| s.contains(&format!("const {n}"))))
        .collect()
}
