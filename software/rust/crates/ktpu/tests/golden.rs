//! The Rust stack against the Python stack's end-to-end goldens
//! (`software/rust/golden/e2e/*.json`, written by `golden/dump.py`), stage
//! by stage: graph -> L2 text (the tracer's), L2 -> L1 text (passes,
//! lowering, image schedule), L1 -> each node's relocatable package bytes and
//! the buffers' parameters, and the verdict's refusal and finding kinds;
//! where Python refuses at a stage, a refusal there. A stage not wired yet
//! (`STACK.PORT`) is counted, not failed; every stage that runs must agree.
//! A panic is its case's finding. Cases run in parallel.

#![cfg(feature = "stack")]

mod line_layout;

use ktpu::stack::{CardStack, NOT_PORTED};
use ktpu::{Backend, Stack};
use ktpu_machine::{Machine, Refusal};
use rayon::prelude::*;
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::Path;
use std::time::Instant;

fn goldens() -> Vec<(String, Value)> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/e2e");
    let mut out: Vec<(String, Value)> = std::fs::read_dir(&dir)
        .expect("golden/e2e: run golden/dump.py")
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .map(|p| {
            let v = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
            (p.file_stem().unwrap().to_string_lossy().into_owned(), v)
        })
        .collect();
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
}

/// The parameter each buffer of a golden package binds (its bindings are
/// the addresses `binds` gave the parameters).
fn params(case: &Value, pkg: &Value) -> Vec<String> {
    let binds = case["binds"].as_object().unwrap();
    pkg["bindings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| {
            binds
                .iter()
                .find(|(_, v)| *v == a)
                .map_or_else(|| format!("@{a}"), |(k, _)| k.clone())
        })
        .collect()
}

fn sorted(mut k: Vec<String>) -> Vec<String> {
    k.sort();
    k
}

fn first_difference(got: &str, want: &str) -> String {
    let at = got
        .lines()
        .zip(want.lines())
        .position(|(a, b)| a != b)
        .unwrap_or(got.lines().count().min(want.lines().count()));
    format!(
        "from line {}: got {:?}, want {:?}",
        at + 1,
        got.lines().nth(at),
        want.lines().nth(at)
    )
}

/// The RUL.063 schedule of a case whose Python schedule the ruling changes
/// (`golden/sched/e2e/<case>.json`: `rulings`, `scheduled_ruled`).
fn ruled_schedule(case: &Value) -> Option<String> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/sched/e2e");
    let text =
        std::fs::read_to_string(dir.join(format!("{}.json", case["case"].as_str()?))).ok()?;
    let s: Value = serde_json::from_str(&text).ok()?;
    let ruled = s["scheduled_ruled"].as_str()?;
    let tagged = s["rulings"].as_array()?.iter().any(|r| r == "RUL.063");
    (tagged && Some(ruled) != s["scheduled"].as_str()).then(|| ruled.to_string())
}

/// Each node's package and buffer parameters against the reference's
/// relocatable bytes; true where the reference's signature word is 0 and the
/// machine's (RUL.041) stands in for it.
fn same_packages(
    case: &Value,
    m: &Machine,
    got: &BTreeMap<u32, ktpu::PackageOut>,
    want: &Value,
) -> Result<bool, String> {
    let mut ruled_signature = false;
    let want = want.as_object().ok_or("no reference packages")?;
    if want.len() != got.len() {
        return Err(format!("{} packages, python {}", got.len(), want.len()));
    }
    for (mesh, p) in want {
        let mesh_n = mesh.parse::<u32>().unwrap();
        let g = &got[&mesh_n];
        let got = ktpu::pipeline::cache::hex(&g.bytes);
        let mut want = p["relocatable"].as_str().unwrap().to_string();
        // The header's signature word (bytes 8..16) is the machine's
        // (RUL.041); the Python builder writes 0.
        let f = ktpu_l0::Facts::of_mesh(m, mesh_n).map_err(|e| e.to_string())?;
        let units = ktpu_l0::units::machine_units(&f).map_err(|e| e.to_string())?;
        let sig = ktpu_l0::format::signature(&units);
        let word = |s: &str| u64::from_str_radix(&s[16..32], 16).map(u64::swap_bytes);
        if got.len() < 32 || word(&got) != Ok(sig) {
            return Err(format!(
                "mesh {mesh}: the signature word is not the machine's {sig:#x} (RUL.041)"
            ));
        }
        if want.len() >= 32 && word(&want) == Ok(0) {
            want.replace_range(16..32, &got[16..32]);
            ruled_signature = true;
        }
        if got != want {
            let at = got
                .bytes()
                .zip(want.bytes())
                .position(|(a, b)| a != b)
                .unwrap_or(got.len().min(want.len()))
                / 2;
            let near =
                |s: &str| s[(at * 2).saturating_sub(16)..(at * 2 + 16).min(s.len())].to_string();
            return Err(format!(
                "mesh {mesh}: package bytes differ at byte {at} (lengths {} / {}): got ..{}.., want ..{}..",
                got.len() / 2,
                want.len() / 2,
                near(&got),
                near(&want)
            ));
        }
        if g.params != params(case, p) {
            return Err(format!("mesh {mesh}: buffer params differ"));
        }
    }
    Ok(ruled_signature)
}

