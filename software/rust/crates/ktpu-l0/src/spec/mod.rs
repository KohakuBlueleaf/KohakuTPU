//! Rust ports of the tier-0 `l0.*` spec runners (`software/tests/spec/
//! runners/l0.py`), for `ktpu-spec`: each takes a vector's `input` and its
//! machine and returns what the runner returns, compared with `expect`.

pub mod boot;
pub mod build;
pub mod json;
pub mod named;
pub mod pairs;
pub mod pkg;
pub mod program;
pub mod template;

use serde_json::Value;

use crate::Facts;

/// A port's result: the runner's output, or why it cannot run.
pub type Port = Result<Value, String>;

/// A runner port: `(input, machine) -> output`.
pub type PortFn = fn(&Value, Option<&ktpu_machine::Machine>) -> Port;

/// The machine a vector naming none is read against: the one every `l0`
/// vector without a machine was written for.
pub const DEFAULT_MACHINE: &str = "multimesh_v9t6";

/// A frozen vector an accepted ruling supersedes. Its input as written is
/// refused under the ruling (the `rul_*` tests witness that); it runs with
/// the ruled input instead (`set`: JSON pointer -> JSON value), so its
/// mutants still reach the code they mutate, and must give `expect` (JSON
/// text), or the vector's own expect when none.
pub struct Superseded {
    pub vector: &'static str,
    pub ruling: &'static str,
    pub set: &'static [(&'static str, &'static str)],
    pub expect: Option<&'static str>,
}

