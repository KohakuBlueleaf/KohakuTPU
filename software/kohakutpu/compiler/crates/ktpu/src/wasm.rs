//! wasm-bindgen exports (feature `wasm`) of [`crate::json`]; an unreadable
//! machine file becomes a JS `Error`.

use wasm_bindgen::prelude::*;

/// compile(source, machineJson, name?) -> string: the compiled kernel's JSON,
/// or `{"refused": {...}}`.
#[wasm_bindgen]
pub fn compile(source: &str, machine_json: &str, name: Option<String>) -> Result<String, JsError> {
    crate::json::compile(source, machine_json, name.as_deref()).map_err(|e| JsError::new(&e))
}

/// The machine-file schema version this build reads (`_rs.MACHINE_SCHEMA`).
#[wasm_bindgen(js_name = machineSchema)]
pub fn machine_schema() -> String {
    ktpu_machine::schema_version().to_string()
}
