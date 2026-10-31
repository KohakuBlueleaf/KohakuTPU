//! A pending read's form: composed where the knob and the kernels' compiled
//! cycles take it, materialized else, the same answer for both planners.

use std::cell::RefCell;
use std::collections::HashMap;

use ktpu_kernels::gen::prepared::Composed;
use ktpu_machine::Machine;
use serde_json::json;

use super::pending::Step;
use crate::program::Call;

thread_local! {
    static CYCLES: RefCell<HashMap<String, Option<i64>>> = RefCell::new(HashMap::new());
}

/// The cycles `call`'s kernel compiles to on `m`; None where it does not.
pub fn cycles(call: &Call, m: &Machine) -> Option<i64> {
    let key = format!("{}|{}|{}", m.digest(), call.gen, call.args);
    if let Some(c) = CYCLES.with(|memo| memo.borrow().get(&key).copied()) {
        return c;
    }
    let got = ktpu_kernels::call::call(&call.gen, &call.args, m)
        .ok()
        .and_then(|k| ktpu_graph::cycles(&k.graph, m).ok());
    CYCLES.with(|memo| memo.borrow_mut().insert(key, got));
    got
}

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
    if crate::mutant::on("api.forms/composed_always") {
        return true;
    }
    if crate::mutant::on("api.forms/composed_never") || mode == Composed::Off {
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

/// The relayout launch making `s`.
pub fn relayout_call(s: &Step) -> Call {
    Call {
        gen: "relayout.relayout_kernel".into(),
        args: json!({"pl": s.made_by()}),
    }
}
