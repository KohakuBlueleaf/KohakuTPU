//! A pending read's form: composed where the knob and the kernels' compiled
//! cycles take it, materialized else.

use ktpu_machine::Machine;

use crate::call::Call;
use crate::gen::prepared::Composed;

/// Compare one composed read with its materialized launches.
pub fn composed(
    mode: Composed,
    kernel: &Call,
    materialized: &[Call],
    m: &Machine,
    cost: &dyn Fn(&Call) -> Option<i64>,
) -> bool {
    sequences(mode, std::slice::from_ref(kernel), materialized, m, cost)
}

/// Compare complete alternatives, including the gaps between their launches.
pub fn sequences(
    mode: Composed,
    kernels: &[Call],
    materialized: &[Call],
    m: &Machine,
    cost: &dyn Fn(&Call) -> Option<i64>,
) -> bool {
    if crate::mutants::on("api.forms/composed_always") {
        return true;
    }
    if crate::mutants::on("api.forms/composed_never") || mode == Composed::Off {
        return false;
    }
    let total = |calls: &[Call]| -> Option<i64> {
        let cycles: Option<i64> = calls.iter().map(cost).sum();
        cycles.map(|c| c + calls.len().saturating_sub(1) as i64 * m.vc.launch_gap as i64)
    };
    let Some(took) = total(kernels) else {
        return false;
    };
    if mode == Composed::On {
        return true;
    }
    total(materialized).is_none_or(|c| took <= c)
}
