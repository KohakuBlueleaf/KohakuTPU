//! How much work the slowest e2e cases take: each case's golden stages
//! (`e2e::stages`, as `golden.rs` runs them) alone in this process, equal to
//! its recorded counted work (`ktpu_l2::work`, `ktpu_verify::work`), which
//! the host's load does not move. The CPU and wall seconds are printed, not
//! held to a budget. The fit memo and the joined totals that keep these
//! counts down are witnessed by their own crates' mutant tests.

#![cfg(feature = "stack")]

mod e2e;

use std::time::Instant;

/// What one case's stages did.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Work {
    /// Band images lowered, images scheduled and encoded for their fit, and
    /// candidate texts timed.
    lowered: u64,
    fitted: u64,
    timed: u64,
    /// Abstract-machine runs and the events they took.
    runs: u64,
    events: u64,
}

impl Work {
    fn now() -> Self {
        let (lowered, fitted, timed) = ktpu_l2::work::counts();
        let (runs, events) = ktpu_verify::work::counts();
        Work {
            lowered,
            fitted,
            timed,
            runs,
            events,
        }
    }

    fn since(self, then: Self) -> Self {
        Work {
            lowered: self.lowered - then.lowered,
            fitted: self.fitted - then.fitted,
            timed: self.timed - then.timed,
            runs: self.runs - then.runs,
            events: self.events - then.events,
        }
    }

    fn named(self) -> [(&'static str, u64); 5] {
        [
            ("lowered", self.lowered),
            ("fitted", self.fitted),
            ("timed", self.timed),
            ("runs", self.runs),
            ("events", self.events),
        ]
    }
}

/// Each case's work, measured alone on the Linux host.
const RECORDED: &[(&str, Work)] = &[
    (
        "examples-l2-ffn4",
        Work {
            lowered: 319,
            fitted: 4,
            timed: 6,
            runs: 9,
            events: 34_893_912,
        },
    ),
    (
        "examples-l2-swiglu4",
        Work {
            lowered: 299,
            fitted: 2,
            timed: 4,
            runs: 9,
            events: 37_943_784,
        },
    ),
    (
        "kernels-lanes-TG.RUN.012.relayout",
        Work {
            lowered: 12_336,
            fitted: 588,
            timed: 882,
            runs: 6,
            events: 1_198_588,
        },
    ),
];

/// The counts of `got` that differ from `want`'s, by name.
fn differ(got: Work, want: Work) -> Vec<String> {
    got.named()
        .into_iter()
        .zip(want.named())
        .filter(|((_, g), (_, w))| g != w)
        .map(|((name, g), (_, w))| format!("{name} {g}, recorded {w}"))
        .collect()
}

/// The counters are process-wide: one case at a time.
static ALONE: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// The case's work, with its CPU and wall seconds, and what differs from its
/// golden.
fn spent(name: &str) -> (Work, f64, f64, Vec<String>) {
    let case = e2e::case(name);
    let m = e2e::machine(&case);
    let (work, cpu, wall) = (Work::now(), e2e::cpu(), Instant::now());
    let steps = e2e::stages(&case, &m);
    let (cpu, wall) = (e2e::cpu() - cpu, wall.elapsed().as_secs_f64());
    let bad = steps.into_iter().filter_map(Result::err).collect();
    (Work::now().since(work), cpu, wall, bad)
}

/// Case `name` agrees with its golden and does exactly its recorded work.
fn works_as_recorded(name: &str) {
    let _alone = ALONE.lock().unwrap_or_else(|e| e.into_inner());
    let want = RECORDED.iter().find(|(n, _)| *n == name).unwrap().1;
    let (work, cpu, wall, bad) = spent(name);
    println!("{name}: {work:?}, {cpu:.1} CPU s, {wall:.1} s wall");
    assert!(bad.is_empty(), "{name} disagrees: {bad:?}");
    let differ = differ(work, want);
    assert!(differ.is_empty(), "{name}: {}", differ.join(", "));
}

#[test]
fn ffn4_works_as_recorded() {
    works_as_recorded("examples-l2-ffn4");
}

#[test]
fn swiglu4_works_as_recorded() {
    works_as_recorded("examples-l2-swiglu4");
}

#[test]
fn relayout_works_as_recorded() {
    works_as_recorded("kernels-lanes-TG.RUN.012.relayout");
}