/// The finding kinds and the refusal against the reference verdict.
fn same_verdict(found: &[ktpu::Finding], v: &Value) -> Result<(), String> {
    let got = sorted(found.iter().map(|f| f.kind.clone()).collect());
    let want = sorted(
        v["findings"]
            .as_array()
            .ok_or("no reference verdict")?
            .iter()
            .map(|f| f[0].as_str().unwrap().to_string())
            .collect(),
    );
    let refused = found.iter().any(|f| Stack.refuses(f));
    match refused == v["refused"].as_bool().unwrap() && got == want {
        true => Ok(()),
        false => Err(format!(
            "verdict {got:?} (refused {refused}), reference {want:?} ({})",
            v["refused"]
        )),
    }
}

/// Every `(ruling, case, stage)` `golden/ruled.json` lists: an accepted
/// ruling makes the Rust stack refuse the case at that stage.
fn listings() -> &'static [(String, String, String)] {
    static RULED: std::sync::OnceLock<Vec<(String, String, String)>> = std::sync::OnceLock::new();
    RULED.get_or_init(|| {
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/ruled.json");
        let all: Value =
            serde_json::from_str(&std::fs::read_to_string(path).expect("golden/ruled.json"))
                .unwrap();
        let mut out = Vec::new();
        for (ruling, cases) in all.as_object().unwrap() {
            for (case, stage) in cases.as_object().into_iter().flatten() {
                out.push((
                    ruling.clone(),
                    case.clone(),
                    stage.as_str().unwrap().to_string(),
                ));
            }
        }
        out
    })
}

/// `(ruling, stage)` where `golden/ruled.json` lists the case.
fn ruled_refusal(case: &Value) -> Option<(&'static str, &'static str)> {
    let name = case["case"].as_str()?;
    listings()
        .iter()
        .find(|(_, c, _)| c == name)
        .map(|(r, _, s)| (leak(r), leak(s)))
}

/// Every listing names an e2e case, and each listed case run here ended in
/// its ruled line or as a ruling vector (ktpu-spec checks it).
fn listings_hold(names: &[String], results: &[(String, f64, Vec<Step>)]) -> Vec<String> {
    let mut bad = Vec::new();
    for (ruling, case, stage) in listings() {
        if !names.contains(case) {
            bad.push(format!(
                "ruled.json lists {case} under {ruling}: no such e2e case"
            ));
            continue;
        }
        let Some((_, _, steps)) = results.iter().find(|(n, _, _)| n == case) else {
            continue;
        };
        let ruled = ruled_line(ruling, stage);
        let ended = steps
            .iter()
            .any(|s| matches!(s, Ok(t) if *t == ruled || t.starts_with("ruling vector")));
        if !ended {
            bad.push(format!(
                "{case}: listed under {ruling} at {stage}, never refused there"
            ));
        }
    }
    bad
}

fn leak(s: &str) -> &'static str {
    Box::leak(s.to_string().into_boxed_str())
}

