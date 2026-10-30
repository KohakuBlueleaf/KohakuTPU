//! The Rust stack against the Python stack's end-to-end goldens, every case
//! stage by stage (`e2e::stages`). A panic is its case's finding, and so is
//! a hang (`Watch`); how long a case takes is `tests/compile_time.rs`'s.
//! Cases run in parallel.

#![cfg(feature = "stack")]

mod e2e;

use e2e::reference;
use e2e::{golden, line_layout, listings, machine, ruled_line, ruled_refusal, stages, Step};
use ktpu::stack::CardStack;
use ktpu::{Backend, Stack};
use ktpu_machine::{Machine, Refusal};
use rayon::prelude::*;
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::mpsc::RecvTimeoutError;
use std::sync::Mutex;
use std::time::{Duration, Instant};

fn goldens() -> Vec<(String, Value)> {
    let dir = golden().join("e2e");
    let mut out: Vec<(String, Value)> = std::fs::read_dir(&dir)
        .expect("golden/e2e: run golden/dump.py")
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .map(|p| {
            let name = p.file_stem().unwrap().to_string_lossy().into_owned();
            let case = e2e::case(&name);
            (name, case)
        })
        .collect();
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out
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

/// Process CPU seconds the cases may spend with none ending: past it, a case
/// still out spins without end, whatever else loads the host (the slowest
/// case takes under 50 alone, `tests/compile_time.rs`).
const SPINNING_CPU: f64 = 600.0;

/// Wall time with a case out and no process CPU spent: every thread waits.
const BLOCKED: Duration = Duration::from_secs(120);

/// The cases' hang detector over the CPU `cpu` reads: the CPU when a case
/// last ended, and the CPU last seen with the wall time it was first seen.
struct Watch {
    cpu: fn() -> f64,
    spinning: f64,
    blocked: Duration,
    seen: Mutex<(f64, f64, Instant)>,
}

impl Watch {
    fn new(cpu: fn() -> f64, spinning: f64, blocked: Duration) -> Self {
        let now = cpu();
        Watch {
            cpu,
            spinning,
            blocked,
            seen: Mutex::new((now, now, Instant::now())),
        }
    }

    /// A case ended (or was found hung): the window starts again.
    fn ended(&self) {
        self.seen.lock().unwrap().0 = (self.cpu)();
    }

    /// Why the cases still out are hung, if they are.
    fn hung(&self) -> Option<String> {
        let cpu = (self.cpu)();
        let mut w = self.seen.lock().unwrap();
        if cpu > w.1 {
            (w.1, w.2) = (cpu, Instant::now());
        }
        if cpu - w.0 > self.spinning {
            return Some(format!("hung: {} CPU s with no case ending", self.spinning));
        }
        if w.2.elapsed() > self.blocked {
            return Some(format!("hung: {:?} with no CPU spent", self.blocked));
        }
        None
    }
}

/// The watch finds a thread that spins and a process that spends nothing.
#[test]
fn the_watch_finds_a_hang() {
    let spin = Watch::new(e2e::cpu, 1.0, Duration::from_secs(3600));
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let spinner = {
        let stop = stop.clone();
        std::thread::spawn(move || {
            while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                std::hint::spin_loop();
            }
        })
    };
    let t0 = Instant::now();
    let found = loop {
        if let Some(why) = spin.hung() {
            break why;
        }
        assert!(t0.elapsed() < Duration::from_secs(60), "a spin not found");
        std::thread::sleep(Duration::from_millis(100));
    };
    stop.store(true, std::sync::atomic::Ordering::Relaxed);
    spinner.join().unwrap();
    assert!(found.contains("no case ending"), "{found}");
    let idle = Watch::new(|| 0.0, 1.0, Duration::from_secs(1));
    assert_eq!(idle.hung(), None);
    std::thread::sleep(Duration::from_millis(1500));
    assert!(idle.hung().is_some_and(|why| why.contains("no CPU spent")));
}

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
    let watch = Watch::new(e2e::cpu, SPINNING_CPU, BLOCKED);
    // A case waits for its stages on a pool of its own: the stages run on a
    // thread of their own and fork onto the global pool (the lowering's
    // search), which waiting cases would otherwise hold.
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
                let m = machine(case);
                let t0 = Instant::now();
                // Each case on a thread of its own: a stage that hangs is its
                // case's finding (the thread is left to the process exit).
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
                let got = loop {
                    match rx.recv_timeout(Duration::from_secs(1)) {
                        Ok(got) => break got,
                        Err(RecvTimeoutError::Disconnected) => unreachable!("the case sends"),
                        Err(RecvTimeoutError::Timeout) => {
                            if let Some(why) = watch.hung() {
                                break vec![Err(why)];
                            }
                        }
                    }
                };
                watch.ended();
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

