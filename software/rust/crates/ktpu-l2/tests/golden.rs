//! The Python stack's L2 goldens (`software/rust/golden/l2/*.json`, written by
//! `software/rust/golden/l2_dump.py`): each pass's L2 text, the lowering's L1
//! text, a refusal's message and position, byte for byte.
//!
//! `KTPU_GOLDEN=REGEX` narrows the cases; `KTPU_GOLDEN_SCHEDULED=1` also
//! matches the image scheduler's L1 (ktpu-l1's).

use std::path::{Path, PathBuf};

use ktpu_l2::{lower, passes, Program, Search, Strategy, Target, PASSES};
use ktpu_machine::Machine;
use serde_json::Value;

fn golden_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/l2")
}

#[path = "golden/review.rs"]
mod review;

const FILE: &str = "<golden.l2>";

/// A refusal as the golden holds it: `(message, line, col)`.
fn refusal(e: &ktpu_l1::Error) -> (String, i64, i64) {
    (e.message.clone(), e.line as i64, e.col as i64)
}

fn want_refusal(v: &Value) -> (String, i64, i64) {
    (
        v["message"].as_str().unwrap_or("").to_string(),
        v["line"].as_i64().unwrap_or(0),
        v["col"].as_i64().unwrap_or(0),
    )
}

fn first_diff(a: &str, b: &str) -> String {
    for (i, (x, y)) in a.lines().zip(b.lines()).enumerate() {
        if x != y {
            return format!("line {}:\n  want: {x}\n  got:  {y}", i + 1);
        }
    }
    format!(
        "lengths {} vs {} lines",
        a.lines().count(),
        b.lines().count()
    )
}

/// Each ruling `golden/ruled.json` names, with the refusal its cases give.
const RULINGS: &[(&str, &str)] = &[("RUL.030", lower::vector::RUL_030)];

/// Whether `case` is one `golden/ruled.json` lists (the owner's list of
/// goldens an accepted ruling supersedes: the Python stack compiles them),
/// refused at the listed stage with the ruling's refusal, after every earlier
/// pass's text matched.
fn ruled(case: &Value, stage: &str, e: &ktpu_l1::Error) -> bool {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/ruled.json");
    let list: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let name = case["case"].as_str().unwrap_or("");
    list.as_object().unwrap().iter().any(|(rul, cases)| {
        let msg = RULINGS.iter().find(|(r, _)| r == rul).map(|(_, m)| *m);
        cases.get(name).and_then(Value::as_str) == Some(stage)
            && msg.is_some_and(|m| e.message.contains(m))
    })
}

#[test]
fn every_ruling_has_its_refusal() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/ruled.json");
    let list: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    for rul in list
        .as_object()
        .unwrap()
        .keys()
        .filter(|k| k.starts_with("RUL."))
    {
        assert!(
            RULINGS.iter().any(|(r, _)| r == rul),
            "golden/ruled.json names {rul}, which RULINGS gives no refusal"
        );
    }
}

/// The search reads and times each distinct candidate text once: every golden
/// case compiles to the same L1 (or refusal) and the same remembered choices,
/// image and cycles, as with every candidate timed apart
/// (`l2.vector/search-each`), in fewer timings.
#[cfg(feature = "mutants")]
#[test]
fn search_times_each_text_once() {
    use ktpu_l2::mutant::{scoped, State};
    let run = |case: &Value, each: bool| {
        let state = State {
            active: each.then_some("l2.vector/search-each"),
            ..State::default()
        };
        scoped(state, || {
            let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
            let t =
                Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap_or(m.card.nodes as i64));
            let search = Search::new(Strategy::Auto);
            let got = Program::parse(case["l2"].as_str().unwrap(), FILE, case["name"].as_str())
                .and_then(|p| ktpu_l2::compile_with(&p, &t, &PASSES, false, &search));
            let out = match got {
                Ok(c) => c.l1_text,
                Err(e) => format!("refused {e}"),
            };
            (out, search.chosen(), search.timed())
        })
    };
    let mut paths: Vec<PathBuf> = std::fs::read_dir(golden_dir())
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    let (mut once, mut apart, mut cases) = (0, 0, 0);
    for path in &paths {
        let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        if case["l2"].as_str().is_none() {
            continue;
        }
        let (a, b) = (run(&case, false), run(&case, true));
        let name = path.file_stem().unwrap().to_string_lossy().to_string();
        assert_eq!(a.0, b.0, "{name}: L1");
        assert_eq!(a.1, b.1, "{name}: choices");
        assert!(a.2 <= b.2, "{name}: {} timings, {} apart", a.2, b.2);
        once += a.2;
        apart += b.2;
        cases += 1;
    }
    eprintln!("{cases} cases: {once} timings, {apart} with every candidate apart");
    assert!(cases > 150 && once * 2 < apart, "{once} vs {apart}");
}

