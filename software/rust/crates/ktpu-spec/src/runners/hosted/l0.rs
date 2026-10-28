//! `l0.program` with `through: packages` (`ktpu::spec::hosted`).

use ktpu_machine::Machine;
use serde_json::Value;

/// ktpu-l0's suite `run`, with the `through: packages` form scheduled.
pub fn l0_run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    if runner != "l0.program" || input.get("through").is_none() {
        return ktpu_l0::spec::run(runner, input, machine, mutant);
    }
    let go = || ktpu::spec::hosted::l0(runner, input, machine);
    match mutant {
        None => go(),
        Some(name) => {
            let name = ktpu_l0::spec::mutants()
                .into_iter()
                .find(|n| *n == name)
                .ok_or_else(|| format!("no l0 mutant {name}"))?;
            ktpu_l0::mutant::with(name, go)
        }
    }
}
