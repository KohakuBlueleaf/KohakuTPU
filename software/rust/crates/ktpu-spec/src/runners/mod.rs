//! The suites: each stage crate's `spec` module, one feature a crate.

mod hosted;

use ktpu_machine::Machine;
use serde_json::Value;
use std::collections::BTreeMap;

/// `(runner, input, machine, mutant)` -> the runner's answer.
pub type Run = fn(&str, &Value, Option<&Machine>, Option<&str>) -> Result<Value, String>;

/// One crate's ports: the `spec` module's `runners`, `run`, `mutants` and
/// `skipped_mutants`.
pub struct Suite {
    pub name: &'static str,
    pub runners: fn() -> Vec<&'static str>,
    pub run: Run,
    /// The mutants with a Rust knob.
    pub mutants: fn() -> Vec<&'static str>,
    /// Mutants a ported vector names that have no knob, and why.
    pub skipped_mutants: fn() -> Vec<(&'static str, &'static str)>,
    /// `(vector file stem, input, expect)` -> the input it runs and the
    /// output it must give: the ruled ones where an accepted ruling
    /// supersedes a frozen vector.
    pub ruled: fn(&str, &Value, &Value) -> (Value, Value),
}

/// The vector as written (a suite whose vectors no ruling supersedes).
pub fn as_written(_: &str, input: &Value, expect: &Value) -> (Value, Value) {
    (input.clone(), expect.clone())
}

#[allow(unused_macros)]
macro_rules! suite {
    ($name:literal, $krate:ident) => {
        suite!($name, $krate, as_written)
    };
    ($name:literal, $krate:ident, $ruled:path) => {
        Suite {
            name: $name,
            runners: $krate::spec::runners,
            run: $krate::spec::run,
            mutants: $krate::spec::mutants,
            skipped_mutants: $krate::spec::skipped_mutants,
            ruled: $ruled,
        }
    };
}

/// Every suite this build has.
pub fn suites() -> Vec<Suite> {
    vec![
        #[cfg(all(feature = "isa", feature = "l2"))]
        Suite {
            runners: hosted::isa_runners,
            run: hosted::isa_run,
            mutants: hosted::isa_mutants,
            skipped_mutants: hosted::isa_skipped_mutants,
            ..suite!("isa", ktpu_isa)
        },
        #[cfg(all(feature = "isa", not(feature = "l2")))]
        suite!("isa", ktpu_isa),
        #[cfg(feature = "stack")]
        Suite {
            run: hosted::l0_run,
            ..suite!("l0", ktpu_l0, ktpu_l0::spec::ruled)
        },
        #[cfg(all(feature = "l0", not(feature = "stack")))]
        suite!("l0", ktpu_l0, ktpu_l0::spec::ruled),
        #[cfg(feature = "stack")]
        Suite {
            run: hosted::l1_run,
            mutants: hosted::l1_mutants,
            skipped_mutants: hosted::l1_skipped_mutants,
            ..suite!("l1", ktpu_l1, ktpu_l1::spec::ruled)
        },
        #[cfg(all(feature = "l1", not(feature = "stack")))]
        suite!("l1", ktpu_l1, ktpu_l1::spec::ruled),
        #[cfg(feature = "l2")]
        suite!("l2", ktpu_l2, ktpu_l2::spec::ruled),
        #[cfg(feature = "verify")]
        suite!("verify", ktpu_verify),
        #[cfg(all(feature = "kernels", feature = "stack"))]
        Suite {
            runners: hosted::kernels_runners,
            run: hosted::kernels_run,
            ..suite!("kernels", ktpu_kernels)
        },
        #[cfg(all(feature = "kernels", not(feature = "stack")))]
        suite!("kernels", ktpu_kernels),
        #[cfg(feature = "plan")]
        Suite {
            run: hosted::plan_run,
            mutants: hosted::plan_mutants,
            skipped_mutants: hosted::plan_skipped_mutants,
            ..suite!("plan", ktpu_plan)
        },
        #[cfg(feature = "stack")]
        Suite {
            name: "num",
            runners: hosted::num_runners,
            run: hosted::num_run,
            mutants: hosted::num_mutants,
            skipped_mutants: hosted::num_skipped_mutants,
            ruled: as_written,
        },
    ]
}

/// Each ported runner's suite; a runner ported by two suites is refused.
pub fn by_runner() -> BTreeMap<&'static str, &'static Suite> {
    let suites: &'static [Suite] = Box::leak(suites().into_boxed_slice());
    let mut out = BTreeMap::new();
    for s in suites {
        for r in (s.runners)() {
            if let Some(other) = out.insert(r, s) {
                panic!("runner {r} ported by {} and {}", other.name, s.name);
            }
        }
    }
    out
}