#[cfg(feature = "witness")]
mod candidate_witness {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    use ktpu_l2::lower::vector::search::witness::{compare, Kind, Mismatch, Reference, Witness};
    use ktpu_l2::mutant::{scoped, State};

    #[test]
    fn repeated_modulo_and_cache_witness() {
        let case: Value = serde_json::from_str(
            &std::fs::read_to_string(golden_dir().join("examples-l2-add.json")).unwrap(),
        )
        .unwrap();
        let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
        let t = Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap());
        let witness = Arc::new(Witness::new(compare));
        let search = Search::new(Strategy::Auto).witnessed(witness.clone());
        let old = Search::new(Strategy::Auto).reference();
        let compile = |s: &Search| {
            Program::parse(case["l2"].as_str().unwrap(), FILE, case["name"].as_str())
                .and_then(|p| ktpu_l2::compile_with(&p, &t, &PASSES, false, s))
                .map(|c| c.l1_text)
                .map_err(|e| e.to_string())
        };
        for again in [false, false, true] {
            scoped(
                State {
                    active: again.then_some("l2.vector/search-again"),
                    ..State::default()
                },
                || {
                    assert_eq!(compile(&search), compile(&old));
                    assert_eq!(search.chosen(), old.chosen());
                },
            );
        }
        let report = witness.report();
        assert!(report.counts()[2] > 0 && report.counts()[3] > 0);
        assert!(report.counts()[0] > 0);
        report.check().unwrap();
    }

    fn same_enumeration(
        a: &ktpu_l2::lower::vector::search::witness::Report,
        b: &ktpu_l2::lower::vector::search::witness::Report,
    ) {
        assert_eq!(a.counts(), b.counts(), "typed versus legacy enumeration");
    }

    #[test]
    fn dropped_typed_candidate_fails_enumeration() {
        let case: Value = serde_json::from_str(
            &std::fs::read_to_string(golden_dir().join("examples-l2-add.json")).unwrap(),
        )
        .unwrap();
        let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
        let t = Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap());
        let reference = Arc::new(Witness::new(compare));
        let typed = Arc::new(Witness::new(compare).dropping_one_candidate());
        for search in [
            Search::new(Strategy::Band)
                .reference()
                .witnessed(reference.clone()),
            Search::new(Strategy::Band).witnessed(typed.clone()),
        ] {
            let p =
                Program::parse(case["l2"].as_str().unwrap(), FILE, case["name"].as_str()).unwrap();
            ktpu_l2::compile_with(&p, &t, &PASSES, false, &search).unwrap();
        }
        let (a, b) = (typed.report(), reference.report());
        a.check().unwrap();
        assert_eq!(a.counts()[1] + 1, b.counts()[1]);
        assert!(std::panic::catch_unwind(|| same_enumeration(&a, &b)).is_err());
        eprintln!(
            "dropped typed candidate: typed={:?} legacy={:?}; count assertion killed mutant",
            a.counts(),
            b.counts()
        );
    }

    fn run(perturb: bool) -> ([usize; 4], usize) {
        let changed = Arc::new(AtomicBool::new(false));
        let mut paths: Vec<_> = std::fs::read_dir(golden_dir())
            .unwrap()
            .map(|p| p.unwrap().path())
            .filter(|p| p.extension().is_some_and(|e| e == "json"))
            .collect();
        paths.sort();
        let mut totals = [0; 4];
        let mut reference_totals = [0; 4];
        let mut failures = 0;
        let mut cases = 0;
        let mut band_refusals = 0;
        let mut encoding_refusals = 0;
        let mut over_imem = 0;
        for path in paths {
            let case: Value =
                serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
            let Some(text) = case["l2"].as_str() else {
                continue;
            };
            cases += 1;
            let changed = changed.clone();
            let comparator =
                move |reference: &Reference, candidate: &[u8]| -> Result<(), Mismatch> {
                    if perturb
                        && matches!(reference.id.kind, Kind::Band(_))
                        && !candidate.is_empty()
                        && !changed.swap(true, Ordering::Relaxed)
                    {
                        let mut candidate = candidate.to_vec();
                        candidate[0] ^= 1;
                        compare(reference, &candidate)
                    } else {
                        compare(reference, candidate)
                    }
                };
            let witness = Arc::new(Witness::new(comparator));
            let search = Search::new(Strategy::Auto).witnessed(witness.clone());
            let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
            let t =
                Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap_or(m.card.nodes as i64));
            // Every invocation searches, even when a probe has a remembered choice.
            let compile = |search: &Search| {
                scoped(
                    State {
                        active: Some("l2.vector/search-again"),
                        ..State::default()
                    },
                    || {
                        Program::parse(text, FILE, case["name"].as_str())
                            .and_then(|p| ktpu_l2::compile_with(&p, &t, &PASSES, false, search))
                            .map(|c| c.l1_text)
                            .map_err(|e| e.to_string())
                    },
                )
            };
            let reference_witness = Arc::new(Witness::new(compare));
            let plain = Search::new(Strategy::Auto)
                .reference()
                .witnessed(reference_witness.clone());
            let expected = compile(&plain);
            assert_eq!(
                compile(&search),
                expected,
                "{}: output or refusal",
                path.display()
            );
            assert_eq!(
                search.chosen(),
                plain.chosen(),
                "{}: choices",
                path.display()
            );
            assert_eq!(search.timed(), plain.timed(), "{}: timings", path.display());
            let report = witness.report();
            let reference_report = reference_witness.report();
            same_enumeration(&report, &reference_report);
            for (total, n) in reference_totals.iter_mut().zip(reference_report.counts()) {
                *total += n;
            }
            band_refusals += report.refused.values().map(Vec::len).sum::<usize>();
            for (id, images) in &report.images {
                if matches!(id.kind, Kind::Modulo { .. }) {
                    let encodings = &report.encoded[id];
                    assert_eq!(
                        images.len(),
                        encodings.len(),
                        "{}: modulo attempts",
                        path.display()
                    );
                    for encoding in encodings {
                        match encoding {
                            Err(_) => encoding_refusals += 1,
                            Ok(words) if *words as i64 > ktpu_l2::target::IMEM_ROOM => {
                                over_imem += 1
                            }
                            _ => {}
                        }
                    }
                }
            }
            for (total, n) in totals.iter_mut().zip(report.counts()) {
                *total += n;
            }
            if let Err(errors) = report.check() {
                failures += errors.len();
                assert!(perturb, "{}: {:?}", path.display(), errors);
                assert!(errors.iter().all(|e| e.byte == 0));
            }
        }
        assert_eq!(changed.load(Ordering::Relaxed), perturb);
        assert_eq!(failures, usize::from(perturb), "byte comparison verdict");
        eprintln!("candidate witness: {cases} cases; probe={} band={} modulo-explicit={} modulo-kernel={}; mismatches={failures}", totals[0], totals[1], totals[2], totals[3]);
        eprintln!("candidate witness rejects: band/probe={band_refusals} encoder={encoding_refusals} over-IMEM={over_imem}");
        assert!(
            cases > 0 && totals.iter().all(|&n| n > 0),
            "nonempty corpus and every candidate kind"
        );
        assert_eq!(totals, reference_totals, "typed versus legacy enumeration");
        eprintln!(
            "legacy enumeration: probe={} band={} modulo-explicit={} modulo-kernel={}",
            reference_totals[0], reference_totals[1], reference_totals[2], reference_totals[3]
        );
        (totals, failures)
    }

    #[test]
    #[ignore = "candidate witness: cargo test -p ktpu-l2 --features witness -- --ignored candidate_witness"]
    fn every_candidate_is_compared() {
        assert_eq!(run(false).1, 0);
    }

    #[test]
    #[ignore = "candidate witness: cargo test -p ktpu-l2 --features witness -- --ignored candidate_witness"]
    fn one_changed_byte_fails_the_witness() {
        assert_eq!(run(true).1, 1);
    }
}

