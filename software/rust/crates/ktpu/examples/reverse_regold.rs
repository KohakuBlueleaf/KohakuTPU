//! Regenerate the end-to-end goldens (`golden/e2e` and the `golden/sched/e2e`
//! schedule of each) a compiler change moves, each proven to be moved by that
//! change alone: a case builds differently with and without the change's
//! reverse mutant, under the mutant it compiles to every view it holds now (L1,
//! schedule fixture, plain and card packages), and the verifier's findings are
//! the same with and without it; the case's `l1` is rewritten only where the
//! mutant reproduces it too. Audit by default;
//! `--write` rewrites the moved cases and keeps each original and each
//! rewritten file under AUDIT_DIR (`originals/`, `updated/`, `manifest.json`).
//!
//! cargo run -p ktpu --features mutants --example reverse_regold -- MUTANT AUDIT_DIR [--write] [CASE...]

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use ktpu::{Backend, CardStack, Finding, PackageOut, Stack};
use ktpu_l1::sched::{self, runs, timing, Ruled};
use ktpu_l1::text::{emit_at, read, Expander};
use ktpu_l2::{Program, Search, Target, PASSES};
use ktpu_machine::Machine;
use rayon::prelude::*;
use serde_json::{json, Value};

type Result<T> = std::result::Result<T, String>;

fn err(e: impl ToString) -> String {
    e.to_string()
}

fn load(path: &Path) -> Result<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).map_err(err)?).map_err(err)
}

fn save(path: &Path, value: &Value) -> Result<()> {
    std::fs::create_dir_all(path.parent().unwrap()).map_err(err)?;
    let text = serde_json::to_string_pretty(value).map_err(err)? + "\n";
    std::fs::write(path, text).map_err(err)
}

fn machine(case: &Value) -> Result<Machine> {
    let m = Machine::named(case["machine"].as_str().unwrap()).map_err(err)?;
    let nodes = case["nodes"].as_u64().unwrap() as u32;
    match m.nodes() == nodes {
        true => Ok(m),
        false => m.with_nodes(nodes).map_err(err),
    }
}

/// Every view of a case the stack builds, as `stages` in `tests/golden.rs`,
/// and the L1 `e2e_l1` in ktpu-l2's `tests/golden.rs` compiles.
#[derive(PartialEq)]
struct Built {
    /// The lowering before the image schedule (the schedule fixture's input).
    lowered: String,
    l1: String,
    /// ktpu-l2's own compile and schedule: the case's `l1`.
    l2_l1: String,
    plain: BTreeMap<u32, PackageOut>,
    verdict: Vec<Finding>,
    card: BTreeMap<u32, PackageOut>,
    card_verdict: Vec<Finding>,
}

fn build(case: &Value, m: &Machine) -> Result<Built> {
    let l2 = Stack.parse_l2(case["l2"].as_str().unwrap()).map_err(err)?;
    let name = case["name"]
        .as_str()
        .map_or_else(|| l2.name.clone(), str::to_string);
    let passed = Stack.passes(&l2, &name, m).map_err(err)?;
    let lowered = Stack.lower(&passed, &name, m).map_err(err)?;
    let l1 = Stack.schedule(&lowered, m).map_err(err)?;
    let progs = Stack.programs(&l1, &name, None, m).map_err(err)?;
    // Past L0.BIND.005 a build is not relocatable: built at the case's binds.
    let bound = matches!(Stack.order(&progs, m), Err(e) if e.clause == "STACK.RELOC");
    let binds: Option<BTreeMap<String, i128>> = bound.then(|| {
        case["binds"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.as_i64().unwrap() as i128))
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
        true => plain.programs(&l1, &name, None, m).map_err(err)?,
        false => progs,
    };
    let named = Machine::named(case["machine"].as_str().unwrap()).map_err(err)?;
    let t = Target::of(&named).with_nodes(case["nodes"].as_i64().unwrap_or(4));
    let p = Program::parse(
        case["l2"].as_str().unwrap(),
        "<golden.l2>",
        case["name"].as_str(),
    )
    .map_err(err)?;
    let l2_l1 = ktpu_l2::compile_with(&p, &t, &PASSES, true, &Search::default()).map_err(err)?;
    Ok(Built {
        lowered: lowered.source.clone(),
        l1: l1.source.clone(),
        l2_l1: l2_l1.l1.source,
        plain: plain.order(&progs, m).map_err(err)?,
        verdict: plain.check(&progs, m).map_err(err)?,
        card: card.order(&progs, m).map_err(err)?,
        card_verdict: card.check(&progs, m).map_err(err)?,
    })
}

