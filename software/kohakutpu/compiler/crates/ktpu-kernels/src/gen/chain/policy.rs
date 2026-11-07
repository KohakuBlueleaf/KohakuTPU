//! A typed chain's public interface and private capacity residency.

use std::collections::{BTreeMap, HashSet};

use ktpu_graph as g;
use ktpu_machine::Machine;
use serde::Deserialize;
use serde_json::Value;

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::{fail, R};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Port {
    side: String,
    port: String,
    name: String,
}

fn bytes(spec: &g::Spec, machine: &Machine) -> R<i64> {
    let values = spec.shape.iter().try_fold(1i64, |n, d| n.checked_mul(*d));
    let bytes = values.and_then(|n| match spec.dtype {
        g::Dtype::Mx7a | g::Dtype::Mx7b => Some(machine.mx7_bytes(n)),
        dtype => n.checked_mul(super::super::Hw::item(dtype)),
    });
    bytes.ok_or_else(|| ktpu_machine::Refusal::new("API.OP.001", "chain capacity exceeds i64"))
}

/// Apply construction policies before complete-schedule pricing. Shared
/// multi-node edges keep their distributed planner's explicit DRAM residency.
pub(crate) fn finish(
    mut kernel: Kernel,
    arguments: &Value,
    machine: &Machine,
    local: bool,
) -> R<(Kernel, BTreeMap<String, String>)> {
    if let Some(value) = arguments.get("link_space") {
        let space = match value.as_str() {
            Some("stage") => g::BufSpace::Stage,
            Some("dram") => g::BufSpace::Dram,
            Some("mem") => g::BufSpace::Mem,
            _ => return fail("API.OP.001", "chain link_space must be stage, dram or mem"),
        };
        if local {
            if let Some(link) = arguments.get("to").and_then(Value::as_str) {
                if let Some(scratch) = kernel.graph.scratch.iter_mut().find(|s| s.name == link) {
                    scratch.spec.space = space;
                }
            }
        }
    }
    if let Some(value) = arguments.get("stage_budget") {
        let budget = value.as_i64().filter(|n| *n >= 0).ok_or_else(|| {
            ktpu_machine::Refusal::new("API.OP.001", "chain stage_budget must be nonnegative")
        })?;
        if local {
            let mut used = 0i64;
            for scratch in &kernel.graph.scratch {
                if scratch.spec.space == g::BufSpace::Stage {
                    used = used
                        .checked_add(bytes(&scratch.spec, machine)?)
                        .ok_or_else(|| {
                            ktpu_machine::Refusal::new(
                                "API.OP.001",
                                "chain stage capacity exceeds i64",
                            )
                        })?;
                }
            }
            for scratch in &mut kernel.graph.scratch {
                if scratch.spec.space == g::BufSpace::Mem {
                    let size = bytes(&scratch.spec, machine)?;
                    scratch.spec.space = if size <= budget.saturating_sub(used) {
                        used += size;
                        g::BufSpace::Stage
                    } else {
                        g::BufSpace::Dram
                    };
                }
            }
        }
    }
    kernel = super::quant::kernel(kernel, super::quant::Backend::read(arguments)?, machine)?;
    let mut mapping = BTreeMap::new();
    if let Some(value) = arguments.get("interface") {
        let ports: Vec<Port> = serde_json::from_value(value.clone()).map_err(|error| {
            ktpu_machine::Refusal::new("API.OP.001", format!("chain interface: {error}"))
        })?;
        let mut exposed = HashSet::new();
        let mut aliases = HashSet::new();
        let mut ordered = Vec::new();
        for port in ports {
            let source = match port.side.as_str() {
                "first" => kernel.graph.consts["chain"]["first_ports"][&port.port]
                    .as_str()
                    .ok_or_else(|| {
                        ktpu_machine::Refusal::new(
                            "API.OP.001",
                            "chain interface has no first port",
                        )
                    })?
                    .to_string(),
                "second" => kernel.graph.consts["chain"]["ports"][&port.port]
                    .as_str()
                    .ok_or_else(|| {
                        ktpu_machine::Refusal::new(
                            "API.OP.001",
                            "chain interface has no second port",
                        )
                    })?
                    .to_string(),
                _ => return fail("API.OP.001", "chain interface side must be first or second"),
            };
            if port.name.is_empty()
                || !port.name.chars().enumerate().all(|(i, c)| {
                    c == '_' || c.is_ascii_alphabetic() || i > 0 && c.is_ascii_digit()
                })
                || !exposed.insert(source.clone())
                || !aliases.insert(port.name.clone())
            {
                return fail(
                    "API.OP.001",
                    "chain interface needs distinct ports and identifiers",
                );
            }
            let mut declared = kernel
                .graph
                .ports
                .iter()
                .find(|p| p.name == source)
                .cloned()
                .ok_or_else(|| {
                    ktpu_machine::Refusal::new("API.OP.001", "chain interface names a private port")
                })?;
            declared.name = port.name.clone();
            ordered.push(declared);
            mapping.insert(source, port.name);
        }
        if exposed.len() != kernel.graph.ports.len() {
            return fail(
                "API.OP.001",
                "chain interface must expose every public port",
            );
        }
        let mut taken: HashSet<_> = kernel.graph.ports.iter().map(|p| p.name.clone()).collect();
        taken.extend(kernel.graph.scratch.iter().map(|s| s.name.clone()));
        taken.extend(aliases.iter().cloned());
        let mut body = serde_json::to_value(&kernel.graph.body).map_err(|e| {
            ktpu_machine::Refusal::new("API.OP.001", format!("chain interface: {e}"))
        })?;
        let mut symbols = HashSet::new();
        super::names_in(&body, &["var", "target"], &mut symbols);
        taken.extend(symbols.iter().cloned());
        let mut symbol_names = BTreeMap::new();
        let mut collisions: Vec<_> = symbols.iter().filter(|s| aliases.contains(*s)).collect();
        collisions.sort();
        for symbol in collisions {
            let replacement = (0..)
                .map(|i| format!("chain_symbol_{i}"))
                .find(|n| taken.insert(n.clone()))
                .expect("an unused symbol name");
            symbol_names.insert(symbol.clone(), replacement);
        }
        let renamed_symbols = symbol_names.keys().cloned().collect();
        super::renamed(&mut body, &renamed_symbols, &|n| symbol_names[n].clone());
        kernel.graph.body = serde_json::from_value(body).map_err(|e| {
            ktpu_machine::Refusal::new("API.OP.001", format!("chain interface: {e}"))
        })?;
        for scratch in &mut kernel.graph.scratch {
            if aliases.contains(&scratch.name) {
                let replacement = (0..)
                    .map(|i| format!("chain_private_{i}"))
                    .find(|n| taken.insert(n.clone()))
                    .expect("an unused private name");
                mapping.insert(scratch.name.clone(), replacement.clone());
                scratch.name = replacement;
            }
        }
        super::rows::views(&mut kernel.graph.body, &mut |view| {
            if let Some(name) = mapping.get(&view.buffer) {
                view.buffer = name.clone();
            }
        });
        kernel.given = ordered
            .iter()
            .map(|port| (port.name.clone(), port.spec.clone()))
            .collect();
        kernel.graph.ports = ordered;
        for key in ["ports", "first_ports"] {
            if let Some(ports) = kernel.graph.consts["chain"][key].as_object_mut() {
                for value in ports.values_mut() {
                    if let Some(replacement) = value.as_str().and_then(|old| mapping.get(old)) {
                        *value = Value::String(replacement.clone());
                    }
                }
            }
        }
    }
    if arguments.get("stage_budget").is_some()
        || arguments.get("interface").is_some()
        || arguments.get("link_space").is_some()
    {
        kernel.graph.name = name(
            "chain_policy",
            vec![Py::str(&kernel.graph.name), Py::str(&arguments.to_string())],
        );
    }
    Ok((kernel, mapping))
}
