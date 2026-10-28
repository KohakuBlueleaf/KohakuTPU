//! Every field of a plan golden (`golden/plan/*.json`, `program::Case`) is
//! compared somewhere, or is an input the comparison feeds the planner, or
//! carries no behaviour. `golden.rs` reads each case back to the same JSON,
//! so no field is dropped unread. Per field:
//!
//! - `case`, `from`: names. `machine`, `nodes`: inputs (the machine planned for).
//! - `paths`: compared as a set with the Rust planner's (`rows.rs`).
//! - `plans[].uops`: inputs (the tinygrad ast). `plans[].plan`, `.refused`,
//!   `.refused_class`: compared (`lower.rs`).
//! - `runs[].plan`, `.state`: inputs (which ast, the driver's records before
//!   the run). `.steps`, `.end`, `.error`: compared (`run.rs`).
//! - `kp[].op`, `.args`, `.kwargs`: inputs (the op log). `.steps`, `.out`,
//!   `.shape`, `.stat`, `.buf`, `.error`: compared (`tensor.rs`). `.read`:
//!   the digest of the values `numpy()` returned, computed by the offline
//!   device, not planned: judged on the sim and the card.
//! - `rows[].args`: inputs. `.steps`, `.error`: compared (`rows.rs`).
//! - `realized`: the runner's realize boundaries, read by `spec`; checked
//!   here to be in order and within the runs.
//! - `error`: the exception that ended the case; checked here to be one a
//!   compared field already holds.
//!
//! Within steps the comparison drops each graph statement's `where` (the
//! Python or Rust source line that emitted it) and names temporaries by
//! first use (Python's counter runs across the process); a truncating
//! launch's graph is RUL.060's.

mod common;

use common::goldens;
use ktpu_plan::Case;

fn cases() -> Vec<Case> {
    goldens()
        .iter()
        .map(|f| serde_json::from_str(&std::fs::read_to_string(f).unwrap()).unwrap())
        .collect()
}

#[test]
fn every_case_error_is_a_compared_refusal() {
    let mut bad = Vec::new();
    let mut ended = 0;
    for c in cases() {
        let Some(e) = &c.error else { continue };
        ended += 1;
        let held = c
            .plans
            .iter()
            .filter_map(|p| {
                let class = p.refused_class.as_deref()?;
                Some(format!("{class}: {}", p.refused.as_deref()?))
            })
            .chain(c.runs.iter().filter_map(|r| r.error.clone()))
            .chain(c.kp.iter().filter_map(|k| k.error.clone()))
            .chain(c.rows.iter().filter_map(|r| r.error.clone()))
            .any(|x| x == *e);
        if !held {
            bad.push(format!("{}: {e}", c.case));
        }
    }
    eprintln!("{ended} cases end in an error");
    assert!(
        bad.is_empty(),
        "an error no compared field holds:\n{}",
        bad.join("\n")
    );
}

#[test]
fn every_realize_boundary_is_in_order_within_the_runs() {
    let mut bad = Vec::new();
    for c in cases() {
        let ordered = c.realized.windows(2).all(|w| w[0] <= w[1]);
        let within = c.realized.iter().all(|r| *r <= c.runs.len());
        if !(ordered && within) {
            bad.push(format!(
                "{}: {:?} over {} runs",
                c.case,
                c.realized,
                c.runs.len()
            ));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

#[test]
fn values_are_read_only_by_numpy() {
    let mut bad = Vec::new();
    for c in cases() {
        for (i, k) in c.kp.iter().enumerate() {
            if k.read.is_some() && k.op != "numpy" {
                bad.push(format!("{} entry {i}: {} holds values", c.case, k.op));
            }
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}