/// `got` as a golden's packages: each mesh's relocatable, bound and lowered
/// bytes and bindings. Where a byte view of `held` (the golden's own) writes
/// the header's signature word 0 (RUL.041), so does this one.
fn packages(
    got: &BTreeMap<u32, PackageOut>,
    case: &Value,
    held: &Value,
    m: &Machine,
) -> Result<Value> {
    let mut out = serde_json::Map::new();
    for (mesh, p) in got {
        let bindings = p
            .params
            .iter()
            .map(|n| {
                case["binds"][n]
                    .as_u64()
                    .ok_or_else(|| format!("no binding {n}"))
            })
            .collect::<Result<Vec<_>>>()?;
        let f = ktpu_l0::Facts::of_mesh(m, *mesh).map_err(err)?;
        let parsed = ktpu_l0::Package::from_bytes(&p.bytes, &f).map_err(err)?;
        let bound = parsed.bind(&bindings).map_err(err)?;
        let lowered = ktpu_l0::engine::lower(&bound, &f).map_err(err)?;
        let zero = "0".repeat(16);
        let signed = |field: &str, bytes: &[u8]| {
            let mut text = ktpu::pipeline::cache::hex(bytes);
            if held[mesh.to_string()][field]
                .as_str()
                .is_some_and(|h| h.get(16..32) == Some(zero.as_str()))
            {
                text.replace_range(16..32, &zero);
            }
            text
        };
        out.insert(
            mesh.to_string(),
            json!({
                "relocatable": signed("relocatable", &p.bytes),
                "bindings": bindings,
                "bound": signed("bound", &bound.to_bytes(&f).map_err(err)?),
                "lowered": signed("lowered", &lowered.to_bytes(&f).map_err(err)?),
            }),
        );
    }
    Ok(Value::Object(out))
}

/// Finding kinds and the refusal as a golden verdict compares them.
fn verdict(found: &[Finding]) -> (Vec<String>, bool) {
    let mut kinds: Vec<String> = found.iter().map(|f| f.kind.clone()).collect();
    kinds.sort();
    (kinds, found.iter().any(|f| Stack.refuses(f)))
}

