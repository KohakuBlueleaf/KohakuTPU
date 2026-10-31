//! A pending read's form: composed where the knob and the kernels' compiled
//! cycles take it, materialized else.

use ktpu_machine::Machine;

use crate::call::Call;
use crate::gen::prepared::Composed;

/// Whether the read is composed: a composed kernel that lowers, and under
/// [`Composed::Cost`] no slower than the `materialized` launches and their
/// gaps, a kernel's answer from `cost`.
pub fn composed(
    mode: Composed,
    kernel: &Call,
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
    let Some(took) = cost(kernel) else {
        return false;
    };
    if mode == Composed::On {
        return true;
    }
    let gaps = materialized.len().saturating_sub(1) as i64 * m.vc.launch_gap as i64;
    let others: Option<i64> = materialized.iter().map(cost).sum();
    others.is_none_or(|c| took <= c + gaps)
}
