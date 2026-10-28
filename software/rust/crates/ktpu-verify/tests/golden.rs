//! The Python checker's goldens (`software/rust/golden/verify_dump.py`):
//! every case's packages bound, ordered by the scheduler, and checked, each
//! against what the Python stack gave.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use ktpu_l0::{Facts, Package};
use ktpu_machine::Machine;
use ktpu_verify::{
    check, check_scheduled, l1_touched, schedule, Finding, Held, HeldCore, Nodes, Target,
};
use serde_json::Value;

/// Python seconds past which a case runs in an optimised build only.
const HEAVY: f64 = 2.0;

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).expect("hex"))
        .collect()
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn pair(v: &Value) -> (u8, u8) {
    (v[0].as_u64().unwrap() as u8, v[1].as_u64().unwrap() as u8)
}

fn held_of(v: &Value) -> Vec<HeldCore> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|c| HeldCore {
            coord: pair(&c[0]),
            scalars: BTreeMap::new(),
            imem: c[1]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| (r[0].as_i64().unwrap(), r[1].as_u64().unwrap()))
                .collect(),
            desc: c[2]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| {
                    (
                        (r[0].as_i64().unwrap(), r[1].as_i64().unwrap()),
                        r[2].as_i64().unwrap(),
                    )
                })
                .collect(),
        })
        .collect()
}

fn target_of(g: &Value) -> Result<(Target, Facts), String> {
    let name = g["machine"].as_str().unwrap();
    let m = Machine::named(name).map_err(|e| e.to_string())?;
    let want = &g["target"];
    let meshes = want["meshes"].as_array().unwrap();
    let t = Target::from_machine(&m, Some(meshes.len() as u32))?;
    for (m, w) in t.meshes.iter().zip(meshes) {
        let list =
            |k: &str| -> Vec<(u8, u8)> { w[k].as_array().unwrap().iter().map(pair).collect() };
        if m.index as u64 != w["index"].as_u64().unwrap()
            || m.mg != list("mg")
            || m.vc != list("vc")
            || m.sr != list("sr")
            || m.mem_ports != list("mem_ports")
        {
            return Err(format!("mesh {} differs from the golden's: {w}", m.index));
        }
    }
    if t.facts.vc_regs as u64 != want["vregs"].as_u64().unwrap() {
        return Err("vc.regs differs from the golden's".into());
    }
    Ok((t, Facts::of(&m)))
}

fn findings_json(found: &[ktpu_verify::Finding]) -> Value {
    Value::from(
        found
            .iter()
            .map(|f| Value::from(vec![f.kind.name(), &f.at, &f.text]))
            .collect::<Vec<_>>(),
    )
}

/// What differs between a case and the golden (empty when nothing does).
fn case(g: &Value) -> Result<Vec<String>, String> {
    let (t, f) = target_of(g)?;
    let mut bad = Vec::new();
    let corrupt_cfg = g["vector"] == "isa/vector/ISA.VC.103.rel"
        && g["mutant"] == "isa.vector/cfg_fields"
        && !ktpu_verify::mutant::on("verify.vector/off");
    let mut runs: Vec<Nodes> = Vec::new();
    let mut first = Nodes::new();
    for (r, run) in g["runs"].as_array().unwrap().iter().enumerate() {
        let mut nodes = Nodes::new();
        let mut held = Held::new();
        let mut want_order = BTreeMap::new();
        for (mesh, m) in run.as_object().unwrap() {
            let mesh: u32 = mesh.parse().unwrap();
            let reloc = Package::from_bytes(&unhex(m["relocatable"].as_str().unwrap()), &f)
                .map_err(|e| e.to_string())?;
            let bindings: Vec<u64> = m["bindings"]
                .as_array()
                .unwrap()
                .iter()
                .map(|b| b.as_u64().unwrap())
                .collect();
            let bound = reloc.bind(&bindings).map_err(|e| e.to_string())?;
            if hex(&bound.to_bytes(&f).map_err(|e| e.to_string())?) != m["bound"].as_str().unwrap()
            {
                bad.push(format!("run {r} mesh {mesh}: bound bytes differ"));
            }
            nodes.insert(mesh, bound);
            held.insert(mesh, held_of(&m["held"]));
            if let Some(o) = m["ordered"].as_str() {
                want_order.insert(mesh, o.to_string());
            }
        }
        if !want_order.is_empty() {
            match schedule(&nodes, &held, &t) {
                Err(e) if corrupt_cfg => {
                    if !e
                        .findings
                        .iter()
                        .any(|f| f.kind == ktpu_verify::Kind::Vector)
                    {
                        bad.push(format!(
                            "corrupted configuration did not refuse as vector: {e:?}"
                        ));
                    }
                }
                Err(e) => bad.push(format!("run {r}: schedule failed: {:?}", e.findings)),
                Ok(s) => {
                    for (mesh, want) in &want_order {
                        let got = hex(&s.packages[mesh].to_bytes(&f).map_err(|e| e.to_string())?);
                        if &got != want {
                            bad.push(format!("run {r} mesh {mesh}: order differs"));
                        }
                        if r == 0 {
                            first.insert(*mesh, s.packages[mesh].clone());
                        }
                    }
                }
            }
        }
        runs.push(nodes);
    }
    let found = check(&runs, &t);
    if corrupt_cfg {
        if !found.iter().any(|f| {
            f.kind == ktpu_verify::Kind::Vector && f.text.contains("reserved configuration bits")
        }) {
            bad.push(format!(
                "corrupted configuration was not statically refused: {found:?}"
            ));
        }
        #[cfg(feature = "mutants")]
        bad.extend(ktpu_verify::mutant::with("verify.vector/off", || case(g))?);
        return Ok(bad);
    }
    if let Ok(s) = schedule(&runs[0], &Held::new(), &t) {
        if check_scheduled(&s, &runs[1..], &t) != found {
            bad.push("check_scheduled differs from check".into());
        }
    }
    let vector = g["vector"].as_str().unwrap_or("");
    if vector.starts_with("isa/rulings/") {
        // A ruled case: the ruling's vector, not the Python stack, is the
        // expectation (BRIEF.md "Contract").
        ruled(vector, &found, &first, &t, &mut bad);
        return Ok(bad);
    }
    if vector.starts_with("isa/") && g["mutant"].is_null() {
        // The `isa.verify` runner's output is the vector's expect.
        let path = root()
            .join("../tests/spec/vectors")
            .join(format!("{vector}.json"));
        let v: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        let want_l1 = v["input"]["l1"].as_bool().unwrap_or(false);
        let got = ktpu_verify::spec::verify_json(&runs, &t, want_l1);
        if got != v["expect"] {
            bad.push(format!("{vector}: runner output {got}"));
        }
    }
    let got = findings_json(&found);
    if got != g["findings"] {
        bad.push(format!(
            "findings differ:\n  got  {got}\n  want {}",
            g["findings"]
        ));
    }
    if let Some(want) = g["l1"].as_object() {
        let mut got = serde_json::Map::new();
        for (name, r, w) in l1_touched(&first, &t) {
            if !r.is_empty() || !w.is_empty() {
                got.insert(name, serde_json::json!({"read": r, "written": w}));
            }
        }
        if &got != want {
            bad.push(format!("l1 differs: got {got:?}"));
        }
    }
    Ok(bad)
}

