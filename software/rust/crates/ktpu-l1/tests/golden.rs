//! The L1 goldens (`software/rust/golden/l1/*.json`) against
//! the Rust stack: canonical text and its round trip, the image scheduler's
//! module, image words, and each node program's package bytes built four ways.

mod common;

use std::collections::{BTreeMap, HashMap};

use common::repo;
use ktpu_l0::Facts;
use ktpu_l1::build::{self, Builder, Opts};
use ktpu_l1::layout::buffer::Buffer;
use ktpu_l1::lower::image::{self, reset, UNKNOWN};
use ktpu_l1::lower::imem::Cores;
use ktpu_l1::sched;
use ktpu_l1::text::{emit, parse, read};
use ktpu_machine::Machine;
use serde_json::Value;

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

struct Check {
    case: String,
    bad: Vec<String>,
    /// Seconds spent in each stage, over every case.
    times: BTreeMap<&'static str, f64>,
    /// Only the reviewed relocation view may be regenerated.
    updates: BTreeMap<String, (String, Vec<u64>)>,
    /// Reviewed software-loop package encoding changes, never image/text outputs.
    loop_updates: BTreeMap<String, Value>,
}

impl Check {
    fn timed<T>(&mut self, stage: &'static str, f: impl FnOnce() -> T) -> T {
        let t0 = std::time::Instant::now();
        let out = f();
        *self.times.entry(stage).or_default() += t0.elapsed().as_secs_f64();
        out
    }
}

impl Check {
    fn text(&mut self, what: &str, got: &str, want: &str) {
        let header_only = got.len() == want.len()
            && got.len() > 32
            && got[..16] == want[..16]
            && got[32..] == want[32..]
            && got[16..32] != want[16..32];
        if header_only {
            self.bad.push(format!(
                "{}: {what} signature only: header word 1 got {} want {}",
                self.case,
                &got[16..32],
                &want[16..32]
            ));
            return;
        }
        if got != want {
            let short = got.len().min(want.len());
            let skip = short > 32 && got[16..32] != want[16..32];
            let from = if skip { 32 } else { 0 };
            let at = (from..short)
                .find(|&i| got.as_bytes()[i] != want.as_bytes()[i])
                .unwrap_or(short);
            let lo = at.saturating_sub(60);
            self.bad.push(format!(
                "{}: {what} differs at hex digit {at} (len got {} want {}):\n  got  {:?}\n  want {:?}",
                self.case,
                got.len(),
                want.len(),
                &got[lo..(at + 60).min(got.len())],
                &want[lo..(at + 60).min(want.len())]
            ));
        }
    }

    fn fail(&mut self, what: &str, msg: impl std::fmt::Display) {
        self.bad.push(format!("{}: {what}: {msg}", self.case));
    }
}

fn machine(name: &str, cache: &mut HashMap<String, Machine>) -> Machine {
    cache
        .entry(name.to_string())
        .or_insert_with(|| Machine::named(name).expect("machine file"))
        .clone()
}

/// The RUL.063 scheduled module of `case`, when the ruling changes it.
fn ruled_schedule(case: &str) -> Option<String> {
    let p = repo().join(format!("software/rust/golden/sched/l1/{case}.json"));
    let r: Value = serde_json::from_str(&std::fs::read_to_string(p).ok()?).ok()?;
    let tagged = r["rulings"].as_array()?.iter().any(|x| x == "RUL.063");
    tagged.then(|| r["scheduled_ruled"].as_str().map(String::from))?
}