/// One case: `Ok(())` or every mismatch found.
fn run_case(case: &Value, scheduled: bool) -> Result<(), String> {
    run_case_review(case, scheduled, None)
}

fn run_case_review(
    case: &Value,
    scheduled: bool,
    review: Option<(&review::Review, &Path)>,
) -> Result<(), String> {
    let l2 = case["l2"].as_str().ok_or("no l2 text")?;
    let m = Machine::named(case["machine"].as_str().unwrap()).map_err(|e| e.to_string())?;
    let t = Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap_or(m.card.nodes as i64));
    let name = case["name"].as_str();
    let want_ref = case.get("refusal").filter(|r| !r.is_null());
    let stage = want_ref.and_then(|r| r["stage"].as_str()).unwrap_or("");
    let p = match Program::parse(l2, FILE, name) {
        Ok(p) => p,
        Err(e) => {
            if stage == "read" && refusal(&e) == want_refusal(want_ref.unwrap()) {
                return Ok(());
            }
            return Err(format!("read: {e}"));
        }
    };
    let mut p = p.with_vregs(t.vregs, t.vcopies);
    let search = Search::new(Strategy::Auto);
    let want_passes = case["passes"].as_array().cloned().unwrap_or_default();
    for (k, name) in PASSES.iter().enumerate() {
        match passes::pass(name, &p, &t, &search) {
            Ok(q) => {
                let Some(w) = want_passes.get(k) else {
                    return Err(format!(
                        "{name}: passed, the golden refuses ({stage}): {:?}",
                        want_ref.map(want_refusal)
                    ));
                };
                let want = w["text"].as_str().unwrap();
                if q.text() != want {
                    return Err(format!(
                        "{name}: text differs, {}",
                        first_diff(want, q.text())
                    ));
                }
                p = q;
            }
            Err(e) => {
                if review.is_some_and(|(r, _)| r.selected(case)) {
                    return Err(format!("REGOLD refuses pass failure: {e}"));
                }
                if ruled(case, name, &e) {
                    return Ok(());
                }
                if stage == *name && refusal(&e) == want_refusal(want_ref.unwrap()) {
                    return Ok(());
                }
                return Err(format!(
                    "{name}: refused {:?}; golden {:?}",
                    refusal(&e),
                    want_ref.map(want_refusal)
                ));
            }
        }
    }
    let search = Search::new(Strategy::Auto);
    let compared;
    match lower::lower(&p, &t, &search) {
        Ok(text) => {
            let Some(want) = case["l1"].as_str() else {
                return Err(format!(
                    "lower: lowered, the golden refuses ({stage}): {:?}",
                    want_ref.map(want_refusal)
                ));
            };
            if text != want && review.is_none() {
                return Err(format!("lower: L1 differs, {}", first_diff(want, &text)));
            }
            if review.is_some() {
                review::Review::report(case, "l1", &text);
            }
            let mut fields = vec![("l1", text.clone())];
            if scheduled {
                if let Some(ws) = case["l1_scheduled"].as_str() {
                    let read = ktpu_l1::text::read(&text, &format!("{FILE}:{}.l1", p.name))
                        .map_err(|e| e.to_string())?;
                    let got =
                        ktpu_l1::sched::schedule_module(&read, &m).map_err(|e| e.to_string())?;
                    if got.source != ws && review.is_none() {
                        return Err(format!(
                            "schedule: differs, {}",
                            first_diff(ws, &got.source)
                        ));
                    }
                    if review.is_some() {
                        review::Review::report(case, "l1_scheduled", &got.source);
                    }
                    fields.push(("l1_scheduled", got.source));
                }
            }
            compared = fields;
        }
        Err(e) => {
            if review.is_some_and(|(r, _)| r.selected(case)) {
                return Err(format!("REGOLD refuses lowering failure: {e}"));
            }
            if ruled(case, "lower", &e) {
                return Ok(());
            }
            if stage == "lower" && refusal(&e) == want_refusal(want_ref.unwrap()) {
                return Ok(());
            }
            return Err(format!(
                "lower: refused {:?}; golden {:?}",
                refusal(&e),
                want_ref.map(want_refusal)
            ));
        }
    }
    if let Some(v) = case.get("variant").filter(|v| !v.is_null()) {
        let ps: Vec<String> = v["passes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().to_string())
            .collect();
        let ps: Vec<&str> = ps.iter().map(String::as_str).collect();
        let strategy = Strategy::parse(v["strategy"].as_str().unwrap()).unwrap();
        let p0 = Program::parse(l2, FILE, name)
            .unwrap()
            .with_vregs(t.vregs, t.vcopies);
        let got = passes::run(&p0, &ps, &t, &Search::new(strategy))
            .and_then(|q| lower::lower(&q, &t, &Search::new(strategy)));
        match (got, v.get("l1").and_then(Value::as_str), v.get("refusal")) {
            (Ok(text), Some(want), _) if text == want => {}
            (Ok(text), Some(want), _) => {
                return Err(format!("variant: L1 differs, {}", first_diff(want, &text)))
            }
            (Err(e), _, Some(r)) if refusal(&e) == want_refusal(r) => {}
            (got, _, r) => {
                return Err(format!(
                    "variant: {:?}; golden {:?}",
                    got.map(|_| ()).map_err(|e| refusal(&e)),
                    r
                ))
            }
        }
    }
    if let Some((r, path)) = review {
        r.compare(path, case, compared, &m)?;
    }
    Ok(())
}

