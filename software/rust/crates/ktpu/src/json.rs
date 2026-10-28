//! The compile path as JSON text, with no disk cache: the `wasm` exports'
//! body, testable natively.
//!
//! The compiled kernel reads as `_rs.compile`'s dict, package bytes as hex;
//! a stage's refusal as `{"refused": {stage, clause, message}}`, as
//! `_rs.compile_many`'s items.

use crate::stack::Stack;
use crate::{Cache, Compiled, Input, Refused};
use ktpu_machine::Machine;
use serde_json::{json, Value};

/// `source` (graph JSON or L2 text) through the stack for the machine file
/// `machine_json`; `name` picks the kernel (the module's first fn when
/// None). Err: the machine file does not read.
pub fn compile(source: &str, machine_json: &str, name: Option<&str>) -> Result<String, String> {
    let m = Machine::from_json(machine_json).map_err(|e| e.to_string())?;
    let got = crate::compile(&Stack, &Input::detect(source), name, &m, &Cache::off());
    let v = match got {
        Ok(c) => compiled(&c),
        Err(r) => json!({ "refused": refused(&r) }),
    };
    Ok(v.to_string())
}

/// A compiled kernel as JSON.
pub fn compiled(c: &Compiled) -> Value {
    json!({
        "name": c.name,
        "l2": c.l2,
        "l1": c.l1,
        "packages": c.packages,
        "scratch": c.scratch,
        "verdict": c.verdict,
        "bound": c.bound,
    })
}

/// A stage's refusal as JSON.
pub fn refused(r: &Refused) -> Value {
    json!({
        "stage": r.stage.name(),
        "clause": r.refusal.clause,
        "message": r.refusal.message,
    })
}