fn run(g: &Value, m: &Machine, c: &mut Check) {
    let text = g["text"].as_str().unwrap();
    let refusal = g["refusal"]
        .as_object()
        .map(|r| r["stage"].as_str().unwrap().to_string());
    // Text: canonical form and its round trip (L1.TXT.013).
    let stmts = match c.timed("parse", || parse(text, "<golden.l1>")) {
        Ok(s) => s,
        Err(e) => {
            if refusal.as_deref() != Some("read") {
                c.fail("parse", e);
            }
            return;
        }
    };
    if let Some(want) = g["canonical"].as_str() {
        let got = emit(&stmts);
        c.text("canonical", &got, want);
        match parse(want, "<canonical>") {
            Ok(again) => c.text("round trip", &emit(&again), want),
            Err(e) => c.fail("reparse", e),
        }
    }
    let module = match read(text, "<golden.l1>") {
        Ok(m) => m,
        Err(e) => {
            if refusal.as_deref() != Some("read") {
                c.fail("read", e);
            }
            return;
        }
    };
    if refusal.as_deref() == Some("read") {
        c.fail("read", "Python refused the module; Rust read it");
        return;
    }
    if let Some(p) = g["pack"].as_object() {
        let ty = &module.body("f", "l1").unwrap().params[0].1;
        let values: Vec<u16> = p["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap() as u16)
            .collect();
        match Buffer::of(ty).and_then(|b| b.pack(&values, m)) {
            Ok(bytes) => c.text("pack", &hex(&bytes), p["bytes"].as_str().unwrap()),
            Err(e) => c.fail("pack", e),
        }
        return;
    }
    if let Some(python) = g["scheduled"].as_str() {
        // RUL.063: where the ruled model reorders, rs-l0's reference holds
        // the ruled module (golden/sched/l1/<case>.json "scheduled_ruled").
        let ruled = ruled_schedule(g["case"].as_str().unwrap());
        let want = ruled.as_deref().unwrap_or(python);
        match c.timed("schedule", || sched::schedule_module(&module, m)) {
            Ok(got) => c.text("scheduled", &got.source, want),
            Err(e) => c.fail("schedule", e),
        }
    }
    if let Some(img) = g["images"].as_object() {
        let args: Vec<i128> = img["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|a| a.as_i64().unwrap() as i128)
            .collect();
        let entry = if img["entry"] == "reset" {
            reset(m.vc.vlmax)
        } else {
            UNKNOWN
        };
        match image::image(&module, m, img["image"].as_str().unwrap(), &args, entry) {
            Ok(_) if refusal.as_deref() == Some("image") => {
                c.fail("image", "Python refused the image; Rust encoded it")
            }
            Err(_) if refusal.as_deref() == Some("image") => {}
            Ok((words, descs)) => {
                let want: Vec<u64> = img["words"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|w| w.as_u64().unwrap())
                    .collect();
                if words != want {
                    c.fail("image words", format!("got {words:x?} want {want:x?}"));
                }
                let wd: Vec<(u32, u32, u64)> = img["descs"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|d| {
                        (
                            d[0].as_u64().unwrap() as u32,
                            d[1].as_u64().unwrap() as u32,
                            d[2].as_u64().unwrap(),
                        )
                    })
                    .collect();
                let gd: Vec<(u32, u32, u64)> =
                    descs.iter().map(|((a, f), v)| (*a, *f, *v)).collect();
                if gd != wd {
                    c.fail("image descs", format!("got {gd:?} want {wd:?}"));
                }
            }
            Err(e) => c.fail("image", e),
        }
    }
    let Some(name) = g["name"].as_str() else {
        return;
    };
    let mut binds = BTreeMap::new();
    for (k, v) in g["binds"].as_object().into_iter().flatten() {
        binds.insert(k.clone(), v.as_i64().unwrap() as i128);
    }
    let nodes = g["nodes"].as_u64().map(|n| n as u32);
    let got = c.timed("programs", || {
        build::programs(&module, name, &binds, m, nodes, None)
    });
    let progs = match got {
        Ok(p) => p,
        Err(e) => {
            if refusal.as_deref() != Some("programs") {
                c.fail("programs", e);
            }
            return;
        }
    };
    if refusal.as_deref() == Some("programs") {
        c.fail("programs", "Python refused the programs; Rust built them");
        return;
    }
    let want = g["programs"].as_object();
    let builder = Builder::new(m).unwrap();
    for np in &progs {
        let key = np.node.map(|n| n.to_string()).unwrap_or_else(|| "-".into());
        let stage = format!("build {key}");
        let f = Facts::of_mesh(m, np.mesh).unwrap();
        let bytes = |b: ktpu_l0::Package| hex(&b.to_bytes(&f).expect("package bytes"));
        let t0 = std::time::Instant::now();
        let all = (|| -> ktpu_l1::Result<Vec<(String, String)>> {
            let mut out = Vec::new();
            let plain = builder.build(np, Opts::default())?;
            out.push(("plain".into(), bytes(plain.build(true, false))));
            let mut cores = Cores::new(m)?;
            for i in 0..2 {
                let opts = Opts {
                    cores: Some(&mut cores),
                    ..Opts::default()
                };
                let b = builder.build(np, opts)?;
                out.push((format!("resident{i}"), bytes(b.build(true, false))));
            }
            let relocate = Opts {
                relocate: true,
                ..Opts::default()
            };
            let rb = builder.build(np, relocate)?;
            let relocated = rb.build(true, false);
            assert_eq!(
                bytes(relocated.bind(&rb.bindings()).unwrap()),
                bytes(plain.build(true, false).bind(&[]).unwrap()),
                "{}: relocation must preserve the absolute program at its original placement",
                c.case
            );
            out.push(("relocated".into(), bytes(relocated)));
            out.push(("bindings".into(), format!("{:?}", rb.bindings())));
            let (pkg, bindings) = builder.package(np, Opts::default())?;
            out.push(("package".into(), bytes(pkg)));
            out.push(("package_bindings".into(), format!("{bindings:?}")));
            Ok(out)
        })();
        *c.times.entry("build").or_default() += t0.elapsed().as_secs_f64();
        let got = match all {
            Ok(g) => g,
            Err(e) => {
                if refusal.as_deref() != Some(stage.as_str()) {
                    c.fail(&stage, e);
                }
                continue;
            }
        };
        if refusal.as_deref() == Some(stage.as_str()) {
            c.fail(&stage, "Python refused the build; Rust built it");
            continue;
        }
        let Some(w) = want.and_then(|w| w.get(&key)) else {
            c.fail(&stage, "no golden for this node");
            continue;
        };
        let expect = |k: &str| -> String {
            match k {
                "resident0" => w["resident"][0].as_str().unwrap().to_string(),
                "resident1" => w["resident"][1].as_str().unwrap().to_string(),
                "bindings" | "package_bindings" => format!(
                    "{:?}",
                    w[k].as_array()
                        .unwrap()
                        .iter()
                        .map(|a| a.as_u64().unwrap())
                        .collect::<Vec<_>>()
                ),
                _ => w[k].as_str().unwrap().to_string(),
            }
        };
        let regold = std::env::var("KTPU_L1_REGOLD").ok();
        if std::env::var("KTPU_LOOP_REGOLD").is_ok() && got.iter().any(|(k, v)| v != &expect(k)) {
            assert!(
                matches!(
                    c.case.as_str(),
                    "ex-tiles"
                        | "vec-l1-low-L1.LOW.008.past-label"
                        | "vec-l1-low-L1.LOW.008.patch"
                        | "vec-l1-low-L1.LOW.011.end"
                        | "vec-l1-mg-L1.MG.016.patched-fill"
                        | "vec-l1-node-L1.NODE.004.runtime-field"
                ),
                "unreviewed software-loop encoding case {}",
                c.case
            );
            let mut updated = w.clone();
            for (k, v) in &got {
                if v == &expect(k) {
                    continue;
                }
                eprintln!("LOOP AUDIT {} node {key} {k}: {} -> {v}", c.case, expect(k));
                match k.as_str() {
                    "resident0" => updated["resident"][0] = Value::String(v.clone()),
                    "resident1" => updated["resident"][1] = Value::String(v.clone()),
                    "plain" | "relocated" | "package" => updated[k] = Value::String(v.clone()),
                    _ => panic!("loop encoding must not change bindings: {k}"),
                }
            }
            c.loop_updates.insert(key.clone(), updated);
            continue;
        }
        for (k, v) in &got {
            if regold.is_some() && (k == "relocated" || k == "bindings") {
                if v != &expect(k) {
                    eprintln!("{} node {key} {k}: {} -> {v}", c.case, expect(k));
                    eprintln!(
                        "  params {:?}; binds {binds:?}; spans {:?}",
                        module.body(name, "l1").unwrap().params,
                        np.program.spans
                    );
                    let rb = builder
                        .build(
                            np,
                            Opts {
                                relocate: true,
                                ..Opts::default()
                            },
                        )
                        .unwrap();
                    eprintln!("  relocation sites {:?}", rb.relocs);
                    c.updates
                        .insert(key.clone(), (bytes(rb.build(true, false)), rb.bindings()));
                }
            } else {
                c.text(&format!("{stage} {k}"), v, &expect(k));
            }
        }
    }
}

#[test]
fn l1_goldens() {
    if let Ok(mode) = std::env::var("KTPU_L1_REGOLD") {
        assert!(
            matches!(mode.as_str(), "audit" | "write"),
            "unknown regeneration mode {mode}"
        );
    }
    if let Ok(mode) = std::env::var("KTPU_LOOP_REGOLD") {
        assert!(matches!(mode.as_str(), "audit" | "write"));
        assert!(std::env::var("KTPU_L1_REGOLD").is_err());
    }
    let dir = repo().join("software/rust/golden/l1");
    let Ok(rd) = std::fs::read_dir(&dir) else {
        panic!("no goldens at {}", dir.display());
    };
    let mut files: Vec<_> = rd
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no golden cases");
    let mut pending = Vec::new();
    let mut machines = HashMap::new();
    let mut bad = Vec::new();
    let mut failed = 0;
    let mut times: BTreeMap<&'static str, f64> = BTreeMap::new();
    let mut ruled: BTreeMap<String, usize> = BTreeMap::new();
    for p in &files {
        let g: Value = serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap();
        let m = machine(g["machine"].as_str().unwrap(), &mut machines);
        let case = g["case"].as_str().unwrap();
        for r in g["rulings"].as_array().into_iter().flatten() {
            *ruled.entry(r.as_str().unwrap().to_string()).or_default() += 1;
        }
        if ruled_schedule(case).is_some() {
            *ruled.entry("RUL.063".into()).or_default() += 1;
        }
        let mut c = Check {
            case: g["case"].as_str().unwrap().to_string(),
            bad: Vec::new(),
            times: BTreeMap::new(),
            updates: BTreeMap::new(),
            loop_updates: BTreeMap::new(),
        };
        run(&g, &m, &mut c);
        if !c.loop_updates.is_empty() {
            let mut updated = g.clone();
            for (node, program) in &c.loop_updates {
                updated["programs"][node] = program.clone();
            }
            pending.push((p.clone(), updated));
        }
        if !c.updates.is_empty() {
            let mut updated = g.clone();
            for (node, (relocated, bindings)) in &c.updates {
                updated["programs"][node]["relocated"] = Value::String(relocated.clone());
                updated["programs"][node]["bindings"] = serde_json::json!(bindings);
            }
            pending.push((p.clone(), updated));
        }
        for (k, v) in c.times {
            *times.entry(k).or_default() += v;
        }
        if !c.bad.is_empty() {
            failed += 1;
            bad.extend(c.bad);
        }
    }
    eprintln!(
        "l1 goldens: {} of {} cases pass; cases a ruling changes {ruled:?}; seconds by stage {times:.2?}",
        files.len() - failed,
        files.len()
    );
    let mut kinds: BTreeMap<String, Vec<&String>> = BTreeMap::new();
    for b in &bad {
        let what = b
            .split(": ")
            .nth(1)
            .unwrap_or("")
            .split(" differs")
            .next()
            .unwrap_or("");
        let what: String = what
            .split_whitespace()
            .filter(|w| w.parse::<i64>().is_err() && *w != "-")
            .collect::<Vec<_>>()
            .join(" ");
        kinds.entry(what).or_default().push(b);
    }
    let mut report = Vec::new();
    for (k, v) in &kinds {
        report.push(format!("== {k}: {} failures", v.len()));
        let shown = if k.ends_with("signature only") {
            1
        } else {
            v.len()
        };
        report.extend(v.iter().take(shown).map(|s| s.to_string()));
    }
    assert!(
        bad.is_empty(),
        "{failed} of {} cases fail:\n{}",
        files.len(),
        report.join("\n")
    );
    if std::env::var("KTPU_LOOP_REGOLD").as_deref() == Ok("write") {
        let selected = std::env::var("KTPU_L1_CASES").expect("explicit reviewed case list");
        regenerate(&pending, &selected, &backup());
    }
    if std::env::var("KTPU_L1_REGOLD").as_deref() == Ok("write") {
        let selected =
            std::env::var("KTPU_L1_CASES").expect("explicit reviewed comma-separated case list");
        regenerate(&pending, &selected, &backup());
    }
}

/// The directory a regold copies each original golden into before it writes.
fn backup() -> std::path::PathBuf {
    std::env::var("KTPU_L1_REGOLD_BACKUP")
        .expect("KTPU_L1_REGOLD_BACKUP: directory receiving the original goldens")
        .into()
}

/// Validate every approval and snapshot destination before writing any fixture.
fn regenerate(pending: &[(std::path::PathBuf, Value)], selected: &str, backup: &std::path::Path) {
    let mut writes = Vec::new();
    for (p, updated) in pending {
        let case = updated["case"].as_str().unwrap();
        assert!(
            selected.split(',').any(|s| s == case),
            "unreviewed case {case}"
        );
        let saved = backup.join(p.file_name().unwrap());
        assert!(
            !saved.exists(),
            "original snapshot already exists: {}",
            saved.display()
        );
        let mut output = Vec::new();
        let formatter = serde_json::ser::PrettyFormatter::with_indent(b" ");
        let mut serializer = serde_json::Serializer::with_formatter(&mut output, formatter);
        serde::Serialize::serialize(updated, &mut serializer).unwrap();
        output.push(b'\n');
        writes.push((p, saved, output));
    }
    for (p, saved, output) in writes {
        std::fs::create_dir_all(backup).unwrap();
        std::fs::copy(p, saved).unwrap();
        std::fs::write(p, output).unwrap();
        eprintln!("REGENERATED {}", p.display());
    }
}

#[test]
fn regeneration_refuses_late_unapproved_case_without_touching_earlier_files() {
    let dir = std::env::temp_dir().join(format!("ktpu-l1-regold-preflight-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let first = dir.join("first.json");
    std::fs::write(&first, b"original").unwrap();
    let pending = vec![
        (first.clone(), serde_json::json!({"case":"first"})),
        (
            dir.join("second.json"),
            serde_json::json!({"case":"second"}),
        ),
    ];
    let backup = dir.join("backup");
    let result = std::panic::catch_unwind(|| regenerate(&pending, "first", &backup));
    assert!(result.is_err());
    assert_eq!(std::fs::read(first).unwrap(), b"original");
    assert!(!backup.exists());
    std::fs::remove_dir_all(dir).unwrap();
}