/// Each mesh's package as a ruled reference records it: relocatable, its
/// `binds` addresses, and the package bound there and lowered.
fn package_json(
    case: &Value,
    got: &BTreeMap<u32, ktpu::PackageOut>,
    m: &Machine,
) -> Result<Value, String> {
    let hex = ktpu::pipeline::cache::hex;
    let mut out = serde_json::Map::new();
    for (mesh, p) in got {
        let bindings: Vec<u64> = p
            .params
            .iter()
            .map(|n| case["binds"][n].as_u64().ok_or(format!("no binding {n}")))
            .collect::<Result<_, _>>()?;
        let f = ktpu_l0::Facts::of_mesh(m, *mesh).map_err(|e| e.to_string())?;
        let bound = ktpu_l0::Package::from_bytes(&p.bytes, &f)
            .and_then(|q| q.bind(&bindings))
            .map_err(|e| e.to_string())?;
        let lowered = ktpu_l0::engine::lower(&bound, &f).map_err(|e| e.to_string())?;
        out.insert(
            mesh.to_string(),
            serde_json::json!({
                "relocatable": hex(&p.bytes),
                "bindings": bindings,
                "bound": hex(&bound.to_bytes(&f).map_err(|e| e.to_string())?),
                "lowered": hex(&lowered.to_bytes(&f).map_err(|e| e.to_string())?),
            }),
        );
    }
    Ok(Value::Object(out))
}

/// `binds` plus each `name.l1` parameter it lacks (a `__mx7_status_*`), in
/// order past the highest bound buffer's end, at 4096-byte boundaries.
fn placed(binds: &Value, l1: &ktpu_l1::text::Module, name: &str, m: &Machine) -> Value {
    let body = &l1.fns[&(name.to_string(), "l1".to_string())];
    let size = |term: &ktpu_l1::text::Term| {
        ktpu_l1::layout::buffer::Buffer::of(term)
            .and_then(|b| b.bytes(m))
            .unwrap()
    };
    let mut out = binds.as_object().unwrap().clone();
    let mut end = body
        .params
        .iter()
        .filter_map(|(p, term)| out.get(p).map(|a| a.as_u64().unwrap() + size(term)))
        .max()
        .unwrap_or(0);
    for (p, term) in &body.params {
        if !out.contains_key(p) {
            let at = end.div_ceil(4096) * 4096;
            out.insert(p.clone(), at.into());
            end = at + size(term);
        }
    }
    Value::Object(out)
}

fn verdict_json(found: &[ktpu::Finding]) -> Value {
    serde_json::json!({
        "findings": found.iter().map(|f| serde_json::json!([f.kind, f.at, f.text])).collect::<Vec<_>>(),
        "refused": found.iter().any(|f| Stack.refuses(f)),
    })
}