/// The `isa.verify` runner's output for a ruled vector, against its expect:
/// the finding kinds, and each core's L1 words by core index.
fn ruled(vector: &str, found: &[Finding], first: &Nodes, t: &Target, bad: &mut Vec<String>) {
    let path = root()
        .join("../tests/spec/vectors")
        .join(format!("{vector}.json"));
    let v: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    let want = &v["expect"];
    let kinds: BTreeSet<&str> = found.iter().map(|f| f.kind.name()).collect();
    if Value::from(kinds.into_iter().collect::<Vec<_>>()) != want["findings"] {
        bad.push(format!("{vector}: findings {found:?}"));
    }
    let vcs = &t.meshes[0].vc;
    let mut l1 = serde_json::Map::new();
    for (name, r, w) in l1_touched(first, t) {
        let xy = vcs
            .iter()
            .position(|&(x, y)| name == format!("vc(0:{x},{y})"))
            .map(|i| format!("vc{i}"));
        if let (Some(k), false) = (xy, r.is_empty() && w.is_empty()) {
            l1.insert(k, serde_json::json!({"read": r, "written": w}));
        }
    }
    if Value::Object(l1.clone()) != want["l1"] {
        bad.push(format!("{vector}: l1 {l1:?}"));
    }
}

#[test]
fn golden_verify() {
    let dir = root().join("golden/verify");
    let mut paths: Vec<PathBuf> = match fs::read_dir(&dir) {
        Ok(rd) => rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "json"))
            .collect(),
        Err(_) => Vec::new(),
    };
    paths.sort();
    // KTPU_GOLDEN_ONLY: run only the cases whose file name holds it.
    if let Ok(only) = std::env::var("KTPU_GOLDEN_ONLY") {
        paths.retain(|p| p.to_string_lossy().contains(&only));
    }
    // KTPU_GOLDEN_SHARD=i/n: cases i, i+n, ... (each run under the shell's
    // time limit); unset, every case.
    if let Ok(s) = std::env::var("KTPU_GOLDEN_SHARD") {
        let (i, n) = s.split_once('/').expect("KTPU_GOLDEN_SHARD=i/n");
        let (i, n): (usize, usize) = (i.parse().unwrap(), n.parse().unwrap());
        paths = paths.into_iter().skip(i).step_by(n).collect();
    }
    let (mut total, mut passed, mut skipped, mut deferred) = (0, 0, 0, 0);
    let mut fails = Vec::new();
    for p in &paths {
        let g: Value = serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap();
        if g.get("skipped").is_some() || !g["refusal"].is_null() || g["runs"].is_null() {
            skipped += 1;
            continue;
        }
        // An unoptimised build runs the abstract machine ~10x slower: the
        // kernels Python took over HEAVY seconds on run in `--release` only.
        let heavy = g["seconds"].as_f64().unwrap_or(0.0) > HEAVY;
        if cfg!(debug_assertions) && heavy && std::env::var_os("KTPU_GOLDEN_ALL").is_none() {
            deferred += 1;
            continue;
        }
        total += 1;
        let t0 = std::time::Instant::now();
        let got = case(&g);
        let took = t0.elapsed().as_secs_f64();
        if took > 0.5 {
            eprintln!("  {:.2}s {}", took, g["case"]);
        }
        match got {
            Ok(bad) if bad.is_empty() => passed += 1,
            Ok(bad) => fails.push(format!("{}:\n {}", g["case"], bad.join("\n "))),
            Err(e) => fails.push(format!("{}: {e}", g["case"])),
        }
    }
    eprintln!(
        "verify goldens: {passed} / {total} pass, {skipped} without a verdict, \
         {deferred} heavy left to --release"
    );
    assert!(fails.is_empty(), "{}", fails.join("\n"));
}
