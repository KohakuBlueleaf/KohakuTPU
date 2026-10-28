//! Rust ports of the planner's tier-0 runners (`software/tests/spec/runners/
//! api.py` `tg.plan`, `tg.run`), in ktpu-isa's `spec` shape: a port takes a
//! vector's `input` and returns what the Python runner returns.
//!
//! The front end stays Python (tinygrad's schedule, the seam's UOp capture,
//! the driver's storage records), so a port reads what it hands the planner
//! from the plan golden of the vector with that `input` (`golden/plan`,
//! `plan_dump.py`): each ast's `ktpu.uops`, each run's `ktpu.state`, where
//! the runner's `make` began (`realized`). The plans, steps and launches
//! are this crate's; the runs and their order stay the recorded ones.

pub mod mutant;

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use ktpu_machine::Machine;
use serde_json::{json, Map, Value};

use crate::fold::Ty;
use crate::input::Buffer;
use crate::program::{self, Access, Case, ExprNode, Launch, Range, Step};
use crate::{class, lower, run};

/// Every mutant ktpu-plan carries.
pub fn mutants() -> Vec<&'static str> {
    mutant::NAMES.to_vec()
}

/// Mutants a ported vector names that no crate carries.
pub fn skipped_mutants() -> Vec<(&'static str, &'static str)> {
    vec![]
}

/// A runner port: `(input, machine) -> output or why it cannot run`.
pub type Port = fn(&Value, Option<&Machine>) -> Result<Value, String>;

const PORTS: &[(&str, Port)] = &[("tg.plan", tg_plan), ("tg.run", tg_run)];

/// Every ported runner's name.
pub fn runners() -> Vec<&'static str> {
    PORTS.iter().map(|(n, _)| *n).collect()
}

/// Every ported runner, by its runner name, without a mutant.
pub fn ports() -> Vec<(&'static str, Port)> {
    PORTS.to_vec()
}

/// Run runner `runner` on `input`, under `mutant` when given (one of
/// [`mutants`]; a name ktpu-kernels also carries is switched there too).
pub fn run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    let port = PORTS
        .iter()
        .find(|(n, _)| *n == runner)
        .map(|(_, p)| *p)
        .ok_or_else(|| format!("runner {runner} has no Rust port"))?;
    let Some(name) = mutant else {
        return port(input, machine);
    };
    let Some(name) = mutant::NAMES.iter().copied().find(|n| *n == name) else {
        return Err(format!("unknown mutant {name}"));
    };
    #[cfg(feature = "mutants")]
    {
        let go = || mutant::with(name, || port(input, machine));
        match ktpu_kernels::mutants::NAMES
            .iter()
            .copied()
            .find(|n| *n == name)
        {
            Some(k) => ktpu_kernels::mutants::with(k, go),
            None => go(),
        }
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = name;
        Err("ktpu-plan is built without the `mutants` feature".into())
    }
}

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(read) = std::fs::read_dir(dir) else {
        return;
    };
    for e in read.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, out);
        } else if p.extension().is_some_and(|x| x == "json") {
            out.push(p);
        }
    }
}

/// A ported vector's `input` (its JSON text) -> its plan golden.
fn goldens() -> &'static HashMap<String, PathBuf> {
    static INDEX: OnceLock<HashMap<String, PathBuf>> = OnceLock::new();
    INDEX.get_or_init(|| {
        let mut files = Vec::new();
        walk(&root().join("tests/spec/vectors"), &mut files);
        files.sort();
        let plan = root().join("rust/golden/plan");
        let mut out = HashMap::new();
        for f in files {
            let Ok(text) = std::fs::read_to_string(&f) else {
                continue;
            };
            let Ok(v) = serde_json::from_str::<Value>(&text) else {
                continue;
            };
            let ported = v["runner"].as_str().is_some_and(|r| runners().contains(&r));
            if ported {
                let stem = f.file_stem().unwrap().to_string_lossy();
                out.entry(v["input"].to_string())
                    .or_insert_with(|| plan.join(format!("{stem}.json")));
            }
        }
        out
    })
}

/// The plan golden recorded for `input`.
fn case_of(input: &Value) -> Result<Case, String> {
    let path = goldens()
        .get(&input.to_string())
        .ok_or("no ported vector has this input (no plan golden records it)")?;
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    serde_json::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))
}

// ------------------------------------------------------------------ tg.plan
/// `tg.plan`: each recorded ast as `lower::plan` plans it (the facts in
/// `keys`), or `{"refused": message}`, sorted by their JSON text.
fn tg_plan(inp: &Value, _: Option<&Machine>) -> Result<Value, String> {
    let case = case_of(inp)?;
    let keys: Vec<String> = match inp.get("keys").and_then(Value::as_array) {
        Some(ks) => ks
            .iter()
            .filter_map(|k| k.as_str().map(String::from))
            .collect(),
        None => vec!["kind".into()],
    };
    let mut out: Vec<Value> = Vec::new();
    for p in &case.plans {
        out.push(match lower::plan(&p.uops) {
            Ok(l) => {
                let f = plan_facts(&l);
                Value::Object(
                    keys.iter()
                        .map(|k| (k.clone(), f.get(k).cloned().unwrap_or(Value::Null)))
                        .collect(),
                )
            }
            Err(r) => json!({ "refused": r.message }),
        });
    }
    out.sort_by_cached_key(py_dumps);
    Ok(Value::Array(out))
}