#[test]
fn goldens() {
    // With mutants: every text read, each search candidate's image included,
    // by both L1 grammars, any difference a panic (ktpu-l1 `read_both`).
    #[cfg(feature = "mutants")]
    ktpu_l1::text::parse::read_both();
    let only = std::env::var("KTPU_GOLDEN").unwrap_or_default();
    let scheduled = std::env::var("KTPU_GOLDEN_SCHEDULED").is_ok();
    let mut paths: Vec<PathBuf> = std::fs::read_dir(golden_dir())
        .expect("software/rust/golden/l2: run golden/l2_dump.py")
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().map(|x| x == "json").unwrap_or(false))
        .collect();
    paths.sort();
    let review = review::Review::environment(&paths);
    assert!(
        review.is_none() || only.is_empty(),
        "regold audits every case; use KTPU_L2_CASES only to authorize writes"
    );
    let (mut pass, mut skipped) = (0, 0);
    let mut fails: Vec<String> = Vec::new();
    for path in &paths {
        let name = path.file_stem().unwrap().to_string_lossy().to_string();
        if !only.is_empty() && !name.contains(&only) {
            continue;
        }
        let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let traced_refusal = case["refusal"]["stage"].as_str() == Some("trace");
        if case.get("skipped").is_some() || traced_refusal {
            if review.as_ref().is_some_and(|r| r.selected(&case)) {
                fails.push(format!("{name}: REGOLD refuses skipped/trace-refused case"));
            }
            skipped += 1;
            continue;
        }
        match run_case_review(
            &case,
            scheduled,
            review.as_ref().map(|r| (r, path.as_path())),
        ) {
            Ok(()) => pass += 1,
            Err(e) => fails.push(format!("{name}: {e}")),
        }
    }
    eprintln!(
        "l2 goldens: {pass} pass, {} fail, {skipped} skipped",
        fails.len()
    );
    for f in &fails {
        eprintln!("FAIL {f}");
    }
    assert!(fails.is_empty(), "{} golden cases differ", fails.len());
}

