//! The stage crates' spec-vector mutants, switched by name for one compile
//! (`_rs.compile(..., mutant=)`): with the `mutants` feature each crate's
//! same-named mutant is switched on for the call; without it the build
//! carries none and asking for one is an error, never a no-op.

/// The `_rs` bindings' own mutants (`python::bindings`): a refusal given
/// for the wrong reason, one a group that refuses.
#[cfg(feature = "mutants")]
pub const BINDINGS: [&str; 13] = [
    "bindings.isa/reason",
    "bindings.cores/reason",
    "bindings.bind/no_cache",
    "bindings.library/no_cache",
    "bindings.recipe/unconditional_admission",
    "bindings.mmu/reuse_handle",
    "bindings.mmu/own_offset",
    "bindings.queue/retires_failed",
    "bindings.poll/cq_only",
    "bindings.program/leaks_cancelled",
    "bindings.submit/python_publication",
    "bindings.capture/drops_tag",
    "bindings.walk/roots_only",
];

/// The Python runtime's mutants (`kohakutpu.application`): switched there by
/// `rs.mutant(name)` and read from `_MUTANT`; no crate carries them.
#[cfg(feature = "mutants")]
pub const RUNTIME: [&str; 17] = [
    "app.arena/home_start_only",
    "app.resident/unset_is_off",
    "app.resident/only_one_resides",
    "app.device/unlatched_resident",
    "app.device/keeps_eager",
    "app.memory/unlatched_carve",
    "app.replay/refuses_unpinned",
    "app.replay/split_workspace",
    "app.sequence/ignores_layout",
    "app.sequence/unlatched",
    "app.schedule/ignores_layout",
    "app.schedule/ignores_context",
    "app.recipe/no_overlap_key",
    "app.recipe/no_host_identity",
    "app.capture/reuses_cold",
    "app.batch/write_skips_flush",
    "app.card/single_by_count",
];

/// Every mutant this build carries (each crate's `NAMES`, in crate order,
/// then this crate's); empty without the `mutants` feature.
pub fn names() -> Vec<&'static str> {
    #[cfg(feature = "mutants")]
    {
        let mut out: Vec<&'static str> = Vec::new();
        for n in ktpu_isa::mutant::NAMES
            .iter()
            .chain(ktpu_machine::mutant::NAMES)
            .chain(ktpu_l0::mutant::NAMES)
            .chain(ktpu_l1::mutant::NAMES)
            .chain(ktpu_l2::mutant::NAMES)
            .chain(ktpu_verify::mutant::NAMES)
            .chain(ktpu_graph::mutant::NAMES)
            .chain(ktpu_kernels::mutants::NAMES)
            .chain(ktpu_plan::spec::mutant::NAMES)
        {
            if !out.contains(n) {
                out.push(n);
            }
        }
        out.push(crate::stack::CHECK_REBUILDS);
        out.extend(BINDINGS);
        out.extend(RUNTIME);
        out.extend(["isa.mover/payloads", "isa.mover/posted"]);
        out.push(crate::stack::BINDS_IGNORED);
        out.push(crate::pipeline::CACHE_ERRORS_REFUSE);
        out.push(crate::RCACHE_IGNORED);
        out.push(crate::stack::lines::OFF);
        out
    }
    #[cfg(not(feature = "mutants"))]
    {
        Vec::new()
    }
}

/// `f()` with mutant `name` on in every crate that carries it, on a pool of
/// one thread (each crate's switch is thread-local, so a search spread over
/// worker threads keeps it). Errs when the build carries no mutants, or none
/// of that name.
pub fn with<T: Send>(name: &str, f: impl FnOnce() -> T + Send) -> Result<T, String> {
    #[cfg(feature = "mutants")]
    {
        let Some(n) = names().into_iter().find(|n| *n == name) else {
            return Err(format!("this build carries no mutant {name:?}"));
        };
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(1)
            .build()
            .map_err(|e| e.to_string())?;
        Ok(pool.install(move || {
            ACTIVE.with(|a| a.set(Some(n)));
            let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| switched(n, f)));
            ACTIVE.with(|a| a.set(None));
            got.unwrap_or_else(|p| std::panic::resume_unwind(p))
        }))
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = f;
        Err(format!(
            "mutant {name:?}: this build of ktpu has no `mutants` feature (a spec-test build carries it)"
        ))
    }
}

#[cfg(feature = "mutants")]
thread_local! {
    static ACTIVE: std::cell::Cell<Option<&'static str>> = const { std::cell::Cell::new(None) };
}

/// The mutant `with` has on for this thread (a stage's cache key carries
/// it, so a mutated output never serves an unmutated compile).
pub fn active() -> Option<&'static str> {
    #[cfg(feature = "mutants")]
    {
        ACTIVE.with(|a| a.get())
    }
    #[cfg(not(feature = "mutants"))]
    {
        None
    }
}

/// `f()` with mutant `n` on, on this thread, in every crate that carries it.
#[cfg(feature = "mutants")]
fn switched<T>(n: &'static str, f: impl FnOnce() -> T) -> T {
    {
        let carries = |list: &[&str]| list.contains(&n);
        let f = move || match n {
            "isa.mover/payloads" => ktpu_l0::mutant::with("l0.build/mover_swapped", f),
            "isa.mover/posted" => ktpu_l0::mutant::with("l0.build/never_posted", f),
            _ => f(),
        };
        let f = move || {
            if carries(ktpu_kernels::mutants::NAMES) {
                ktpu_kernels::mutants::with(n, f)
            } else {
                f()
            }
        };
        let f = move || {
            if carries(ktpu_plan::spec::mutant::NAMES) {
                ktpu_plan::spec::mutant::with(n, f)
            } else {
                f()
            }
        };
        let f = move || {
            if carries(ktpu_graph::mutant::NAMES) {
                ktpu_graph::mutant::with(n, f)
            } else {
                f()
            }
        };
        let f = move || {
            if carries(ktpu_verify::mutant::NAMES) {
                ktpu_verify::mutant::with(n, f)
            } else {
                f()
            }
        };
        let f = move || {
            if carries(ktpu_l2::mutant::NAMES) {
                let state = ktpu_l2::mutant::State {
                    active: Some(n),
                    ..ktpu_l2::mutant::current()
                };
                ktpu_l2::mutant::scoped(state, f)
            } else {
                f()
            }
        };
        let f = move || {
            if carries(ktpu_l1::mutant::NAMES) {
                ktpu_l1::mutant::with(n, f)
            } else {
                f()
            }
        };
        let f = move || {
            if carries(ktpu_l0::mutant::NAMES) {
                ktpu_l0::mutant::with(n, f)
            } else {
                f()
            }
        };
        let f = move || {
            if carries(ktpu_machine::mutant::NAMES) {
                ktpu_machine::mutant::with(n, f)
            } else {
                f()
            }
        };
        if carries(ktpu_isa::mutant::NAMES) {
            ktpu_isa::mutant::with(n, f)
        } else {
            f()
        }
    }
}
