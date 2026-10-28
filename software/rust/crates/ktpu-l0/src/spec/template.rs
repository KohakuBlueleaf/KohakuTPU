//! Tier-0 value binding, including byte identity with an ordinary package.

use serde_json::{json, Value};

use super::json::{package, u};
use super::Port;
use crate::format::template::{CountPath, ParameterTable, Params, Template};

pub fn run(input: &Value, machine: Option<&ktpu_machine::Machine>) -> Port {
    let package = package(&input["package"])?;
    let table: ParameterTable =
        serde_json::from_value(input["table"].clone()).map_err(|e| e.to_string())?;
    let params: Params =
        serde_json::from_value(input["params"].clone()).map_err(|e| e.to_string())?;
    let bindings = input["bindings"]
        .as_array()
        .ok_or("bindings is not an array")?
        .iter()
        .map(u)
        .collect::<Result<Vec<_>, _>>()?;
    let template = Template { package, table };
    let path = match input
        .get("count_path")
        .and_then(Value::as_str)
        .unwrap_or("step")
    {
        "step" => CountPath::Step,
        "register" => CountPath::Register,
        other => return Err(format!("unknown count path {other}")),
    };
    match template.bind_with_path(&bindings, &params, path) {
        Err(e) if input.get("refusal_only").and_then(Value::as_bool) == Some(true) => {
            Ok(json!({"refused": e.clause}))
        }
        Err(e) => Ok(json!({"clause": e.clause, "error": e.msg})),
        Ok(bound) => {
            let facts = super::facts(machine)?;
            let raw = bound.to_bytes(&facts).map_err(|e| e.to_string())?;
            let expected = if let Some(p) = input.get("ordinary") {
                package_from(p, &facts)?
            } else {
                template
                    .package
                    .bind(&bindings)
                    .and_then(|p| p.to_bytes(&facts))
                    .map_err(|e| e.to_string())?
            };
            Ok(json!({
                "payloads": bound.payloads.iter().map(|w| w.to_hex()).collect::<Vec<_>>(),
                "bytes_equal": raw == expected,
                "buffers": bound.buffers.len(),
                "relocs": bound.relocs.len(),
            }))
        }
    }
}

fn package_from(input: &Value, facts: &crate::Facts) -> Result<Vec<u8>, String> {
    package(input)?.to_bytes(facts).map_err(|e| e.to_string())
}