/// `plan_facts`: a plan's kind, output, expression, reads (slot or `table`,
/// element type, strides by output range `oN` / reduced range `kN`,
/// offset), reduced extents; a product's M, N, K, batches and A / B; a
/// constant's distinct values.
pub fn plan_facts(l: &lower::Lowered) -> Map<String, Value> {
    let p = lower::plan_json(&l.graph, &l.plan);
    let label = |r: &Range| -> String {
        if let Some(i) = p.out_ranges.iter().position(|x| x == r) {
            format!("o{i}")
        } else if let Some(i) = p.reduce_ranges.iter().position(|x| x == r) {
            format!("k{i}")
        } else {
            format!("r{}", r.extent)
        }
    };
    let read = |a: &Access| -> Value {
        let mut coef: Vec<(String, i64)> = a
            .coef
            .iter()
            .filter(|(_, c)| *c != 0)
            .map(|(r, c)| (label(r), *c))
            .collect();
        coef.sort();
        let slot = if a.slot.is_null() { "table" } else { "buf" };
        json!([slot, a.dtype, coef, a.off])
    };
    let extents = |rs: &[Range]| -> Vec<i64> { rs.iter().map(|r| r.extent).collect() };
    let mut out = Map::new();
    out.insert("kind".into(), json!(p.kind));
    out.insert("shape".into(), json!(p.out_shape));
    out.insert("dtype".into(), json!(p.out_dtype));
    out.insert(
        "expr".into(),
        p.expr
            .as_ref()
            .map_or(Value::Null, |e| json!(expr_text(e, e.root))),
    );
    out.insert(
        "reads".into(),
        Value::Array(p.accesses.iter().map(read).collect()),
    );
    out.insert("reduce".into(), json!(extents(&p.reduce_ranges)));
    if p.kind == "matmul" {
        if let (Some((m, n, ks)), Some(a), Some(b)) = (&p.mnk, &p.a, &p.b) {
            out.insert("mnk".into(), json!([m.extent, n.extent, extents(ks)]));
            out.insert(
                "batch".into(),
                json!(extents(p.batch.as_deref().unwrap_or(&[]))),
            );
            out.insert("ab".into(), json!([read(a), read(b)]));
        }
    }
    if p.kind == "const" {
        if let Some(v) = &l.plan.value {
            out.insert("value".into(), unique(v));
        }
    }
    out
}

/// `np.unique(value).tolist()`.
fn unique(a: &crate::fold::Arr) -> Value {
    match a.ty {
        Ty::F16 | Ty::F32 | Ty::F64 => {
            let mut xs = a.floats.clone();
            xs.sort_by(f64::total_cmp);
            xs.dedup_by(|x, y| x.total_cmp(y).is_eq());
            Value::Array(xs.into_iter().map(crate::expr::number).collect())
        }
        Ty::Bool => {
            let mut xs: Vec<bool> = a.ints.iter().map(|i| *i != 0).collect();
            xs.sort();
            xs.dedup();
            json!(xs)
        }
        _ => {
            let mut xs = a.ints.clone();
            xs.sort();
            xs.dedup();
            Value::Array(xs.into_iter().map(|i| json!(i as i64)).collect())
        }
    }
}

/// `expr_text`: `L0`, a number's repr, `op(a, b)`, `sum(a)`.
fn expr_text(e: &program::Expr, at: usize) -> String {
    match &e.nodes[at] {
        ExprNode::Load { load } => format!("L{load}"),
        ExprNode::Const { r#const } => match r#const {
            Value::String(s) => s.clone(),
            v => ktpu_graph::text::py_float(v.as_f64().unwrap_or(f64::NAN)),
        },
        ExprNode::Op { op, args } => {
            let args: Vec<String> = args.iter().map(|a| expr_text(e, *a)).collect();
            format!("{op}({})", args.join(", "))
        }
        ExprNode::Reduce { reduce, arg } => format!("{reduce}({})", expr_text(e, *arg)),
    }
}

// ------------------------------------------------------------------- tg.run
/// What `make`'s realize ran: every launch in order and the output's base
/// storage record after (the last run's output slot).
pub struct Realized {
    pub launches: Vec<Launch>,
    pub out: Option<Buffer>,
}

