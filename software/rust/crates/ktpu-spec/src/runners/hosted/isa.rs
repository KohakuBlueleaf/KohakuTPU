//! `isa.vector.params`: the names ktpu-l2 owns (the lowering's landing
//! pool, ...) answered by ktpu-l2, the rest by ktpu-isa; with the stack,
//! the runners that compile through it (`isa_stack`).

use ktpu_machine::Machine;
use serde_json::{json, Map, Value};

/// ktpu-isa's ported runners, ktpu-l2's landing pool and, with the stack,
/// the hosted ones.
pub fn isa_runners() -> Vec<&'static str> {
    let mut out = ktpu_isa::spec::runners();
    out.push("isa.vector.links");
    #[cfg(feature = "stack")]
    out.extend(super::isa_stack::RUNNERS);
    out
}

/// ktpu-isa's mutants and ktpu-l2's (a routed name runs under either), and
/// with the stack ktpu-l1's and ktpu-verify's (the hosted runners build and
/// check through them).
pub fn isa_mutants() -> Vec<&'static str> {
    let mut out = ktpu_isa::spec::mutants();
    out.extend(ktpu_l2::spec::mutants());
    #[cfg(feature = "stack")]
    {
        out.extend(ktpu_l1::spec::mutants());
        out.extend(ktpu_verify::spec::mutants());
        out.extend(L0_ROUTES.iter().map(|(n, _)| *n));
    }
    out
}

/// isa mutants whose Python patch is a ktpu-l0 package-builder point, and
/// the ktpu-l0 knob that is that patch.
#[cfg(feature = "stack")]
const L0_ROUTES: &[(&str, &str)] = &[
    ("isa.mover/payloads", "l0.build/mover_swapped"),
    ("isa.mover/posted", "l0.build/never_posted"),
];

/// `f` under `mutant` in every crate with a knob of that name (a name such
/// as `isa.vector/lower` mutates both ktpu-isa and ktpu-l2, as the Python
/// patch does); an error when none has one.
#[cfg(feature = "stack")]
fn under(mutant: Option<&str>, f: impl FnOnce() -> Result<Value, String>) -> Result<Value, String> {
    type Go<'a> = Box<dyn FnOnce() -> Result<Value, String> + 'a>;
    let Some(mu) = mutant else {
        return f();
    };
    let isa = ktpu_isa::mutant::NAMES.iter().find(|n| **n == mu);
    let l1 = ktpu_l1::mutant::NAMES.iter().find(|n| **n == mu);
    let l2 = ktpu_l2::mutant::NAMES.iter().find(|n| **n == mu);
    let verify = ktpu_verify::mutant::NAMES.iter().find(|n| **n == mu);
    let l0 = L0_ROUTES.iter().find(|(n, _)| *n == mu).map(|(_, k)| *k);
    if isa.is_none() && l1.is_none() && l2.is_none() && verify.is_none() && l0.is_none() {
        return Err(format!("no knob for mutant {mu} in the hosted isa runners"));
    }
    let mut go: Go = Box::new(f);
    if let Some(k) = l0 {
        go = Box::new(move || ktpu_l0::mutant::with(k, go));
    }
    if let Some(n) = verify {
        go = Box::new(move || ktpu_verify::mutant::with(n, go));
    }
    if let Some(n) = isa {
        go = Box::new(move || ktpu_isa::mutant::with(n, go));
    }
    if let Some(n) = l1 {
        go = Box::new(move || ktpu_l1::mutant::with(n, go));
    }
    if let Some(n) = l2 {
        let state = ktpu_l2::mutant::State {
            active: Some(n),
            ..ktpu_l2::mutant::State::default()
        };
        go = Box::new(move || ktpu_l2::mutant::scoped(state, go));
    }
    go()
}

/// ktpu-isa's mutants without a knob, less those ktpu-l2 has one for.
pub fn isa_skipped_mutants() -> Vec<(&'static str, &'static str)> {
    let l2 = ktpu_l2::spec::mutants();
    ktpu_isa::spec::skipped_mutants()
        .into_iter()
        .filter(|(n, _)| !l2.contains(n))
        .collect()
}

/// ktpu-isa's suite `run`, with the params ktpu-l2 owns routed there.
pub fn isa_run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    #[cfg(feature = "stack")]
    if super::isa_stack::RUNNERS.contains(&runner) {
        return under(mutant, || super::isa_stack::run(runner, input, machine));
    }
    if runner == "isa.vector.links" {
        let m = machine.ok_or("isa.vector.links needs a machine")?;
        let sends: Vec<i64> = input["sends"]
            .as_array()
            .ok_or("isa.vector.links: no sends")?
            .iter()
            .map(|n| {
                n.as_i64()
                    .ok_or("isa.vector.links: a send is not an integer")
            })
            .collect::<Result<_, _>>()?;
        let l2_mutant = mutant.filter(|mu| ktpu_l2::spec::mutants().contains(mu));
        return ktpu_l2::spec::run_links(&sends, m, l2_mutant);
    }
    if runner != "isa.vector.params" {
        return ktpu_isa::spec::run(runner, input, machine, mutant);
    }
    let m = machine.ok_or("isa.vector.params needs a machine")?;
    let names: Vec<&str> = input["names"]
        .as_array()
        .ok_or("isa.vector.params: no names")?
        .iter()
        .filter_map(Value::as_str)
        .collect();
    let l2_mutant = mutant.filter(|mu| ktpu_l2::spec::mutants().contains(mu));
    let mut out = Map::new();
    let mut isa_names = Vec::new();
    for n in names {
        match ktpu_l2::spec::run_param(n, m, l2_mutant)? {
            Some(v) => {
                out.insert(n.to_string(), v);
            }
            None => isa_names.push(n),
        }
    }
    if !isa_names.is_empty() {
        let isa_mutant = mutant.filter(|mu| ktpu_isa::spec::mutants().contains(mu));
        let got = ktpu_isa::spec::run(runner, &json!({ "names": isa_names }), machine, isa_mutant)?;
        let got = got
            .as_object()
            .ok_or("isa.vector.params answers an object")?;
        out.extend(got.clone());
    }
    Ok(Value::Object(out))
}