/// The e2e goldens (`golden/e2e`, `dump.py`): `compile_l1` with one search
/// for the passes and the lowering, then the image scheduler, against the
/// Python L1. Run with `KTPU_E2E=REGEX`.
#[test]
fn e2e_l1() {
    let Ok(only) = std::env::var("KTPU_E2E") else {
        return;
    };
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/e2e");
    let mut paths: Vec<PathBuf> = std::fs::read_dir(dir)
        .unwrap()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().map(|x| x == "json").unwrap_or(false))
        .filter(|p| p.to_string_lossy().contains(&only))
        .collect();
    paths.sort();
    let (mut pass, mut fails) = (0, Vec::new());
    for path in &paths {
        let case: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let (Some(l2), Some(want)) = (case["l2"].as_str(), case["l1"].as_str()) else {
            continue;
        };
        let m = Machine::named(case["machine"].as_str().unwrap()).unwrap();
        let t = Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap_or(4));
        let p = Program::parse(l2, "<golden.l2>", case["name"].as_str()).unwrap();
        let got = ktpu_l2::compile_with(&p, &t, &PASSES, true, &Search::default());
        let name = path.file_stem().unwrap().to_string_lossy().to_string();
        if std::env::var("KTPU_E2E_SHOW").is_ok() {
            if let Ok(c) = &got {
                let fresh = lower::lower(&c.l2, &t, &Search::default()).unwrap();
                eprintln!("--- shared\n{}--- fresh\n{fresh}", c.l1_text);
            }
        }
        match got {
            Ok(c) if c.l1.source == want => pass += 1,
            Ok(c) => fails.push(format!(
                "{name}: scheduled L1 differs, {}",
                first_diff(want, &c.l1.source)
            )),
            Err(e) => fails.push(format!("{name}: refused {e}")),
        }
    }
    eprintln!("e2e: {pass} pass, {} fail", fails.len());
    for f in &fails {
        eprintln!("FAIL {f}");
    }
    assert!(fails.is_empty());
}