/// The runs of the vector with `input`, each planned and run by this crate
/// in order; the launches from its `make` on. A run starts from its
/// recorded state, each base the driver left as the last run did taking
/// the record this crate's run left (so a mutated run's forms carry on); a
/// refusal as Python prints it.
pub fn realized(input: &Value) -> Result<Realized, String> {
    let case = case_of(input)?;
    let m = case.machine();
    let from = case.realized.last().copied().unwrap_or(0);
    let mut got = Realized {
        launches: Vec::new(),
        out: None,
    };
    // base -> (the record Python's last run left, the one Rust's left)
    let mut left: BTreeMap<String, (Buffer, Buffer)> = BTreeMap::new();
    for (i, r) in case.runs.iter().enumerate() {
        let mut state = r.state.clone();
        for (b, rec) in state.buffers.iter_mut() {
            if let Some((_, rs)) = left.get(b).filter(|(py, _)| py == rec) {
                *rec = rs.clone();
            }
        }
        let l = lower::plan(&case.plans[r.plan].uops).map_err(|e| class::text(&e))?;
        let base = match &l.plan.out_slot {
            lower::Slot::Param(n) => state.slots.get(&n.to_string()).map(|s| s.base.clone()),
            _ => None,
        };
        let x = run::execute(l, &state, &m).map_err(|(e, _)| class::text(&e))?;
        let bases: Vec<String> = r.end.keys().cloned().collect();
        for (b, rs) in x.records(&bases) {
            left.insert(b.clone(), (r.end[&b].clone(), rs));
        }
        if i < from {
            continue;
        }
        got.launches.extend(x.steps.iter().filter_map(|s| match s {
            Step::Launch(l) => Some((**l).clone()),
            _ => None,
        }));
        got.out = base.and_then(|b| x.records(&[b]).into_values().next());
    }
    Ok(got)
}

/// `tg.run`: `make`'s kernels (name prefixes, kinds, rows views, counts),
/// its output's forms and host bytes. `grep` and `params` read the compiled
/// L2, past the planner: they are left out, for a host with the pipeline to
/// add from [`realized`]'s launches.
fn tg_run(inp: &Value, _: Option<&Machine>) -> Result<Value, String> {
    let want: Vec<&str> = match inp.get("facts").and_then(Value::as_array) {
        Some(fs) => fs.iter().filter_map(Value::as_str).collect(),
        None => vec!["runs"],
    };
    let r = realized(inp)?;
    let names: Vec<String> = r
        .launches
        .iter()
        .map(|l| {
            l.name
                .rsplit_once('_')
                .map_or(l.name.as_str(), |(a, _)| a)
                .to_string()
        })
        .collect();
    let views: Vec<Value> = r
        .launches
        .iter()
        .filter_map(|l| l.info.get("view").cloned())
        .collect();
    let mut got = Map::new();
    for f in &want {
        let v = match *f {
            "runs" => json!(names),
            "kinds" => {
                let mut ks = names.clone();
                ks.sort();
                ks.dedup();
                json!(ks)
            }
            "views" => Value::Array(views.clone()),
            "viewset" => {
                let mut vs = views.clone();
                vs.sort_by(py_cmp);
                vs.dedup();
                Value::Array(vs)
            }
            "count" => {
                let ps = inp
                    .get("count")
                    .and_then(Value::as_array)
                    .ok_or("no `count` list")?;
                let mut c = Map::new();
                for p in ps.iter().filter_map(Value::as_str) {
                    c.insert(p.into(), json!(names.iter().filter(|n| *n == p).count()));
                }
                Value::Object(c)
            }
            "forms" => {
                let b = r.out.as_ref().ok_or("make ran no kernel")?;
                let mut keys: Vec<Value> = b
                    .valid
                    .iter()
                    .map(|k| match k.as_array() {
                        Some(a) if a.first() == Some(&json!("mx7")) => json!(a[..2]),
                        _ => k.clone(),
                    })
                    .collect();
                keys.sort_by_cached_key(crate::exec::repr);
                Value::Array(keys)
            }
            "host" => {
                let b = r.out.as_ref().ok_or("make ran no kernel")?;
                b.host.as_ref().map_or(Value::Null, |d| json!(d.bytes))
            }
            "grep" | "params" => continue,
            other => return Err(format!("no fact {other}")),
        };
        got.insert(f.to_string(), v);
    }
    Ok(Value::Object(got))
}

// ------------------------------------------------------------ Python's order
/// Python's ordering of plain values (numbers, strings, lists).
fn py_cmp(a: &Value, b: &Value) -> std::cmp::Ordering {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => x
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&y.as_f64().unwrap_or(0.0)),
        (Value::String(x), Value::String(y)) => x.cmp(y),
        (Value::Array(x), Value::Array(y)) => x
            .iter()
            .zip(y)
            .map(|(p, q)| py_cmp(p, q))
            .find(|o| o.is_ne())
            .unwrap_or_else(|| x.len().cmp(&y.len())),
        _ => py_dumps(a).cmp(&py_dumps(b)),
    }
}

/// `json.dumps(v, sort_keys=True)`.
fn py_dumps(v: &Value) -> String {
    match v {
        Value::Null => "null".into(),
        Value::Bool(b) => b.to_string(),
        Value::Number(n) if n.is_f64() => ktpu_graph::text::py_float(n.as_f64().unwrap()),
        Value::Number(n) => n.to_string(),
        Value::String(_) => v.to_string(),
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(py_dumps).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(o) => {
            let sorted: BTreeMap<&String, &Value> = o.iter().collect();
            let items: Vec<String> = sorted
                .into_iter()
                .map(|(k, x)| format!("{}: {}", Value::String(k.clone()), py_dumps(x)))
                .collect();
            format!("{{{}}}", items.join(", "))
        }
    }
}
