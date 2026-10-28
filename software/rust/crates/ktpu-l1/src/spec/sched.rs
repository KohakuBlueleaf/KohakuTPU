//! `l1.block`, `l1.cost`, `l1.schedule`: the image scheduler (L1.SCHED.*).

use ktpu_machine::Machine;
use serde_json::{json, Map, Value};

use super::Port;
use crate::sched::{schedule_module, Ruled};
use crate::text::{emit, read, Module, Stmt};

fn err(e: crate::Error) -> String {
    e.to_string()
}

fn module(inp: &Value) -> Result<Module, String> {
    read(inp["text"].as_str().ok_or("input has no text")?, "<ktpu>").map_err(err)
}

fn lines(stmts: &[Stmt]) -> Value {
    json!(emit(stmts).lines().collect::<Vec<_>>())
}

/// The input's `image`, as written (no expansion).
fn image<'a>(m: &'a Module, inp: &Value) -> Result<&'a [Stmt], String> {
    let name = inp["image"].as_str().ok_or("input has no image")?;
    m.images
        .get(name)
        .map(|d| d.stmts.as_slice())
        .ok_or_else(|| format!("no image {name}"))
}

/// The list scheduler over an image's runs (no timing gate), as text.
pub fn block(inp: &Value, machine: &Machine) -> Port {
    let m = module(inp)?;
    let r = m.reader();
    let ruled = Ruled::new(&r, machine, m.vregs, m.vcopies).map_err(err)?;
    Ok(lines(&ruled.block(image(&m, inp)?).map_err(err)?))
}

/// The issue model on an image's statements: cycles as written, and the
/// list scheduler's order (statement indices).
pub fn cost(inp: &Value, machine: &Machine) -> Port {
    let m = module(inp)?;
    let r = m.reader();
    let ruled = Ruled::new(&r, machine, m.vregs, m.vcopies).map_err(err)?;
    let run = image(&m, inp)?;
    let written: Vec<usize> = (0..run.len()).collect();
    Ok(json!({
        "cost": ruled.cycles(run, &written).map_err(err)?,
        "order": ruled.order(run).map_err(err)?,
    }))
}

/// `schedule_module`: each image's statements, as text; with `twice`,
/// whether a second schedule of the same text reads back the same.
pub fn schedule(inp: &Value, machine: &Machine) -> Port {
    let got = schedule_module(&module(inp)?, machine).map_err(err)?;
    let mut out: Map<String, Value> = got
        .images
        .iter()
        .map(|(name, d)| (name.clone(), lines(&d.stmts)))
        .collect();
    if inp["twice"].as_bool() == Some(true) {
        let again = schedule_module(&module(inp)?, machine).map_err(err)?;
        out.insert("same".into(), json!(again.source == got.source));
    }
    Ok(Value::Object(out))
}
