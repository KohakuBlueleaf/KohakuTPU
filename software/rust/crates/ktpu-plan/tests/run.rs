//! `run::execute_with` at each golden's knobs (`common::knobs`) against the
//! Python planner (TG.RUN): every recorded run
//! takes the same steps (allocations, forms, tables, writes, casts, launches
//! by generator call, name, graph, kind, info and binds; temporaries matched
//! by order of first use) and leaves the same storage records, or fails the
//! same way. A path not ported refuses by name and fails the test.

mod common;

use std::collections::HashMap;

use common::goldens;
use ktpu_plan::{class, lower, run, Case, Step};
use serde_json::Value;

fn normal(steps: &[Step]) -> Vec<Value> {
    common::normal(steps, true)
}

#[test]
fn every_run_takes_the_python_steps() {
    let (mut same, mut unported, mut bad, mut total) = (0, 0, Vec::new(), 0);
    let mut why: HashMap<String, usize> = HashMap::new();
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        let m = case.machine();
        let k = common::knobs(&case.case);
        for (i, r) in case.runs.iter().enumerate() {
            total += 1;
            let l = lower::plan(&case.plans[r.plan].uops).expect("a run's ast plans");
            let got = run::execute_with(l, &r.state, &m, k);
            let (x, err) = match got {
                Ok(x) => (x, None),
                Err((e, x)) => (x, Some(e)),
            };
            if let Some(e) = &err {
                if e.message.starts_with("not ported: ") {
                    unported += 1;
                    *why.entry(e.message.clone()).or_default() += 1;
                    continue;
                }
            }
            let want = common::want(&r.steps, true);
            let have = normal(&x.steps);
            let bases: Vec<String> = r.end.keys().cloned().collect();
            let ok_err = match (&err, &r.error) {
                (None, None) => true,
                (Some(e), Some(w)) => *w == class::text(e),
                _ => false,
            };
            if have == want && ok_err && x.records(&bases) == r.end {
                same += 1;
                continue;
            }
            #[cfg(feature = "mutants")]
            if err.is_none() && r.error.is_none() && x.records(&bases) == r.end {
                let legacy = ktpu_plan::mutant::with("memory/unsafe-bounds", || {
                    let l = lower::plan(&case.plans[r.plan].uops).unwrap();
                    run::execute_with(l, &r.state, &m, k).unwrap()
                });
                if normal(&legacy.steps) == want {
                    // RUL.020 adds explicit padded operands instead of allocation tails.
                    let allocated = |steps: &[Step]| {
                        steps
                            .iter()
                            .filter_map(|s| match s {
                                Step::Alloc { nbytes, .. } => Some(nbytes),
                                _ => None,
                            })
                            .sum::<i64>()
                    };
                    assert!(allocated(&x.steps) > allocated(&legacy.steps));
                    assert_ne!(
                        have,
                        normal(&legacy.steps),
                        "bounds mutant must reverse the fix"
                    );
                    same += 1;
                    continue;
                }
            }
            let at = have
                .iter()
                .zip(&want)
                .position(|(a, b)| a != b)
                .unwrap_or(have.len().min(want.len()));
            bad.push(format!(
                "{} run {i}: step {at} of {}/{}: {}\n  error {:?} want {:?}",
                case.case,
                have.len(),
                want.len(),
                common::first_diff(
                    have.get(at).unwrap_or(&Value::Null),
                    want.get(at).unwrap_or(&Value::Null)
                ),
                err.as_ref().map(class::text),
                r.error
            ));
        }
    }
    let mut why: Vec<_> = why.into_iter().collect();
    why.sort();
    eprintln!("{same} of {total} runs take the Python steps; {unported} not ported: {why:?}");
    assert!(bad.is_empty(), "{} differ:\n{}", bad.len(), bad.join("\n"));
    assert_eq!(unported, 0, "not ported: {why:?}");
}

#[test]
fn quant_schedule_emission_reverse_witness() {
    let failure = std::panic::catch_unwind(|| {
        ktpu_kernels::mutants::with(
            "kernels.quant/no-schedule",
            every_run_takes_the_python_steps,
        );
    })
    .expect_err("removing Rust quant schedule attributes must fail the recorded-step comparison");
    let message = failure
        .downcast_ref::<String>()
        .expect("assertion failure text");
    assert!(
        message.contains(r#".kw[0][0]: have "pad" want "finite_batch""#),
        "{message}"
    );
    eprintln!("quant schedule reverse witness: recorded-step gate rejects removed emission");
}