/// RUL.099's regeneration of the e2e references it changes:
/// `KTPU_RUL099_REGOLD=audit|write` (a `mutants` build; `write` also needs
/// `KTPU_RUL099_REGOLD_BACKUP=<dir>`). A case is the ruling's when its L1
/// under `l2.rul099/before` is the stored reference L1 (Python's, or
/// RUL.063's ruled schedule) and differs without it; its `ruled` block then
/// takes the current build's L1, packages and verdicts and the ruling. A case
/// the predecessor lowering does not reproduce is listed, never written.
#[test]
fn rul099_regold() {
    let Ok(mode) = std::env::var("KTPU_RUL099_REGOLD") else {
        return;
    };
    let write = mode == "write";
    let backup = write.then(|| {
        std::path::PathBuf::from(
            std::env::var("KTPU_RUL099_REGOLD_BACKUP").expect("KTPU_RUL099_REGOLD_BACKUP"),
        )
    });
    let dir = golden().join("e2e");
    let b = Stack;
    let lowered = |case: &Value, l2: &str, m: &Machine| {
        let p = b.parse_l2(l2)?;
        let name = case["name"]
            .as_str()
            .map_or_else(|| p.name.clone(), str::to_string);
        let l1 = b.schedule(&b.lower(&b.passes(&p, &name, m)?, &name, m)?, m)?;
        Ok::<_, Refusal>((name, l1))
    };
    let mut report: BTreeMap<&str, Vec<String>> = BTreeMap::new();
    for (stem, case) in shard(goldens()) {
        let Some(l2) = case["l2"].as_str() else {
            continue;
        };
        if case.get("skipped").is_some()
            || case.get("python-too-slow").is_some()
            || case["case"] == line_layout::CASE
            || ruled_refusal(&case).is_some()
        {
            continue;
        }
        let m = machine(&case);
        let stored =
            reference::ruled_schedule(&case, &golden()).or(case["l1"].as_str().map(str::to_string));
        let before = ktpu::mutant::with("l2.rul099/before", || {
            lowered(&case, l2, &m).map(|(_, l1)| b.print_l1(&l1))
        })
        .expect("a mutants build");
        let now = lowered(&case, l2, &m);
        let now_text = now
            .as_ref()
            .map(|(_, l1)| b.print_l1(l1))
            .map_err(|e| e.to_string());
        match (&before, &now_text) {
            (Ok(x), Ok(y)) if x == y => {
                report.entry("unchanged").or_default().push(stem);
                continue;
            }
            (Ok(x), _) if Some(x) != stored.as_ref() => {
                report
                    .entry("predecessor differs from the reference")
                    .or_default()
                    .push(stem);
                continue;
            }
            (Err(e), _) => {
                report
                    .entry("predecessor refused")
                    .or_default()
                    .push(format!("{stem}: {e}"));
                continue;
            }
            (Ok(_), Err(e)) => {
                report
                    .entry("refused under RUL.099")
                    .or_default()
                    .push(format!("{stem}: {e}"));
                continue;
            }
            _ => {}
        }
        let (name, l1) = now.unwrap();
        let mut case = case;
        case["binds"] = placed(&case["binds"], &l1, &name, &m);
        let progs = match b.programs(&l1, &name, None, &m) {
            Ok(p) => p,
            Err(e) => {
                report
                    .entry("programs refused")
                    .or_default()
                    .push(format!("{stem}: {e}"));
                continue;
            }
        };
        let bound = matches!(b.order(&progs, &m), Err(e) if e.clause == "STACK.RELOC");
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
        let progs = if bound {
            plain.programs(&l1, &name, None, &m).unwrap()
        } else {
            progs
        };
        let built = (|| {
            let e = |e: Refusal| e.to_string();
            Ok::<_, String>(serde_json::json!({
                "packages": package_json(&case, &plain.order(&progs, &m).map_err(e)?, &m)?,
                "verdict": verdict_json(&plain.check(&progs, &m).map_err(e)?),
                "card": {
                    "fetch": [m.die.fetch_port.0, m.die.fetch_port.1],
                    "packages": package_json(&case, &card.order(&progs, &m).map_err(e)?, &m)?,
                    "verdict": verdict_json(&card.check(&progs, &m).map_err(e)?),
                },
            }))
        })();
        let mut ruled = match built {
            Ok(r) => r,
            Err(e) => {
                report
                    .entry("build refused")
                    .or_default()
                    .push(format!("{stem}: {e}"));
                continue;
            }
        };
        let mut rulings: Vec<Value> = case["ruled"]["rulings"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        if !rulings.iter().any(|r| r == "RUL.099") {
            rulings.push("RUL.099".into());
        }
        ruled["rulings"] = Value::Array(rulings);
        ruled["l1"] = Value::String(now_text.unwrap());
        report.entry("ruled").or_default().push(stem.clone());
        if let Some(backup) = &backup {
            let path = dir.join(format!("{stem}.json"));
            let original = std::fs::read(&path).unwrap();
            std::fs::create_dir_all(backup).unwrap();
            std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(backup.join(format!("{stem}.json")))
                .and_then(|mut f| std::io::Write::write_all(&mut f, &original))
                .expect("a new backup");
            let mut updated = case.clone();
            updated["ruled"] = ruled;
            let text = serde_json::to_string_pretty(&updated).unwrap() + "\n";
            std::fs::write(&path, text).unwrap();
        }
    }
    for (what, cases) in &report {
        println!("{what}: {}", cases.len());
        if *what != "unchanged" {
            for c in cases {
                println!("    {c}");
            }
        }
    }
}

/// Migrated attention has no refusal exception; a wrong entry name is detected.
#[test]
fn attention_is_compared_without_a_refusal_exception() {
    let (name, case) = goldens()
        .into_iter()
        .find(|(n, _)| n == "examples-dsl-attention")
        .expect("the compact-statistic attention case");
    let m = machine(&case);
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