fn compile_example(stem: &str) -> Result<ktpu_l2::Compiled, ktpu_l1::Error> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../language/kohakutpu/language/examples/l2/multinode")
        .join(format!("{stem}.ktpu"));
    let text = std::fs::read_to_string(path).unwrap();
    let m = Machine::named("multimesh_v9t6").unwrap();
    let p = Program::parse(&text, "<golden.l2>", Some(stem)).unwrap();
    ktpu_l2::compile_l1(&p, &m, &PASSES, false, &Search::default())
}

/// The four-node examples name mg[0..4]; multimesh_v9t6 has two clusters a
/// node, so the shared fills' grouping refuses, by clause, and never panics.
#[test]
#[allow(non_snake_case)]
fn L2_MG_005_share_past_the_clusters_refuses() {
    for stem in ["ffn4", "swiglu4"] {
        let e = compile_example(stem).unwrap_err();
        assert_eq!(e.clause, "L2.MG.005", "{stem}: {e}");
        assert!(
            e.message.contains("mg[2] on a node of 2 clusters"),
            "{stem}: {e}"
        );
    }
}

/// One spec vector's case, by the clause it witnesses.
fn clause(stem: &str) {
    let path = golden_dir().join(format!("{stem}.json"));
    let case: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    if let Err(e) = run_case(&case, false) {
        panic!("{stem}: {e}");
    }
}

macro_rules! clauses {
    ($($name:ident => $stem:literal,)*) => {
        $(
            #[test]
            #[allow(non_snake_case)]
            fn $name() {
                clause($stem)
            }
        )*
    };
}

