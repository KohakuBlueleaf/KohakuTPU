//! `tg.run`'s `grep` and `params`: what the compiled L2 of `make`'s kernels
//! holds (`runners/api.py` `run_run`), past the planner: each launch's graph
//! through ktpu-graph, which ktpu-plan's port does not reach.

use ktpu_machine::Machine;
use serde_json::{json, Map, Value};

/// The mutants a hosted `tg.run` switches: ktpu-plan's and ktpu-kernels'.
pub fn plan_mutants() -> Vec<&'static str> {
    let mut out = ktpu_plan::spec::mutants();
    for n in ktpu_kernels::mutants::NAMES {
        if !out.contains(n) {
            out.push(n);
        }
    }
    out
}

/// ktpu-plan's skipped mutants that no hosted knob covers.
pub fn plan_skipped_mutants() -> Vec<(&'static str, &'static str)> {
    let knobs = plan_mutants();
    ktpu_plan::spec::skipped_mutants()
        .into_iter()
        .filter(|(n, _)| !knobs.contains(n))
        .collect()
}

/// ktpu-plan's suite `run`, with `tg.run`'s `grep` and `params` facts made
/// here from the launches' graphs.
pub fn plan_run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    let facts: Vec<&str> = input
        .get("facts")
        .and_then(Value::as_array)
        .map_or_else(Vec::new, |fs| fs.iter().filter_map(Value::as_str).collect());
    let hosted = |f: &&str| matches!(*f, "grep" | "params");
    if runner != "tg.run" || !facts.iter().any(hosted) {
        return ktpu_plan::spec::run(runner, input, machine, mutant);
    }
    let go = || {
        let mut got = ktpu_plan::spec::run(runner, input, machine, None)?;
        let more = compiled_facts(input, &facts)?;
        let obj = got.as_object_mut().ok_or("tg.run answers an object")?;
        obj.extend(
            more.as_object()
                .into_iter()
                .flatten()
                .map(|(k, v)| (k.clone(), v.clone())),
        );
        Ok(got)
    };
    let Some(name) = mutant else {
        return go();
    };
    let plan = ktpu_plan::mutant::NAMES
        .iter()
        .copied()
        .find(|n| *n == name);
    let kernels = ktpu_kernels::mutants::NAMES
        .iter()
        .copied()
        .find(|n| *n == name);
    match (plan, kernels) {
        (Some(p), Some(k)) => ktpu_kernels::mutants::with(k, || ktpu_plan::mutant::with(p, go)),
        (Some(p), None) => ktpu_plan::mutant::with(p, go),
        (None, Some(k)) => ktpu_kernels::mutants::with(k, go),
        (None, None) => Err(format!("unknown mutant {name}")),
    }
}

/// `grep` (run prefix -> pattern -> whether any of its kernels' L2 texts
/// holds it) and `params` (run prefix -> each kernel's `[name, type]`s).
fn compiled_facts(input: &Value, facts: &[&str]) -> Result<Value, String> {
    let r = ktpu_plan::spec::realized(input)?;
    let mut kernels = Vec::new();
    for l in &r.launches {
        let Some(g) = &l.graph else { continue };
        let text = ktpu_graph::lower(g).map_err(|e| e.to_string())?.text();
        let params: Vec<Value> = ktpu_graph::term::params(g)
            .iter()
            .map(|(n, t)| json!([n, ktpu_l1::text::fmt(t)]))
            .collect();
        kernels.push((l.name.as_str(), text, params));
    }
    let of = |prefix: &str| {
        let head = format!("{prefix}_");
        kernels.iter().filter(move |(n, _, _)| n.starts_with(&head))
    };
    let mut got = Map::new();
    if facts.contains(&"grep") {
        let mut grep = Map::new();
        let asked = input["grep"].as_object().ok_or("no `grep` map")?;
        for (prefix, pats) in asked {
            let mut found = Map::new();
            for p in pats.as_array().ok_or("a `grep` pattern list")? {
                let p = p.as_str().ok_or("a `grep` pattern")?;
                let re = regex::RegexBuilder::new(p)
                    .multi_line(true)
                    .build()
                    .map_err(|e| format!("pattern {p:?}: {e}"))?;
                found.insert(p.into(), json!(of(prefix).any(|(_, t, _)| re.is_match(t))));
            }
            grep.insert(prefix.clone(), Value::Object(found));
        }
        got.insert("grep".into(), Value::Object(grep));
    }
    if facts.contains(&"params") {
        let mut params = Map::new();
        for prefix in input["params"].as_array().ok_or("no `params` list")? {
            let prefix = prefix.as_str().ok_or("a `params` prefix")?;
            let each: Vec<Value> = of(prefix).map(|(_, _, ps)| json!(ps)).collect();
            params.insert(prefix.into(), Value::Array(each));
        }
        got.insert("params".into(), Value::Object(params));
    }
    Ok(Value::Object(got))
}
