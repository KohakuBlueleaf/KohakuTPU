//! `lower::plan` against the Python planner (TG.PLAN): every ast of the plan
//! goldens plans to the same plan (kind, output, ranges, expression, reads,
//! tables by digest, reductions, product) or refuses with the same words.

use std::path::{Path, PathBuf};

use ktpu_plan::{class, lower, Case};

fn goldens() -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/plan");
    let mut out: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    out.sort();
    out
}

#[test]
fn every_ast_plans_as_python_plans_it() {
    let mut bad = Vec::new();
    let mut total = 0;
    for f in goldens() {
        let case: Case = serde_json::from_str(&std::fs::read_to_string(&f).unwrap()).unwrap();
        for (i, p) in case.plans.iter().enumerate() {
            total += 1;
            let got = lower::plan(&p.uops);
            let ok = match (&got, &p.plan, &p.refused) {
                (Ok(l), Some(want), _) => lower::plan_json(&l.graph, &l.plan) == *want,
                (Err(r), _, Some(msg)) => {
                    r.message == *msg && p.refused_class.as_deref() == Some(class::of(&r.clause))
                }
                _ => false,
            };
            if !ok {
                let why = match &got {
                    Ok(l) => {
                        let have =
                            serde_json::to_string(&lower::plan_json(&l.graph, &l.plan)).unwrap();
                        let want = serde_json::to_string(&p.plan).unwrap();
                        let at = have
                            .bytes()
                            .zip(want.bytes())
                            .position(|(a, b)| a != b)
                            .unwrap_or(0);
                        let lo = at.saturating_sub(80);
                        format!(
                            "\n  have ..{}\n  want ..{}",
                            &have[lo..(at + 120).min(have.len())],
                            &want[lo.min(want.len())..(at + 120).min(want.len())]
                        )
                    }
                    Err(r) => format!(
                        " refused {} (want {:?}: {:?})",
                        class::text(r),
                        p.refused_class,
                        p.refused
                    ),
                };
                bad.push(format!("{} plan {i}:{why}", case.case));
            }
        }
    }
    assert!(total >= 100, "{total} asts");
    assert!(
        bad.is_empty(),
        "{} of {total} differ:\n{}",
        bad.len(),
        bad.join("\n")
    );
}
