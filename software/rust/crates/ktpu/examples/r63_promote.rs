//! Regenerate timing-sensitive goldens with identical L2 and verifier witnesses.
//! Run one family and shard, appending the promotion audit to
//! `AUDIT_DIR/promoted-<family>-<shard>.json`:
//! cargo run -p ktpu --example r63_promote -- e2e 0 AUDIT_DIR

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use ktpu::{Backend, CardStack, Finding, Stack};
use ktpu_l1::sched::{self, runs, timing, Ruled};
use ktpu_l1::text::{emit_at, read, Expander, Module};
use ktpu_l2::{passes, Program, Search, Strategy, Target, PASSES};
use ktpu_machine::Machine;
use serde_json::{json, Value};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

fn load(path: &Path) -> Result<Value> {
    Ok(serde_json::from_str(&std::fs::read_to_string(path)?)?)
}

fn save(path: &Path, value: &Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, serde_json::to_string_pretty(value)? + "\n")?;
    Ok(())
}

fn paths(dir: &Path) -> Result<Vec<PathBuf>> {
    let mut out = std::fs::read_dir(dir)?
        .map(|p| p.map(|p| p.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    out.retain(|p| p.extension().is_some_and(|s| s == "json"));
    out.sort();
    Ok(out)
}

fn old_machine(m: &Machine) -> Result<Machine> {
    Ok(m.with_fact("vc.timing.xbar", json!(7))?
        .with_fact("vc.timing.unpack_math", json!(12))?
        .with_fact("vc.timing.seti", json!(2))?
        .with_fact("vc.timing.mx7_issue_gap", json!(0))?)
}

fn classification(old: &str, new: &str) -> &'static str {
    let bag = |s: &str| {
        let mut lines: Vec<_> = s.lines().map(str::trim).collect();
        lines.sort();
        lines.join("\n")
    };
    if bag(old) == bag(new) {
        "P"
    } else {
        "S"
    }
}

fn verdict(found: &[Finding]) -> Value {
    json!({"refused": found.iter().any(|f| Stack.refuses(f)),
        "findings": found.iter().map(|f| json!([f.kind, f.at, f.text])).collect::<Vec<_>>()})
}

fn witness(old: &Module, new: &Module, name: &str, m: &Machine) -> Result<Value> {
    let a = Stack.programs(old, name, None, m)?;
    let b = Stack.programs(new, name, None, m)?;
    let va = Stack.check(&a, m)?;
    let vb = Stack.check(&b, m)?;
    if va != vb {
        return Err(format!("{name}: verifier findings changed: {va:?} -> {vb:?}").into());
    }
    Ok(json!({"verdict": verdict(&vb), "findings": vb,
        "classification": classification(&old.source, &new.source),
        "old_l1": old.source, "new_l1": new.source}))
}

fn packages(got: &BTreeMap<u32, ktpu::PackageOut>, case: &Value, m: &Machine) -> Result<Value> {
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
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let f = ktpu_l0::Facts::of_mesh(m, *mesh)?;
        let parsed = ktpu_l0::Package::from_bytes(&p.bytes, &f)?;
        let bound = parsed.bind(&bindings)?;
        let lowered = ktpu_l0::engine::lower(&bound, &f)?;
        out.insert(
            mesh.to_string(),
            json!({
                "relocatable": ktpu::pipeline::cache::hex(&p.bytes), "bindings": bindings,
                "bound": ktpu::pipeline::cache::hex(&bound.to_bytes(&f)?),
                "lowered": ktpu::pipeline::cache::hex(&lowered.to_bytes(&f)?),
            }),
        );
    }
    Ok(Value::Object(out))
}