/// Whether refusal `e` is the one `ruling` makes: its clause, or for
/// RUL.030 the statistic pack's refusal under the pass that hit it.
fn refuses_as(ruling: &str, e: &Refusal) -> bool {
    e.clause == ruling
        || (ruling == "RUL.030" && e.message.contains(ktpu_l2::lower::vector::RUL_030))
}

fn ruled_line(ruling: &str, stage: &str) -> &'static str {
    leak(&format!("refused at {stage} as the ruling says [{ruling}]"))
}

/// One stage's result for a case: `Ok(tag)` or `Err(why)`.
type Step = Result<&'static str, String>;

/// Every stage of one case, in order, until one disagrees or is not wired.
fn stages(case: &Value, m: &Machine) -> Vec<Step> {
    if case["case"] == line_layout::CASE {
        return vec![
            line_layout::check(case, m).map(|()| "reviewed L1.VER.017 line-layout overlay")
        ];
    }
    let b = Stack;
    let mut out = Vec::new();
    let python_stage = case["refusal"]["stage"].as_str();
    // graph -> L2
    if let (Some(g), Some(want)) = (
        case.get("graph").filter(|g| !g.is_null()),
        case["l2"].as_str(),
    ) {
        out.push(match b.graph_to_l2(&g.to_string(), m) {
            Ok(p) if b.print_l2(&p) == want => Ok("graph -> L2 agree"),
            Ok(p) => Err(format!(
                "graph -> L2 {}",
                first_difference(&b.print_l2(&p), want)
            )),
            Err(e) => Err(format!("graph -> L2 refused: {e}")),
        });
    }
    let Some(l2) = case["l2"].as_str() else {
        out.push(Ok("python refused at trace"));
        return out;
    };
    // A ruling vector's case: Python does not follow the ruling, so its
    // output is no reference; ktpu-spec checks the vector's ruled expect.
    let vectors = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/spec/vectors");
    let ruling_vector = case["from"].as_array().is_some_and(|f| {
        f.iter().filter_map(Value::as_str).any(|id| {
            std::fs::read_to_string(vectors.join(format!("{id}.json")))
                .ok()
                .and_then(|t| serde_json::from_str::<Value>(&t).ok())
                .is_some_and(|v| v["status"] == "ruling")
        })
    });
    if ruling_vector {
        out.push(Ok(
            "ruling vector: checked by ktpu-spec against its ruled expect",
        ));
        return out;
    }
    // L2 -> L1
    let l1 = (|| {
        let p = b.parse_l2(l2)?;
        let name = case["name"]
            .as_str()
            .map_or_else(|| p.name.clone(), str::to_string);
        let l1 = b.schedule(&b.lower(&b.passes(&p, &name, m)?, &name, m)?, m)?;
        Ok::<_, Refusal>((name, l1))
    })();
    // Where RUL.063 changes the image schedule, the reference is the ruled
    // schedule (golden/sched/e2e, an independent emulation), not Python's.
    let ruled = ruled_schedule(case);
    // A case an accepted ruling refuses (golden/ruled.json): the reference
    // is that refusal at its stage, not Python's output.
    let refused = ruled_refusal(case);
    if let Some((ruling, stage)) = refused.filter(|(_, s)| *s != "order") {
        out.push(match l1 {
            Err(e) if refuses_as(ruling, &e) => Ok(ruled_line(ruling, stage)),
            Err(e) => Err(format!(
                "refused at {stage}, but not as {ruling} rules: {e}"
            )),
            Ok(_) => Err(format!("compiled, {ruling} refuses it at {stage}")),
        });
        return out;
    }
    let python_l1 = case["l1"].as_str();
    let want_l1 = ruled.as_deref().or(python_l1);
    let (name, l1) = match (l1, want_l1) {
        (Ok((name, l1)), Some(want)) if b.print_l1(&l1) == want => {
            out.push(Ok(match ruled.is_some() {
                true => "L2 -> L1 agree [RUL.063: the ruled schedule]",
                false => "L2 -> L1 agree",
            }));
            (name, l1)
        }
        (Ok((_, l1)), Some(want)) => {
            out.push(Err(format!(
                "L1 {}",
                first_difference(&b.print_l1(&l1), want)
            )));
            return out;
        }
        (Ok(_), None) => {
            out.push(Err(format!("L1 made, python refused: {}", case["refusal"])));
            return out;
        }
        (Err(e), _) if e.clause == NOT_PORTED => {
            out.push(Ok("L2 -> L1 not ported"));
            return out;
        }
        (Err(_), None) if python_stage == Some("l2") => {
            out.push(Ok("L2 -> L1 both refuse"));
            return out;
        }
        (Err(e), _) => {
            out.push(Err(format!("L2 -> L1 refused, python did not: {e}")));
            return out;
        }
    };
    // L1 -> packages -> verdict
    // Every parameter bound on its own, as the Python reference binds them.
    let progs = match b.programs(&l1, &name, None, m) {
        Err(e) if e.clause == NOT_PORTED => {
            out.push(Ok("L1 -> L0 not ported"));
            return out;
        }
        Err(_) if python_stage == Some("l1") => {
            out.push(Ok("L1 -> L0 both refuse"));
            return out;
        }
        Err(e) => {
            out.push(Err(format!("L1 -> programs refused, python did not: {e}")));
            return out;
        }
        Ok(p) => p,
    };
    if let Some((ruling, stage)) = refused {
        out.push(match b.order(&progs, m) {
            Err(e) if refuses_as(ruling, &e) => Ok(ruled_line(ruling, stage)),
            Err(e) => Err(format!(
                "refused at {stage}, but not as {ruling} rules: {e}"
            )),
            Ok(_) => Err(format!("ordered, {ruling} refuses it at {stage}")),
        });
        return out;
    }
    // Where a ruling changes the bytes, the reference is dump.py's ruled
    // build (`ruled`: RUL.063's schedule, RUL.056's rounding), not Python's.
    let reference = match case.get("ruled").filter(|r| !r.is_null()) {
        Some(r) if r.get("refusal").is_some() => {
            out.push(Err(format!(
                "the ruled reference refused: {}",
                r["refusal"]
            )));
            return out;
        }
        Some(r) => r,
        None => case,
    };
    let ruled_tag = !std::ptr::eq(reference, case);
    // Which rulings make the reference: one table line each.
    let rules = |id: &str| {
        reference["rulings"]
            .as_array()
            .is_some_and(|r| r.iter().any(|x| x == id))
    };
    let rul = match (rules("RUL.056"), rules("RUL.063")) {
        (true, true) => "RUL.056 + RUL.063",
        (true, false) => "RUL.056",
        _ => "RUL.063",
    };
    // A table line: `base`, or `base [rulings: the ruled reference]`.
    let line = |base: &str| -> &'static str {
        match ruled_tag {
            true => Box::leak(format!("{base} [{rul}: the ruled reference]").into_boxed_str()),
            false => Box::leak(base.to_string().into_boxed_str()),
        }
    };
    // A build that is not relocatable (STACK.RELOC: past L0.BIND.005's 256
    // buffers) is compared built at the reference's own binds, as the card
    // builds it then (`CardStack::binds`).
    let bound = matches!(b.order(&progs, m), Err(e) if e.clause == "STACK.RELOC");
    let binds: Option<BTreeMap<String, i128>> = bound.then(|| {
        case["binds"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(k, v)| (k.clone(), v.as_i64().unwrap_or(0) as i128))
            .collect()
    });
    let plain = CardStack {
        binds: binds.clone(),
        node_fed: true,
        ..CardStack::default()
    };
    let card = CardStack {
        binds,
        node_fed: false,
        ..CardStack::default()
    };
    let progs = match bound {
        false => progs,
        true => match plain.programs(&l1, &name, None, m) {
            Ok(p) => p,
            Err(e) => {
                out.push(Err(format!("built at the reference's binds, refused: {e}")));
                return out;
            }
        },
    };
    let bound_line = |base: &str| -> &'static str {
        match bound {
            true => leak(&format!("{} (built at its binds: STACK.RELOC)", line(base))),
            false => line(base),
        }
    };
    let want = &reference["packages"];
    out.push(match plain.order(&progs, m) {
        Err(e) if e.clause == NOT_PORTED => Ok("order not ported"),
        Err(_) if python_stage == Some("schedule") => Ok("order both refuse"),
        Err(e) => Err(format!("order refused, python did not: {e}")),
        Ok(_) if want.is_null() => Err("ordered, python refused".into()),
        Ok(got) => same_packages(case, m, &got, want).map(|signature| match signature {
            true => bound_line("L1 -> L0 packages agree (signature as RUL.041 rules)"),
            false => bound_line("L1 -> L0 packages agree"),
        }),
    });
    let v = &reference["verdict"];
    out.push(match plain.check(&progs, m) {
        Err(e) if e.clause == NOT_PORTED => Ok("verify not ported"),
        Err(e) => Err(format!("verify refused: {e}")),
        Ok(_) if v.is_null() => Err("verified, python refused".into()),
        Ok(found) => same_verdict(&found, v).map(|()| bound_line("verdict agree")),
    });
    // The arenas: the L2 fn's parameters bound by the caller, the others in
    // one arena a space (no Python reference): relocatable, and the
    // checker's verdict the reference's.
    let arena = b
        .parse_l2(l2)
        .and_then(|p| b.caller(&p, &name))
        .and_then(|c| b.programs(&l1, &name, Some(&c), m));
    out.push(match arena {
        Err(e) => Err(format!("arena build refused: {e}")),
        Ok(ap) => match b.order(&ap, m).and_then(|_| b.check(&ap, m)) {
            Err(e) => Err(format!("arena build refused: {e}")),
            Ok(found) => same_verdict(&found, v).map(|()| match b.scratch(&ap).is_empty() {
                true => line("arena build agrees (no scratch)"),
                false => line("arena build relocatable, verdict agree"),
            }),
        },
    });
    // The card path: the same programs ordered and checked fetching through
    // the board's fetch port (`CardStack`), against dump.py's `card`.
    let c = &reference["card"];
    if c.is_null() {
        out.push(Err("no card reference: rerun golden/dump.py".into()));
        return out;
    }
    let fetch = &m.die.fetch_port;
    if c["fetch"] != serde_json::json!([fetch.0, fetch.1]) {
        out.push(Err(format!(
            "card reference fetches through {}, the machine {fetch:?}",
            c["fetch"]
        )));
        return out;
    }
    let python_card = c["refusal"]["stage"].as_str();
    out.push(match card.order(&progs, m) {
        Err(_) if python_card == Some("schedule") => Ok("card order both refuse"),
        Err(e) => Err(format!("card order refused, python did not: {e}")),
        Ok(_) if python_card == Some("schedule") => Err("card ordered, python refused".into()),
        Ok(got) => {
            same_packages(case, m, &got, &c["packages"]).map(|_| bound_line("card packages agree"))
        }
    });
    if python_card == Some("schedule") {
        return out;
    }
    out.push(match card.check(&progs, m) {
        Err(_) if python_card == Some("verify") => Ok("card verify both refuse"),
        Err(e) => Err(format!("card verify refused: {e}")),
        Ok(_) if python_card == Some("verify") => Err("card verified, python refused".into()),
        Ok(found) => same_verdict(&found, &c["verdict"]).map(|()| bound_line("card verdict agree")),
    });
    out
}