fn held_verdict(v: &Value) -> (Vec<String>, bool) {
    let mut kinds: Vec<String> = v["findings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f[0].as_str().unwrap().to_string())
        .collect();
    kinds.sort();
    (kinds, v["refused"].as_bool().unwrap())
}

/// A schedule fixture's views of its `text`: every image's runs, the RUL.063
/// order and cycles of each, the written and reordered images and their
/// timing, and what `schedule_module` keeps.
fn schedule(fixture: &mut Value) -> Result<()> {
    let m = Machine::named(fixture["machine"].as_str().unwrap()).map_err(err)?;
    let module = read(fixture["text"].as_str().unwrap(), "<sched>").map_err(err)?;
    let scheduled = sched::schedule_module(&module, &m).map_err(err)?;
    let r = module.reader();
    let macros = module.macro_table();
    let ruled = Ruled::new(&r, &m, module.vregs, module.vcopies).map_err(err)?;
    let fill = (m.vc.fill_alone.0 as i64, m.vc.fill_alone.1 as i64);
    let held: Vec<Value> = fixture["images"].as_array().unwrap().clone();
    let mut images = Vec::new();
    for (name, img) in &module.images {
        let body = Expander::new(&macros, &r, true)
            .stmts(&img.stmts, &HashMap::new())
            .map_err(err)?;
        let mut records = Vec::new();
        for run in runs(&body) {
            let order = ruled.order(&run).map_err(err)?;
            let all: Vec<usize> = (0..run.len()).collect();
            let stmts: Vec<String> = run
                .iter()
                .map(|s| emit_at(std::slice::from_ref(s), 0, 48).trim().to_string())
                .collect();
            let preds: Vec<Vec<usize>> = sched::preds(&run, m.vc.chunks)
                .iter()
                .map(|p| {
                    let mut p: Vec<usize> = p.iter().copied().collect();
                    p.sort();
                    p
                })
                .collect();
            records.push(json!({"stmts": stmts, "preds": preds, "ruled": {
                "order": order,
                "written": ruled.cycles(&run, &all).map_err(err)?,
                "cycles": ruled.cycles(&run, &order).map_err(err)?,
            }}));
        }
        let head = format!("image {}({})\n", img.name, img.params.join(", "));
        let written = format!("{head}{}", emit_at(&body, 1, 48));
        let tuned = format!(
            "{head}{}",
            emit_at(&ruled.block(&body).map_err(err)?, 1, 48)
        );
        let cycles = |text: &str| {
            timing::cycles(text, name, module.vregs, module.vcopies, fill, &m).map_err(err)
        };
        // An image the fixture holds keeps the fields this does not make.
        let mut im = held
            .iter()
            .find(|h| h["name"] == **name)
            .cloned()
            .unwrap_or_else(|| json!({ "name": name }));
        im["runs"] = json!(records);
        im["chosen"] = json!(format!(
            "{head}{}",
            emit_at(&scheduled.images[name].stmts, 1, 48)
        ));
        im["cycles_written"] = json!(cycles(&written)?);
        im["cycles_tuned"] = json!(cycles(&tuned)?);
        im["written"] = json!(written);
        im["ruled"] = json!(tuned);
        images.push(im);
    }
    fixture["images"] = json!(images);
    fixture["scheduled_ruled"] = json!(scheduled.source);
    Ok(())
}

/// The golden's L1: the RUL.063 schedule where the ruling moves Python's
/// (`ruled_schedule` in `tests/golden.rs`), else the case's own.
fn held_l1<'a>(case: &'a Value, fixture: Option<&'a Value>) -> &'a str {
    let ruled = fixture.and_then(|s| {
        let r = s["scheduled_ruled"].as_str()?;
        let tagged = s["rulings"].as_array()?.iter().any(|x| x == "RUL.063");
        (tagged && Some(r) != s["scheduled"].as_str()).then_some(r)
    });
    ruled.unwrap_or_else(|| case["l1"].as_str().unwrap())
}

/// The first line at which `got` and `want` differ, with both lines.
fn first_difference(got: &str, want: &str) -> String {
    let at = got
        .lines()
        .zip(want.lines())
        .position(|(a, b)| a != b)
        .unwrap_or(got.lines().count().min(want.lines().count()));
    format!(
        "line {}: got {:?}, want {:?}",
        at + 1,
        got.lines().nth(at),
        want.lines().nth(at)
    )
}

/// Whether every line of `old` is in `new`, in order.
fn inserted_only(old: &str, new: &str) -> bool {
    let mut lines = new.lines();
    old.lines().all(|l| lines.any(|n| n == l))
}

/// The JSON pointers at which `a` and `b` differ, down to `depth` levels.
fn moved(a: &Value, b: &Value, at: &str, depth: usize, out: &mut Vec<String>) {
    match (a, b) {
        (Value::Object(x), Value::Object(y)) if depth > 0 => {
            for (k, v) in x {
                moved(
                    v,
                    y.get(k).unwrap_or(&Value::Null),
                    &format!("{at}/{k}"),
                    depth - 1,
                    out,
                );
            }
        }
        (Value::Array(x), Value::Array(y)) if depth > 0 && x.len() == y.len() => {
            for (i, (v, w)) in x.iter().zip(y).enumerate() {
                moved(v, w, &format!("{at}/{i}"), depth - 1, out);
            }
        }
        _ if a != b => out.push(at.to_string()),
        _ => {}
    }
}

struct Case {
    stem: String,
    path: PathBuf,
    sched: PathBuf,
}

/// One case: `None` where the change does not move it, else its rewritten
/// golden and schedule fixture and the evidence.
fn regold(c: &Case, mutant: &str) -> Result<Option<(Value, Option<Value>, Value)>> {
    let case = load(&c.path)?;
    if case["l2"].as_str().is_none()
        || case["l1"].as_str().is_none()
        || case.get("skipped").is_some()
        || case.get("python-too-slow").is_some()
    {
        return Ok(None);
    }
    let fixture = match c.sched.exists() {
        true => Some(load(&c.sched)?),
        false => None,
    };
    let m = machine(&case)?;
    let (old, new) = match (
        ktpu::mutant::with(mutant, || build(&case, &m))?,
        build(&case, &m),
    ) {
        (Ok(old), Ok(new)) => (old, new),
        (Err(a), Err(b)) if a == b => return Ok(None),
        (a, b) => {
            return Err(format!(
                "the change moves the build: {:?} -> {:?}",
                a.err(),
                b.err()
            ))
        }
    };
    if new == old {
        return Ok(None);
    }

    // A case the change moves: its golden is the reverse mutant's output,
    // view by view.
    let reference = match case.get("ruled").filter(|r| !r.is_null()) {
        Some(r) => r.clone(),
        None => case.clone(),
    };
    let held = held_l1(&case, fixture.as_ref());
    if old.l1 != held {
        return Err(format!(
            "the golden's scheduled L1 is not {mutant}'s: {}",
            first_difference(&old.l1, held)
        ));
    }
    // `l1` is rewritten only where the mutant reproduces it: a case whose
    // `l1` ktpu-l2 does not compile to without the change keeps it.
    let l2_l1_held = old.l2_l1 == case["l1"];
    if let Some(s) = &fixture {
        if old.lowered != s["text"] {
            return Err(format!("the schedule fixture is not {mutant}'s"));
        }
    }
    for (pointer, got) in [("/packages", &old.plain), ("/card/packages", &old.card)] {
        let held = reference.pointer(pointer).unwrap();
        if held.is_null() {
            return Err(format!("{pointer}: the reference refused"));
        }
        let mut differ = Vec::new();
        moved(&packages(got, &case, held, &m)?, held, "", 2, &mut differ);
        if !differ.is_empty() {
            return Err(format!("{pointer} are not {mutant}'s at {differ:?}"));
        }
    }
    if verdict(&old.verdict) != held_verdict(&reference["verdict"])
        || verdict(&old.card_verdict) != held_verdict(&reference["card"]["verdict"])
    {
        return Err(format!("the verdicts are not {mutant}'s"));
    }
    if new.verdict != old.verdict || new.card_verdict != old.card_verdict {
        return Err("the change moves the verifier's findings".into());
    }

    let mut updated = case.clone();
    if l2_l1_held {
        updated["l1"] = json!(new.l2_l1);
    }
    let at = match case.get("ruled").filter(|r| !r.is_null()) {
        Some(_) => "/ruled",
        None => "",
    };
    for (pointer, got) in [("/packages", &new.plain), ("/card/packages", &new.card)] {
        let pointer = format!("{at}{pointer}");
        let held = case.pointer(&pointer).unwrap();
        *updated.pointer_mut(&pointer).unwrap() = packages(got, &case, held, &m)?;
    }
    let fixture_updated = match &fixture {
        Some(s) => {
            let mut f = s.clone();
            f["text"] = json!(new.lowered);
            schedule(&mut f)?;
            if s["scheduled"] == s["scheduled_ruled"] {
                f["scheduled"] = f["scheduled_ruled"].clone();
            }
            if held_l1(&updated, Some(&f)) != new.l1 {
                return Err("the rewritten golden is not the new L1".into());
            }
            Some(f)
        }
        None => None,
    };
    let mut fields = Vec::new();
    moved(&case, &updated, "", 4, &mut fields);
    let mut sched_fields = Vec::new();
    if let (Some(a), Some(b)) = (&fixture, &fixture_updated) {
        moved(a, b, "", 2, &mut sched_fields);
    }
    let evidence = json!({
        "case": c.stem,
        "e2e": fields,
        "l1_kept": (!l2_l1_held).then(|| first_difference(&old.l2_l1, case["l1"].as_str().unwrap())),
        "sched/e2e": sched_fields,
        "l1_lines": [old.l1.lines().count(), new.l1.lines().count()],
        "inserted_only": inserted_only(&old.l1, &new.l1),
        "lowered_inserted_only": inserted_only(&old.lowered, &new.lowered),
    });
    Ok(Some((updated, fixture_updated, evidence)))
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (mutant, audit) = (&args[0], PathBuf::from(&args[1]));
    let write = args.iter().any(|a| a == "--write");
    let only: Vec<&String> = args[2..].iter().filter(|a| *a != "--write").collect();
    let golden = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden");
    let mut cases = Vec::new();
    for e in std::fs::read_dir(golden.join("e2e")).map_err(err)? {
        let path = e.map_err(err)?.path();
        let stem = path.file_stem().unwrap().to_string_lossy().to_string();
        if path.extension().is_some_and(|x| x == "json")
            && (only.is_empty() || only.contains(&&stem))
        {
            let sched = golden.join("sched/e2e").join(path.file_name().unwrap());
            cases.push(Case { stem, path, sched });
        }
    }
    cases.sort_by(|a, b| a.stem.cmp(&b.stem));
    assert!(only.is_empty() || cases.len() == only.len(), "unknown case");

    let results: Vec<Result<Option<(Value, Option<Value>, Value)>>> =
        cases.par_iter().map(|c| regold(c, mutant)).collect();
    let mut manifest = Vec::new();
    let mut failed = Vec::new();
    for (c, got) in cases.iter().zip(results) {
        let (updated, fixture, evidence) = match got {
            Ok(Some(x)) => x,
            Ok(None) => continue,
            Err(e) => {
                failed.push(format!("{}: {e}", c.stem));
                continue;
            }
        };
        println!("{evidence}");
        manifest.push(evidence);
        if !write {
            continue;
        }
        let name = c.path.file_name().unwrap();
        for (family, path, value) in [
            ("e2e", &c.path, Some(updated)),
            ("sched/e2e", &c.sched, fixture),
        ] {
            let Some(value) = value else { continue };
            let original = audit.join("originals").join(family).join(name);
            assert!(
                !original.exists(),
                "{}: kept from an earlier run",
                original.display()
            );
            std::fs::create_dir_all(original.parent().unwrap()).map_err(err)?;
            std::fs::copy(path, &original).map_err(err)?;
            save(path, &value)?;
            save(&audit.join("updated").join(family).join(name), &value)?;
        }
    }
    save(&audit.join("manifest.json"), &json!(manifest))?;
    println!(
        "{} cases, {} moved by the change, {} refused",
        cases.len(),
        manifest.len(),
        failed.len()
    );
    for f in &failed {
        println!("REFUSED {f}");
    }
    match failed.is_empty() {
        true => Ok(()),
        false => Err(format!("{} cases refused", failed.len())),
    }
}