fn e2e(case: &mut Value, root: &Path, stem: &str) -> Result<Option<Value>> {
    let Some(text) = case["l2"].as_str() else {
        return Ok(None);
    };
    let m = Machine::named(case["machine"].as_str().unwrap())?;
    let old = old_machine(&m)?;
    let p = Stack.parse_l2(text)?;
    let name = case["name"].as_str().unwrap_or(&p.name).to_string();
    let compile = |m: &Machine| -> Result<(Program, Module)> {
        let passed = Stack.passes(&p, &name, m)?;
        let l1 = Stack.schedule(&Stack.lower(&passed, &name, m)?, m)?;
        Ok((passed, l1))
    };
    let (a, b) = match (compile(&old), compile(&m)) {
        (Ok(a), Ok(b)) => (a, b),
        (Err(_), Err(_)) => return Ok(None),
        (a, b) => {
            return Err(format!("{stem}: changed refusal: {:?} {:?}", a.err(), b.err()).into())
        }
    };
    // Compiled.l2 is the identical input L2, before timing-driven retile.
    // Preserve post-pass texts in the witness instead of confusing them with it.
    let sched_path = root.join(format!("software/rust/golden/sched/e2e/{stem}.json"));
    let existing = if sched_path.exists() {
        load(&sched_path)?["scheduled_ruled"]
            .as_str()
            .map(str::to_string)
    } else {
        None
    };
    if existing
        .as_deref()
        .unwrap_or(case["l1"].as_str().unwrap_or(""))
        == b.1.source
    {
        return Ok(None);
    }
    let mut evidence = witness(&a.1, &b.1, &name, &m)?;
    evidence["same_l2"] = json!(true);
    evidence["l2"] = json!(text);
    evidence["old_passed_l2"] = json!(a.0.text());
    evidence["new_passed_l2"] = json!(b.0.text());
    if evidence["verdict"]["refused"] == true {
        return Err(format!("{stem}: verifier refuses {evidence}").into());
    }
    let progs = Stack.programs(&b.1, &name, None, &m)?;
    let bound = matches!(Stack.order(&progs, &m), Err(e) if e.clause == "STACK.RELOC");
    let binds = bound.then(|| {
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
        ..Default::default()
    };
    let card = CardStack {
        binds,
        ..Default::default()
    };
    let progs = if bound {
        plain.programs(&b.1, &name, None, &m)?
    } else {
        progs
    };
    let pf = plain.check(&progs, &m)?;
    let cf = card.check(&progs, &m)?;
    let mut rulings = case["ruled"]["rulings"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    if !rulings.contains(&json!("RUL.063")) {
        rulings.push(json!("RUL.063"));
    }
    case["ruled"] = json!({"rulings": rulings,
        "packages": packages(&plain.order(&progs, &m)?, case, &m)?,
        "verdict": verdict(&pf),
        "card": {"fetch": [m.die.fetch_port.0, m.die.fetch_port.1],
            "packages": packages(&card.order(&progs, &m)?, case, &m)?, "verdict": verdict(&cf)}});
    if sched_path.exists() {
        let mut fixture = load(&sched_path)?;
        fixture["machine"] = case["machine"].clone();
        fixture["text"] = json!(Stack.lower(&b.0, &name, &m)?.source);
        fixture["scheduled_ruled"] = json!(a.1.source);
        // Refresh the input and all costs together; the fixture remains executable.
        schedule(&mut fixture, root, stem)?;
        save(&sched_path, &fixture)?;
    }
    case["l1"] = json!(b.1.source);
    case["timing_measured"] = json!("RUL.063");
    Ok(Some(evidence))
}

fn lower(case: &mut Value, _root: &Path, stem: &str) -> Result<Option<Value>> {
    let Some(text) = case["l2"].as_str().map(str::to_string) else {
        return Ok(None);
    };
    if case.get("skipped").is_some() {
        return Ok(None);
    }
    let m = Machine::named(case["machine"].as_str().unwrap())?;
    let old = old_machine(&m)?;
    let name = case["name"].as_str();
    let compile = |m: &Machine, ps: &[&str], strategy| -> Result<ktpu_l2::Compiled> {
        let t = Target::of(m).with_nodes(case["nodes"].as_i64().unwrap_or(m.card.nodes as i64));
        let p = Program::parse(&text, "<golden.l2>", name)?;
        Ok(ktpu_l2::compile_with(
            &p,
            &t,
            ps,
            false,
            &Search::new(strategy),
        )?)
    };
    let (a, b) = match (
        compile(&old, &PASSES, Strategy::Auto),
        compile(&m, &PASSES, Strategy::Auto),
    ) {
        (Ok(a), Ok(b)) => (a, b),
        (Err(_), Err(_)) => return Ok(None),
        _ => return Err(format!("{stem}: lower refusal changed").into()),
    };
    // Input L2 is shared; retile decisions may legitimately differ.
    let mut changes = Vec::new();
    if case["l1"] != b.l1_text || a.l1_text != b.l1_text {
        let w = witness(&a.l1, &b.l1, &b.l2.name, &m)?;
        if w["verdict"]["refused"] == true {
            return Err(format!("{stem}: refused lower output {w}").into());
        }
        changes.push(w);
    }
    let scheduled = sched::schedule_module(&b.l1, &m)?;
    let scheduled_changed =
        case["l1_scheduled"].is_string() && case["l1_scheduled"] != scheduled.source;
    let variant = if let Some(v) = case.get("variant").filter(|v| v["l1"].is_string()) {
        let ps = v["passes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap())
            .collect::<Vec<_>>();
        let strategy = Strategy::parse(v["strategy"].as_str().unwrap()).unwrap();
        let va = compile(&old, &ps, strategy)?;
        let vb = compile(&m, &ps, strategy)?;
        // The variant also has identical input, with timing-driven passes.
        if v["l1"] != vb.l1_text || va.l1_text != vb.l1_text {
            changes.push(witness(&va.l1, &vb.l1, &vb.l2.name, &m)?);
            Some(vb.l1_text)
        } else {
            None
        }
    } else {
        None
    };
    let mut passed = Program::parse(&text, "<golden.l2>", name)?;
    let target = Target::of(&m).with_nodes(case["nodes"].as_i64().unwrap_or(m.card.nodes as i64));
    passed = passed.with_vregs(target.vregs, target.vcopies);
    let search = Search::new(Strategy::Auto);
    let mut pass_changed = false;
    if let Some(ps) = case["passes"].as_array_mut() {
        for step in ps {
            passed = passes::pass(step["pass"].as_str().unwrap(), &passed, &target, &search)?;
            if step["text"].is_string() {
                pass_changed |= step["text"] != passed.text();
                step["text"] = json!(passed.text());
            }
        }
    }
    let old_scheduled = sched::schedule_module(&a.l1, &old)?;
    if scheduled_changed
        || (case["l1_scheduled"].is_string() && old_scheduled.source != scheduled.source)
    {
        let mut w = witness(&old_scheduled, &scheduled, &b.l2.name, &m)?;
        w["artifact"] = json!("l1_scheduled");
        changes.push(w);
    }
    if changes.is_empty() && !pass_changed {
        return Ok(None);
    }
    if changes.is_empty() {
        changes.push(witness(&old_scheduled, &scheduled, &b.l2.name, &m)?);
    }
    for w in &changes {
        if w["verdict"]["refused"] == true {
            return Err(format!("{stem}: refused artifact {w}").into());
        }
    }
    case["l1"] = json!(b.l1_text);
    if case["l1_scheduled"].is_string() {
        case["l1_scheduled"] = json!(scheduled.source);
    }
    if let Some(v) = variant {
        case["variant"]["l1"] = json!(v);
    }
    Ok(Some(json!({"witnesses": changes})))
}

fn module_witness(old: &Module, new: &Module, m: &Machine) -> Result<Value> {
    let mut out = Vec::new();
    for ((name, level), _) in &new.fns {
        if level == "l1" {
            out.push(witness(old, new, name, m)?);
        }
    }
    if out.is_empty() {
        for (name, img) in &new.images {
            let args = vec!["0"; img.params.len()].join(", ");
            let tail = format!(
                "\nfn r63_check.l1()\n    send vc[0]\n        run {name}({args})\n    barrier\n"
            );
            out.push(witness(
                &read(&(old.source.clone() + &tail), "<old>")?,
                &read(&(new.source.clone() + &tail), "<new>")?,
                "r63_check",
                m,
            )?);
        }
    }
    Ok(json!(out))
}

fn schedule(case: &mut Value, _root: &Path, _stem: &str) -> Result<Option<Value>> {
    if case["refusal"].is_string() {
        return Ok(None);
    }
    let Some(text) = case["text"].as_str() else {
        return Ok(None);
    };
    let m = Machine::named(case["machine"].as_str().unwrap_or("multimesh_v9t6"))?;
    let module = read(text, "<sched>")?;
    let scheduled = sched::schedule_module(&module, &m)?;
    let previous = case.clone();
    let r = module.reader();
    let macros = module.macro_table();
    let ruled = Ruled::new(&r, &m, module.vregs, module.vcopies)?;
    let fill = (m.vc.fill_alone.0 as i64, m.vc.fill_alone.1 as i64);
    for im in case["images"].as_array_mut().unwrap() {
        let name = im["name"].as_str().unwrap().to_string();
        let img = &module.images[&name];
        let body = Expander::new(&macros, &r, true).stmts(&img.stmts, &HashMap::new())?;
        let mut run_records = Vec::new();
        for run in runs(&body) {
            let order = ruled.order(&run)?;
            let all = (0..run.len()).collect::<Vec<_>>();
            let text = run
                .iter()
                .map(|s| emit_at(std::slice::from_ref(s), 0, 48).trim().to_string())
                .collect::<Vec<_>>();
            let preds = sched::preds(&run, m.vc.chunks)
                .iter()
                .map(|p| {
                    let mut p = p.iter().copied().collect::<Vec<_>>();
                    p.sort();
                    p
                })
                .collect::<Vec<_>>();
            run_records.push(json!({"stmts": text, "preds": preds,
                "ruled": {"order": order, "written": ruled.cycles(&run, &all)?,
                "cycles": ruled.cycles(&run, &order)?}}));
        }
        im["runs"] = json!(run_records);
        let head = format!("image {}({})\n", img.name, img.params.join(", "));
        let written = format!("{head}{}", emit_at(&body, 1, 48));
        im["written"] = json!(written);
        let tuned = format!("{head}{}", emit_at(&ruled.block(&body)?, 1, 48));
        im["ruled"] = json!(tuned);
        im["cycles_written"] = json!(timing::cycles(
            &written,
            &name,
            module.vregs,
            module.vcopies,
            fill,
            &m
        )?);
        im["cycles_tuned"] = json!(timing::cycles(
            &tuned,
            &name,
            module.vregs,
            module.vcopies,
            fill,
            &m
        )?);
        im["chosen"] = json!(format!(
            "{head}{}",
            emit_at(&scheduled.images[&name].stmts, 1, 48)
        ));
    }
    case["scheduled_ruled"] = json!(scheduled.source);
    if *case == previous {
        return Ok(None);
    }
    let old = read(previous["scheduled_ruled"].as_str().unwrap(), "<previous>")?;
    Ok(Some(
        json!({"classification": classification(&old.source, &scheduled.source),
        "witnesses": module_witness(&old, &scheduled, &m)?}),
    ))
}

fn l1(case: &mut Value, _root: &Path, _stem: &str) -> Result<Option<Value>> {
    let Some(text) = case["text"].as_str() else {
        return Ok(None);
    };
    if !case["scheduled"].is_string() {
        return Ok(None);
    }
    let m = Machine::named(case["machine"].as_str().unwrap())?;
    let module = read(text, "<golden.l1>")?;
    let new = sched::schedule_module(&module, &m)?;
    if case["scheduled"] == new.source {
        return Ok(None);
    }
    let old = read(case["scheduled"].as_str().unwrap(), "<previous>")?;
    let evidence = module_witness(&old, &new, &m)?;
    case["scheduled"] = json!(new.source);
    Ok(Some(
        json!({"classification": classification(&old.source, &new.source), "witnesses": evidence}),
    ))
}

fn main() -> Result<()> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let args: Vec<_> = std::env::args().collect();
    let family = args.get(1).expect("e2e, l2, l1, sched/l1 or sched/e2e");
    let shard: usize = args.get(2).expect("shard 0..15").parse()?;
    let audit_dir = PathBuf::from(args.get(3).expect("audit directory"));
    let mut audit = Vec::new();
    for (i, path) in paths(&root.join(format!("software/rust/golden/{family}")))?
        .iter()
        .enumerate()
    {
        if i % 16 != shard {
            continue;
        }
        let mut case = load(path)?;
        let previous = case.clone();
        let stem = path.file_stem().unwrap().to_str().unwrap();
        let result = match family.as_str() {
            "e2e" => e2e(&mut case, &root, stem),
            "l2" => lower(&mut case, &root, stem),
            "l1" => l1(&mut case, &root, stem),
            "sched/l1" | "sched/e2e" => schedule(&mut case, &root, stem),
            _ => panic!("unknown family"),
        };
        let evidence = result.map_err(|e| format!("{family}/{stem}: {e}"))?;
        if let Some(evidence) = evidence {
            let fields = previous
                .as_object()
                .unwrap()
                .iter()
                .filter(|(k, v)| case[*k] != **v)
                .map(|(k, _)| k.clone())
                .collect::<Vec<_>>();
            audit.push(
                json!({"case": stem, "family": family, "fields": fields, "evidence": evidence}),
            );
            save(path, &case)?;
            println!("{family}/{stem}: promoted");
        }
    }
    let audit_path = audit_dir.join(format!(
        "promoted-{}-{shard:02}.json",
        family.replace('/', "-")
    ));
    if audit_path.exists() {
        let mut previous = load(&audit_path)?.as_array().unwrap().clone();
        previous.extend(audit);
        audit = previous;
    }
    save(&audit_path, &json!(audit))?;
    Ok(())
}