clauses! {
    L2_BODY_001_scratch_first => "l2-body-L2.BODY.001.scratch_first",
    L2_BODY_002_folded_loop_kept => "l2-body-L2.BODY.002.folded_loop_kept",
    L2_BODY_003_par_units => "l2-body-L2.BODY.003.par_units",
    L2_BODY_004_iterations_in_order => "l2-body-L2.BODY.004.iterations_in_order",
    L2_BODY_006_sticky_in_loop => "l2-body-L2.BODY.006.sticky_in_loop",
    L2_BODY_007_units_in_nodes => "l2-body-L2.BODY.007.units_in_nodes",
    L2_BODY_008_values_effects_types => "l2-body-L2.BODY.008.values_effects_types",
    L2_BODY_009_stages => "l2-body-L2.BODY.009.stages",
    L2_BODY_013_pipe_k => "l2-body-L2.BODY.013.pipe_k",
    L2_BODY_019_node_phase => "l2-body-L2.BODY.019.node_phase",
    L2_LOW_001_text_shape => "l2-low-L2.LOW.001.text_shape",
    L2_LOW_003_shared_image => "l2-low-L2.LOW.003.shared_image",
    L2_LOW_004_merged_streams => "l2-low-L2.LOW.004.merged_streams",
    L2_LOW_005_cluster_bank => "l2-low-L2.LOW.005.cluster_bank",
    L2_LOW_011_silu_card => "l2-low-L2.LOW.011.silu_card",
    L2_MG_002_fill_runs => "l2-mg-L2.MG.002.fill_runs",
    L2_MG_004_halves => "l2-mg-L2.MG.004.halves",
    L2_MG_004_whole => "l2-mg-L2.MG.004.whole",
    L2_MG_005_share_cross => "l2-mg-L2.MG.005.share_cross",
    L2_MG_010_fused_drains => "l2-mg-L2.MG.010.fused_drains",
    L2_MG_013_split_k_add => "l2-mg-L2.MG.013.split_k_add",
    L2_MOV_001_node_ops => "l2-mov-L2.MOV.001.node_ops",
    L2_MOV_002_word_walk => "l2-mov-L2.MOV.002.word_walk",
    L2_MOV_003_copy_card => "l2-mov-L2.MOV.003.copy_card",
    L2_MOV_004_signed_walk => "l2-mov-L2.MOV.004.signed_walk",
    L2_MOV_005_fill_immediate => "l2-mov-L2.MOV.005.fill_immediate",
    L2_MOV_006_gather_card => "l2-mov-L2.MOV.006.gather_card",
    L2_MOV_006_gather_rows => "l2-mov-L2.MOV.006.gather_rows",
    L2_MOV_007_index_view => "l2-mov-L2.MOV.007.index_view",
    L2_MOV_008_cut_levels => "l2-mov-L2.MOV.008.cut_levels",
    L2_MOV_008_cut_words => "l2-mov-L2.MOV.008.cut_words",
    L2_MOV_010_quant_and_gt4 => "l2-mov-L2.MOV.010.quant_and_gt4",
    L2_MOV_011_gt4_walk => "l2-mov-L2.MOV.011.gt4_walk",
    L2_MOV_013_perm => "l2-mov-L2.MOV.013.perm",
    L2_MOV_014_scatter_rows => "l2-mov-L2.MOV.014.scatter_rows",
    L2_OP_001_one_instruction_a_register => "l2-op-L2.OP.001.one_instruction_a_register",
    L2_OP_002_sub_fma_exp2d_fnma => "l2-op-L2.OP.002.sub_fma_exp2d_fnma",
    L2_OP_004_band_broadcast => "l2-op-L2.OP.004.band_broadcast",
    L2_OP_004_row_broadcast => "l2-op-L2.OP.004.row_broadcast",
    L2_OP_005_compare_mask => "l2-op-L2.OP.005.compare_mask",
    L2_OP_006_vsel => "l2-op-L2.OP.006.vsel",
    L2_OP_007_stat_band => "l2-op-L2.OP.007.stat_band",
    L2_OP_009_exact_card => "l2-op-L2.OP.009.exact_card",
    L2_OP_009_xlane => "l2-op-L2.OP.009.xlane",
    L2_OP_010_exact_card => "l2-op-L2.OP.010.exact_card",
    L2_OP_010_xrot => "l2-op-L2.OP.010.xrot",
    L2_OP_011_merge_tree => "l2-op-L2.OP.011.merge_tree",
    L2_OP_012_column_blocks => "l2-op-L2.OP.012.column_blocks",
    L2_OP_012_gt4_fold => "l2-op-L2.OP.012.gt4_fold",
    L2_OP_013_square_folded => "l2-op-L2.OP.013.square_folded",
    L2_OP_014_unpack_pack_modes => "l2-op-L2.OP.014.unpack_pack_modes",
    L2_OP_015_quantiser_order => "l2-op-L2.OP.015.quantiser_order",
    L2_ORDER_001_after_par => "l2-order-L2.ORDER.001.after_par",
    L2_ORDER_002_disjoint_rows => "l2-order-L2.ORDER.002.disjoint_rows",
    L2_ORDER_002_meeting_rows => "l2-order-L2.ORDER.002.meeting_rows",
    L2_ORDER_005_last_move_plain => "l2-order-L2.ORDER.005.last_move_plain",
    L2_ORDER_005_posted_then_barrier_golden => "l2-order-L2.ORDER.005.posted_then_barrier",
    L2_ORDER_008_one_phase_unordered => "l2-order-L2.ORDER.008.one_phase_unordered",
    L2_PASS_002_bitwise_card => "l2-pass-L2.PASS.002.bitwise_card",
    L2_PASS_002_leaky_max => "l2-pass-L2.PASS.002.leaky_max",
    L2_PASS_009_retile => "l2-pass-L2.PASS.009.retile",
    L2_PASS_011_passes_keep_values_card => "l2-pass-L2.PASS.011.passes_keep_values_card",
    RUL_030_no_band_subtile_pack => "l2-rulings-RUL.030.no_band_subtile_pack",
    L2_SEARCH_001_fastest_kept => "l2-search-L2.SEARCH.001.fastest_kept",
    L2_SPACE_001_dram_dtypes => "l2-space-L2.SPACE.001.dram_dtypes",
    L2_SPACE_002_packings_golden => "l2-space-L2.SPACE.002.packings",
    L2_SPACE_004_acc_4096 => "l2-space-L2.SPACE.004.acc_4096",
    L2_SPACE_005_l1_alone_512 => "l2-space-L2.SPACE.005.l1_alone_512",
    L2_SPACE_006_reg_gt4 => "l2-space-L2.SPACE.006.reg_gt4",
    L2_SPACE_006_reg_rows => "l2-space-L2.SPACE.006.reg_rows",
    L2_SPACE_010_f32_buffer => "l2-space-L2.SPACE.010.f32_buffer",
    L2_STAT_001_loaded_stat => "l2-stat-L2.STAT.001.loaded_stat",
    L2_STAT_003_sixteen_a_word => "l2-stat-L2.STAT.003.sixteen_a_word",
    L2_VC_001_pre_then_loop => "l2-vc-L2.VC.001.pre_then_loop",
    L2_VC_003_gt4_tile_bands => "l2-vc-L2.VC.003.gt4_tile_bands",
    L2_VC_003_row_view => "l2-vc-L2.VC.003.row_view",
    L2_VC_003_rowmajor_bands => "l2-vc-L2.VC.003.rowmajor_bands",
    L2_VC_004_affine_views => "l2-vc-L2.VC.004.affine_views",
    L2_VC_007_packed_once => "l2-vc-L2.VC.007.packed_once",
    L2_VC_012_scalar_constants => "l2-vc-L2.VC.012.scalar_constants",
    L2_VC_013_band_order => "l2-vc-L2.VC.013.band_order",
    L2_VIEW_001_runs_and_wholes_golden => "l2-view-L2.VIEW.001.runs_and_wholes",
}
