//! Every recorded run under each `COMPOSED` mode, and the decision itself:
//! composed where the kernel lowers and the cost takes it.

mod common;

use std::sync::OnceLock;

use common::goldens;
use ktpu_kernels::gen::prepared::Composed;
use ktpu_machine::Machine;
use ktpu_plan::program::Call;
use ktpu_plan::run::reads;
use ktpu_plan::{lower, run, Case, Step};
use serde_json::json;

/// Every composed launch of the recorded runs under `mode`, with its machine.
fn composed(mode: Composed) -> Vec<(Machine, Call)> {
    let mut out = Vec::new();
    for f in goldens() {
        let text = std::fs::read_to_string(&f).unwrap();
        let case: Case = serde_json::from_str(&text).unwrap();
        let m = case.machine();
        for r in &case.runs {
            let Ok(l) = lower::plan(&case.plans[r.plan].uops) else {
                continue;
            };
            let mut k = run::Knobs::resolve();
            k.forms.composed = mode;
            let x = run::execute_with(l, &r.state, &m, k).unwrap_or_else(|(e, _)| {
                panic!("{}: refused {e}", f.display());
            });
            for s in &x.steps {
                let Step::Launch(l) = s else { continue };
                let Some(call) = &l.call else { continue };
                if call.args.get("composed").is_some() {
                    out.push((m.clone(), call.clone()));
                }
            }
        }
    }
    out
}

#[test]
fn off_composes_nothing_and_on_composes_kernels_that_lower() {
    assert!(composed(Composed::Off).is_empty());
    let on = composed(Composed::On);
    assert!(on.len() >= 30, "{} composed launches", on.len());
    for (m, call) in &on {
        assert!(reads::cycles(call, m).is_some());
        let kernel = ktpu_kernels::call::call(&call.gen, &call.args, m).unwrap();
        let graph = serde_json::to_string(&kernel.graph).unwrap();
        assert!(graph.contains("read_map"), "{}: no read map", call.gen);
        assert!(!graph.contains("index0"), "{}: a table port", call.gen);
    }
}

/// The costliest and cheapest composed kernels of one machine.
fn big_and_small() -> (Machine, Call, Call) {
    static PAIR: OnceLock<(Machine, Call, Call)> = OnceLock::new();
    PAIR.get_or_init(|| {
        let on = composed(Composed::On);
        let (m, _) = on[0].clone();
        let mut sites: Vec<(i64, Call)> = on
            .into_iter()
            .filter(|(n, _)| n.digest() == m.digest())
            .map(|(n, c)| (reads::cycles(&c, &n).unwrap(), c))
            .collect();
        sites.sort_by_key(|s| s.0);
        let (little, big) = (sites[0].clone(), sites.pop().unwrap());
        assert!(big.0 > 3 * little.0, "{} against {}", big.0, little.0);
        (m, big.1, little.1)
    })
    .clone()
}

#[test]
fn cost_takes_the_form_with_the_fewer_cycles_and_gaps() {
    let (m, big, small) = big_and_small();
    let cost = |c: &Call| reads::cycles(c, &m);
    let made = [small.clone(), small.clone()];
    assert!(reads::composed(
        Composed::Cost,
        &small,
        std::slice::from_ref(&big),
        &m,
        &cost
    ));
    let gapless = m.with_fact("vc.launch_gap", json!(0)).unwrap();
    assert!(!reads::composed(
        Composed::Cost,
        &big,
        &made,
        &gapless,
        &cost
    ));
    assert!(reads::composed(Composed::On, &big, &made, &gapless, &cost));
    assert!(!reads::composed(
        Composed::Off,
        &small,
        std::slice::from_ref(&big),
        &m,
        &cost
    ));
    let took = cost(&big).unwrap();
    let gapped = m.with_fact("vc.launch_gap", json!(took)).unwrap();
    assert!(reads::composed(Composed::Cost, &big, &made, &gapped, &cost));
}

#[test]
fn a_kernel_that_does_not_lower_is_never_composed() {
    let (m, big, small) = big_and_small();
    let cost = |c: &Call| reads::cycles(c, &m);
    let none = Call {
        gen: "prepared.no_such_kernel".into(),
        args: json!({}),
    };
    assert!(!reads::composed(
        Composed::On,
        &none,
        std::slice::from_ref(&big),
        &m,
        &cost
    ));
    assert!(!reads::composed(Composed::Cost, &none, &[big], &m, &cost));
    assert!(reads::composed(Composed::Cost, &small, &[none], &m, &cost));
}

/// The default the Python planner reads is on; its reverse mutant reads
/// materialized.
#[cfg(feature = "mutants")]
#[test]
fn the_default_reads_composed() {
    use ktpu_kernels::gen::prepared::{defaults, MATERIALIZED_MUTANT};
    assert_eq!(defaults().composed, Composed::On);
    let reversed = ktpu_kernels::mutants::with(MATERIALIZED_MUTANT, defaults);
    assert_eq!(reversed.composed, Composed::Off);
}
