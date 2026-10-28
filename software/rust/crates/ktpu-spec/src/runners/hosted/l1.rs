//! `l1.package` with `all` and `l1.order` (`ktpu::spec::hosted`).

use ktpu_machine::Machine;
use serde_json::Value;

/// The verifier's mutants an `l1.order` vector names.
const VERIFY_MUTANTS: [&str; 1] = ["l1.verify/reverse-segment"];

/// ktpu-l1's suite `run`, with `l1.package` `all` and `l1.order` scheduled.
pub fn l1_run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    let scheduled =
        runner == "l1.order" || (runner == "l1.package" && input["all"] == Value::Bool(true));
    if !scheduled {
        return ktpu_l1::spec::run(runner, input, machine, mutant);
    }
    let go = || ktpu::spec::hosted::l1(runner, input, machine);
    let Some(name) = mutant else {
        return go();
    };
    if let Some(v) = VERIFY_MUTANTS.into_iter().find(|v| *v == name) {
        return ktpu_verify::mutant::with(v, go);
    }
    let name = ktpu_l1::spec::mutants()
        .into_iter()
        .find(|n| *n == name)
        .ok_or_else(|| format!("no l1 mutant {name}"))?;
    ktpu_l1::mutant::with(name, go)
}

/// ktpu-l1's knobs and the verifier's an `l1.order` vector names.
pub fn l1_mutants() -> Vec<&'static str> {
    let mut out = ktpu_l1::spec::mutants();
    out.extend(VERIFY_MUTANTS);
    out
}

/// ktpu-l1's skipped mutants less the verifier's knobs run here.
pub fn l1_skipped_mutants() -> Vec<(&'static str, &'static str)> {
    ktpu_l1::spec::skipped_mutants()
        .into_iter()
        .filter(|(m, _)| !VERIFY_MUTANTS.contains(m))
        .collect()
}