/// The vector files change when the Python harness retires (2p.7).
pub const SUPERSEDED: &[Superseded] = &[
    // The 8-bit field could not hold 0x400000008: bound at 0x5 both
    // segments fit; the high segment takes (0x5 >> 34) = 0.
    Superseded {
        vector: "L0.BIND.010.split_field",
        ruling: "RUL.039",
        set: &[("/bindings/0", r#""0x5""#)],
        expect: Some(
            r#"{"payloads":["0xffffffffffffc0fffffffffffffffffffffffffffffffffffffffffffffff08f"],
            "relocs":0,"buffers":0,"units_same":true,"steps_same":true,
            "signature":"0x0","ack_reserve":0,"checksum":false}"#,
        ),
    },
    // No default timeout: the caller names the bound the old default held.
    Superseded {
        vector: "L0.BOOT.001.placement",
        ruling: "RUL.033",
        set: TIMEOUT,
        expect: None,
    },
    Superseded {
        vector: "L0.BOOT.002.order",
        ruling: "RUL.033",
        set: TIMEOUT,
        expect: None,
    },
    Superseded {
        vector: "L0.BOOT.007.default_timeout",
        ruling: "RUL.033",
        set: TIMEOUT,
        expect: None,
    },
    Superseded {
        vector: "L0.BOOT.008.default_cq_depth",
        ruling: "RUL.033",
        set: TIMEOUT,
        expect: None,
    },
    Superseded {
        vector: "L0.STEP.008.default_cq_depth",
        ruling: "RUL.033",
        set: TIMEOUT,
        expect: None,
    },
    Superseded {
        vector: "DBG.TMO.004.default",
        ruling: "RUL.033",
        set: TIMEOUT,
        expect: None,
    },
    Superseded {
        vector: "L0.BOOT.026.regions",
        ruling: "RUL.033",
        set: TIMEOUT,
        expect: None,
    },
];

const TIMEOUT: &[(&str, &str)] = &[("/args/timeout", r#""0x1e8480""#)];

/// `(input, expect)` vector `name` runs with: the ruled ones when a ruling
/// supersedes it, else its own.
pub fn ruled(name: &str, input: &Value, expect: &Value) -> (Value, Value) {
    let Some(s) = SUPERSEDED.iter().find(|s| s.vector == name) else {
        return (input.clone(), expect.clone());
    };
    let mut input = input.clone();
    for (pointer, value) in s.set {
        let v: Value = serde_json::from_str(value).expect("ruled value is JSON");
        let (parent, key) = pointer.rsplit_once('/').expect("a JSON pointer");
        let slot = input
            .pointer_mut(parent)
            .expect("the ruled field's parent exists");
        match slot {
            Value::Array(a) => a[key.parse::<usize>().expect("an index")] = v,
            // RUL.033 asks only that the caller name a timeout: a vector
            // that names one already follows it, and keeps its own.
            Value::Object(o) if s.ruling == "RUL.033" => {
                o.entry(key.to_string()).or_insert(v);
            }
            Value::Object(o) => {
                o.insert(key.to_string(), v);
            }
            _ => panic!("{pointer}: not a container"),
        }
    }
    let expect = s.expect.map_or(expect.clone(), |e| {
        serde_json::from_str(e).expect("ruled output is JSON")
    });
    (input, expect)
}

/// Runner name -> port.
pub fn ports() -> Vec<(&'static str, PortFn)> {
    vec![
        ("l0.pkg.bytes", pkg::bytes as PortFn),
        ("l0.pkg.parse", pkg::parse),
        ("l0.pkg.bind", pkg::bind),
        ("l0.template.bind", template::run),
        ("l0.pkg.lower", pkg::lower),
        ("l0.pkg.inflight", pkg::inflight),
        ("l0.pkg.build", build::run),
        ("l0.program", program::run),
        ("l0.boot", boot::run),
        ("l0.pairs.package", pairs::run),
    ]
}

/// The runners this crate ports.
pub fn runners() -> Vec<&'static str> {
    ports().into_iter().map(|p| p.0).collect()
}

/// Runner `runner` on a vector's input and machine, under `mutant` when one
/// is given (which needs the `mutants` feature).
pub fn run(
    runner: &str,
    input: &Value,
    machine: Option<&ktpu_machine::Machine>,
    mutant: Option<&str>,
) -> Port {
    let (_, port) = ports()
        .into_iter()
        .find(|p| p.0 == runner)
        .ok_or_else(|| format!("no l0 port for runner {runner}"))?;
    match mutant {
        None => port(input, machine),
        Some(m) => under(m, || port(input, machine)),
    }
}

#[cfg(feature = "mutants")]
fn under(name: &str, f: impl FnOnce() -> Port) -> Port {
    let known = crate::mutant::NAMES
        .iter()
        .find(|n| **n == name)
        .ok_or_else(|| format!("no Rust knob for mutant {name}"))?;
    crate::mutant::with(known, f)
}

#[cfg(not(feature = "mutants"))]
fn under(name: &str, _: impl FnOnce() -> Port) -> Port {
    Err(format!(
        "mutant {name}: ktpu-l0 built without the `mutants` feature"
    ))
}

/// The mutants with a Rust knob.
pub fn mutants() -> Vec<&'static str> {
    crate::mutant::NAMES.to_vec()
}

/// Mutants a ported vector names that have no knob, and why: none.
pub fn skipped_mutants() -> Vec<(&'static str, &'static str)> {
    Vec::new()
}

/// The vector's machine's facts, or the default machine's.
pub fn facts(m: Option<&ktpu_machine::Machine>) -> Result<Facts, String> {
    match m {
        Some(m) => Ok(Facts::of(m)),
        None => ktpu_machine::Machine::named(DEFAULT_MACHINE)
            .map(|m| Facts::of(&m))
            .map_err(|e| e.to_string()),
    }
}

/// `out` narrowed to the input's `keys`, when it names any.
pub fn keys(out: Value, input: &Value) -> Value {
    match input.get("keys").and_then(Value::as_array) {
        Some(ks) => Value::Object(
            ks.iter()
                .filter_map(Value::as_str)
                .filter_map(|k| out.get(k).map(|v| (k.to_string(), v.clone())))
                .collect(),
        ),
        None => out,
    }
}
