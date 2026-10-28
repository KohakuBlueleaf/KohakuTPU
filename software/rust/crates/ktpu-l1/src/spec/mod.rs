//! Rust ports of the tier-0 `l1.*` spec runners (`software/tests/spec/
//! runners/l1.py`), for `ktpu-spec`: each takes a vector's `input` and its
//! machine and returns what the runner returns, compared with `expect`.

pub mod json;
mod lower;
pub mod num;
mod ruled;
mod sched;
mod text;

use std::panic::{catch_unwind, AssertUnwindSafe};

use ktpu_machine::Machine;
use serde_json::Value;

/// A port's result: the runner's output, or why it cannot run.
pub type Port = Result<Value, String>;

/// A runner port: `(input, machine) -> output`.
pub type PortFn = fn(&Value, &Machine) -> Port;

/// The machine a vector naming none is read against.
pub const DEFAULT_MACHINE: &str = "multimesh_v9t6";

/// Runner name -> port.
pub fn ports() -> Vec<(&'static str, PortFn)> {
    vec![
        ("l1.parse", text::parse as PortFn),
        ("num.buffer", num::buffer),
        ("num.mx7.quantise", num::quantise),
        ("num.fp16_values", num::fp16_values),
        ("num.image.walk", num::walk),
        ("num.image.seti", num::seti),
        ("l1.module", text::module),
        ("l1.expand", text::expand),
        ("l1.image", lower::image),
        ("l1.steps", lower::steps),
        ("l1.build", lower::build),
        ("l1.package", lower::package),
        ("l1.order", lower::order),
        ("l1.block", sched::block),
        ("l1.cost", sched::cost),
        ("l1.schedule", sched::schedule),
        ("l1.machine_of", ruled::machine_of),
        ("l1.ruled", ruled::ruled),
    ]
}

/// The runners this crate ports.
pub fn runners() -> Vec<&'static str> {
    ports().into_iter().map(|p| p.0).collect()
}

/// Runner `runner` on a vector's input and machine, under `mutant` when one
/// is given (which needs the `mutants` feature). A panic is an error.
pub fn run(runner: &str, input: &Value, machine: Option<&Machine>, mutant: Option<&str>) -> Port {
    #[cfg(feature = "mutants")]
    if let Some(name) = mutant.and_then(|mu| {
        ktpu_machine::mutant::NAMES
            .iter()
            .copied()
            .find(|n| *n == mu)
    }) {
        return ktpu_machine::mutant::with(name, || run(runner, input, machine, None));
    }
    #[cfg(feature = "mutants")]
    if let Some(name) =
        mutant.and_then(|mu| ktpu_isa::mutant::NAMES.iter().copied().find(|n| *n == mu))
    {
        return ktpu_isa::mutant::with(name, || run(runner, input, machine, None));
    }
    let (_, port) = ports()
        .into_iter()
        .find(|p| p.0 == runner)
        .ok_or_else(|| format!("no l1 port for runner {runner}"))?;
    let default;
    let m = match machine {
        Some(m) => m,
        None => {
            default = Machine::named(DEFAULT_MACHINE).map_err(|e| e.to_string())?;
            &default
        }
    };
    let go = || {
        crate::build::forget_held();
        catch_unwind(AssertUnwindSafe(|| port(input, m)))
            .unwrap_or_else(|_| Err(format!("{runner} panicked")))
    };
    match mutant {
        None => go(),
        Some(name) => under(name, go),
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
        "mutant {name}: ktpu-l1 built without the `mutants` feature"
    ))
}

pub use lower::{Held, OrderSchedule, Schedule};

/// `l1.order`, ordered by `schedule` (the verifier's), under `mutant` when
/// given.
pub fn order_all(
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
    schedule: OrderSchedule<'_>,
) -> Port {
    let default;
    let m = match machine {
        Some(m) => m,
        None => {
            default = Machine::named(DEFAULT_MACHINE).map_err(|e| e.to_string())?;
            &default
        }
    };
    let go = || {
        catch_unwind(AssertUnwindSafe(|| lower::order_with(input, m, schedule)))
            .unwrap_or_else(|_| Err("l1.order panicked".into()))
    };
    match mutant {
        None => go(),
        Some(name) => under(name, go),
    }
}

/// `l1.package` with `all` (`build.packages`), ordered by `schedule` (the
/// verifier's, which ktpu-l1 does not depend on), under `mutant` when given.
pub fn package_all(
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
    schedule: Schedule<'_>,
) -> Port {
    let default;
    let m = match machine {
        Some(m) => m,
        None => {
            default = Machine::named(DEFAULT_MACHINE).map_err(|e| e.to_string())?;
            &default
        }
    };
    let go = || {
        catch_unwind(AssertUnwindSafe(|| lower::package_all(input, m, schedule)))
            .unwrap_or_else(|_| Err("l1.package all panicked".into()))
    };
    match mutant {
        None => go(),
        Some(name) => under(name, go),
    }
}

/// The image scheduler's frozen vectors RUL.063 supersedes, with the ruled
/// model's answer the golden dump keeps (`golden/sched/vectors`, `ruled`).
const SCHED_RULED: [(&str, &str); 7] = [
    (
        "L1.SCHED.001.runs",
        include_str!("../../../../golden/sched/vectors/L1.SCHED.001.runs.json"),
    ),
    (
        "L1.SCHED.002.l1-words",
        include_str!("../../../../golden/sched/vectors/L1.SCHED.002.l1-words.json"),
    ),
    (
        "L1.SCHED.003.model",
        include_str!("../../../../golden/sched/vectors/L1.SCHED.003.model.json"),
    ),
    (
        "L1.SCHED.004.gain",
        include_str!("../../../../golden/sched/vectors/L1.SCHED.004.gain.json"),
    ),
    (
        "L1.SCHED.004.short-run",
        include_str!("../../../../golden/sched/vectors/L1.SCHED.004.short-run.json"),
    ),
    (
        "L1.SCHED.005.image",
        include_str!("../../../../golden/sched/vectors/L1.SCHED.005.image.json"),
    ),
    (
        "L1.SCHED.006.deterministic",
        include_str!("../../../../golden/sched/vectors/L1.SCHED.006.deterministic.json"),
    ),
];

/// `(input, expect)` a vector runs with: a scheduler vector RUL.063
/// supersedes expects the ruled model's answer, when the golden was dumped
/// from this same input and expectation; every other vector as written.
pub fn ruled(stem: &str, input: &Value, expect: &Value) -> (Value, Value) {
    let golden = SCHED_RULED
        .iter()
        .find(|(s, _)| *s == stem)
        .and_then(|(_, text)| serde_json::from_str::<Value>(text).ok());
    match golden {
        Some(g) if &g["input"] == input && &g["expect"] == expect && !g["ruled"].is_null() => {
            (input.clone(), g["ruled"].clone())
        }
        _ => (input.clone(), expect.clone()),
    }
}

/// The mutants with a Rust knob.
pub fn mutants() -> Vec<&'static str> {
    crate::mutant::NAMES
        .iter()
        .chain(ktpu_machine::mutant::NAMES)
        .chain(ktpu_isa::mutant::NAMES)
        .copied()
        .collect()
}

/// Mutants an L1 vector names that have no knob here, and why.
pub fn skipped_mutants() -> Vec<(&'static str, &'static str)> {
    vec![(
        "l1.verify/reverse-segment",
        "breaks verify.schedule's segment order (l1.order): ktpu-verify's knob",
    )]
}