fn machine(file: &str, nodes: u32) -> Machine {
    let m = Machine::named(file).expect("machine file");
    match m.nodes() == nodes {
        true => m,
        false => m.with_nodes(nodes).expect("a card of that many nodes"),
    }
}

/// A case's time limit (the slowest case measured: examples-l2-swiglu4,
/// over 40 s in this profile with a shard beside it).
const CASE_SECONDS: std::time::Duration = std::time::Duration::from_secs(100);

/// `KTPU_GOLDEN_SHARD=i/n` keeps case i, i+n, ... (each run under the
/// shell's time limit); unset, every case.
fn shard(cases: Vec<(String, Value)>) -> Vec<(String, Value)> {
    let Ok(s) = std::env::var("KTPU_GOLDEN_SHARD") else {
        return cases;
    };
    let (i, n) = s.split_once('/').expect("KTPU_GOLDEN_SHARD=i/n");
    let (i, n): (usize, usize) = (i.parse().unwrap(), n.parse().unwrap());
    cases.into_iter().skip(i).step_by(n).collect()
}

#[test]
fn the_stack_agrees_with_the_python_goldens() {
    let cases = shard(goldens());
    // A case waits for its stages on a pool of its own: the stages run on a
    // thread of their own and fork onto the global pool (the lowering's
    // search), which waiting cases would otherwise hold to the time limit.
    let waiting = rayon::ThreadPoolBuilder::new().build().unwrap();
    let results: Vec<(String, f64, Vec<Step>)> = waiting.install(|| {
        cases
            .par_iter()
            .map(|(name, case)| {
                if case.get("skipped").is_some() {
                    return (name.clone(), 0.0, vec![Ok("skipped by the dump")]);
                }
                if case.get("python-too-slow").is_some() {
                    return (name.clone(), 0.0, vec![Ok("python too slow: no reference")]);
                }
                let m = machine(
                    case["machine"].as_str().unwrap(),
                    case["nodes"].as_u64().unwrap() as u32,
                );
                let t0 = Instant::now();
                // Each case on a thread of its own: a stage that does not end
                // is its case's finding (the thread is left to the process exit).
                let (tx, rx) = std::sync::mpsc::channel();
                let c = case.clone();
                std::thread::spawn(move || {
                    let got = std::panic::catch_unwind(|| stages(&c, &m)).unwrap_or_else(|p| {
                        let what = p
                            .downcast_ref::<String>()
                            .cloned()
                            .or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()))
                            .unwrap_or_default();
                        vec![Err(format!("panicked: {what}"))]
                    });
                    let _ = tx.send(got);
                });
                let got = rx
                    .recv_timeout(CASE_SECONDS)
                    .unwrap_or_else(|_| vec![Err(format!("over {CASE_SECONDS:?}"))]);
                (name.clone(), t0.elapsed().as_secs_f64(), got)
            })
            .collect()
    });
    let mut tally: BTreeMap<&str, usize> = BTreeMap::new();
    let mut bad = Vec::new();
    for (name, _, steps) in &results {
        for s in steps {
            match s {
                Ok(tag) => *tally.entry(tag).or_default() += 1,
                Err(why) => bad.push(format!("{name}: {why}")),
            }
        }
    }
    // machine -> (cases compared, cases with a disagreement)
    let mut by_machine: BTreeMap<&str, (usize, usize)> = BTreeMap::new();
    for ((_, case), (_, _, steps)) in cases.iter().zip(&results) {
        let Some(file) = case["machine"].as_str() else {
            continue;
        };
        let e = by_machine.entry(file).or_default();
        e.0 += 1;
        e.1 += usize::from(steps.iter().any(Result::is_err));
    }
    println!("by machine (cases, disagreeing): {by_machine:?}");
    let mut slow: Vec<(f64, &str)> = results.iter().map(|(n, s, _)| (*s, n.as_str())).collect();
    slow.sort_by(|a, b| b.0.total_cmp(&a.0));
    println!("{tally:#?}");
    println!("slowest: {:?}", &slow[..slow.len().min(5)]);
    let names: Vec<String> = goldens().into_iter().map(|(n, _)| n).collect();
    bad.extend(listings_hold(&names, &results));
    println!("{} disagree", bad.len());
    assert!(bad.is_empty(), "\n{}", bad.join("\n"));
}

/// Migrated attention has no refusal exception; a wrong entry name is detected.
#[test]
fn attention_is_compared_without_a_refusal_exception() {
    let (name, case) = goldens()
        .into_iter()
        .find(|(n, _)| n == "examples-dsl-attention")
        .expect("the compact-statistic attention case");
    let m = machine(
        case["machine"].as_str().unwrap(),
        case["nodes"].as_u64().unwrap() as u32,
    );
    let listed = stages(&case, &m);
    assert!(listed.iter().all(Result::is_ok), "{listed:?}");
    assert_eq!(case["verdict"]["refused"], false);
    if !case["ruled"].is_null() {
        assert_eq!(case["ruled"]["verdict"]["refused"], false);
    }
    assert!(
        ruled_refusal(&case).is_none(),
        "{name} has a refusal exception"
    );
    let mut wrong = case.clone();
    wrong["name"] = Value::String("rul030_missing_entry".into());
    let mismatched = stages(&wrong, &m);
    assert!(
        mismatched.iter().any(Result::is_err),
        "wrong entry name passes: {mismatched:?}"
    );
}
