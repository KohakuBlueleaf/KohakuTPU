//! Record planner goldens (`golden/plan/*.json`) as ktpu-plan plans and runs
//! them now (`run::execute_with` at the knobs of the golden's vector,
//! `run::Runner::rows_run`, `tensor::Session::replay`), each into
//! `OUT_DIR/<file>`, never in place; with `--mutant NAME` (repeated), under
//! those ktpu-kernels mutants.
//!
//! A golden is recorded when its text or its recording holds a `--select`
//! text (default: `matmul.mm_kernel`, `matmul.plan`); a kp entry's steps are
//! replaced when its recording holds one. A ported vector with no golden,
//! whose knobs the planner takes, is always recorded, from the golden of the
//! vector with its input less `knobs`, under its own name. With `--only
//! NAME`, exactly the goldens named are recorded, whatever they hold.
//! `scripts/py/measure/rows/plan_regold.py` compares the recordings with the
//! goldens and with an independently predicted change.
//!
//!     cargo run -p ktpu-plan --example planner_record -- OUT_DIR
//!         [--mutant NAME] [--select TEXT]... [--only NAME]...

#[path = "../tests/common/mod.rs"]
mod common;

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use common::vectors;
use ktpu_plan::{lower, run, spec, tensor::Session, Buffer, Case};
use serde_json::Value;

/// `case` recorded now at `input`'s knobs: each run's steps and end state (a
/// later run's start state that was an earlier run's recorded end takes its
/// new end), each direct rows call's steps and refusal, each kp step list
/// holding a `select` text.
fn record(mut case: Case, input: &Value, select: &[String]) -> Case {
    let m = case.machine();
    let knobs = spec::knobs(input).unwrap();
    let mut prev_old_end: BTreeMap<String, Buffer> = BTreeMap::new();
    let mut prev_new_end: BTreeMap<String, Buffer> = BTreeMap::new();
    for r in &mut case.runs {
        for (bname, bstate) in &mut r.state.buffers {
            if let (Some(old_rec), Some(new_rec)) =
                (prev_old_end.get(bname), prev_new_end.get(bname))
            {
                if bstate == old_rec {
                    *bstate = new_rec.clone();
                }
            }
        }
        let l = lower::plan(&case.plans[r.plan].uops).unwrap();
        let x = match run::execute_with(l, &r.state, &m, knobs) {
            Ok(x) => x,
            Err((_, x)) => x,
        };
        let bases: Vec<String> = r.end.keys().cloned().collect();
        let new_end = x.records(&bases);
        prev_old_end = r.end.clone();
        prev_new_end = new_end.clone();
        r.steps = x.steps;
        r.end = new_end;
    }
    for c in &mut case.rows {
        let (r, error) = common::rows_call(c, &m, knobs);
        c.steps = r.x.steps;
        c.error = error;
    }
    let mut s = Session::new(&m);
    s.r.knobs = knobs;
    let mut names: HashMap<String, usize> = HashMap::new();
    for k in &mut case.kp {
        let (steps, out) = match s.replay(&k.op, &k.args, &names) {
            Ok(r) => (r.steps, r.out),
            Err((_, steps)) => (steps, None),
        };
        if let (Some(name), Some(t)) = (&k.out, out) {
            names.entry(name.clone()).or_insert(t);
        }
        if holds(&serde_json::to_string(&steps).unwrap(), select) {
            k.steps = steps;
        }
    }
    case
}

/// `f` with every one of `mutants` on, each switched in the crate that
/// carries it (ktpu-plan's spec mutants, one at a time; ktpu-kernels').
fn under<T>(mutants: &[&'static str], f: impl FnOnce() -> T) -> T {
    match mutants.split_first() {
        Some((m, rest)) if ktpu_plan::mutant::NAMES.contains(m) => {
            ktpu_plan::mutant::with(m, || under(rest, f))
        }
        Some((m, rest)) => ktpu_kernels::mutants::with(m, || under(rest, f)),
        None => f(),
    }
}

fn holds(text: &str, select: &[String]) -> bool {
    select.iter().any(|s| text.contains(s.as_str()))
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let out = PathBuf::from(&args[0]);
    let flag = |name: &str| -> Vec<String> {
        args.windows(2)
            .filter(|p| p[0] == name)
            .map(|p| p[1].clone())
            .collect()
    };
    let mutants: Vec<&'static str> = flag("--mutant")
        .iter()
        .map(|name| {
            *ktpu_kernels::mutants::NAMES
                .iter()
                .chain(ktpu_kernels::mutants::LOCAL)
                .chain(ktpu_plan::mutant::NAMES)
                .find(|n| *n == name)
                .expect("a mutant")
        })
        .collect();
    let mut select = flag("--select");
    if select.is_empty() {
        select = vec!["matmul.mm_kernel".into(), "matmul.plan".into()];
    }
    std::fs::create_dir_all(&out).unwrap();
    let software = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let plan_dir = software.join("rust/golden/plan");
    let vectors = vectors();
    let golden = |name: &str| plan_dir.join(format!("{name}.json"));
    // (name, case text, input, recorded whatever it holds)
    let mut jobs: Vec<(String, String, Value, bool)> = Vec::new();
    for v in vectors {
        if golden(&v.name).exists() || spec::knobs(&v.input).is_err() {
            continue;
        }
        let mut bare = v.input.clone();
        bare.as_object_mut().unwrap().remove("knobs");
        let Some(twin) = vectors
            .iter()
            .find(|t| t.input == bare && golden(&t.name).exists())
        else {
            continue;
        };
        let mut case: Value =
            serde_json::from_str(&std::fs::read_to_string(golden(&twin.name)).unwrap()).unwrap();
        case["case"] = Value::String(v.name.clone());
        case["from"] = Value::String(v.from.clone());
        jobs.push((v.name.clone(), case.to_string(), v.input.clone(), true));
    }
    let mut files: Vec<PathBuf> = std::fs::read_dir(&plan_dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    files.sort();
    for f in files {
        let name = f.file_stem().unwrap().to_string_lossy().to_string();
        let input = vectors
            .iter()
            .find(|v| v.name == name)
            .map_or(Value::Null, |v| v.input.clone());
        jobs.push((name, std::fs::read_to_string(&f).unwrap(), input, false));
    }
    let only = flag("--only");
    if !only.is_empty() {
        jobs.retain(|j| only.contains(&j.0));
        jobs.iter_mut().for_each(|j| j.3 = true);
    }
    let mut n = 0;
    for (name, raw, input, new) in jobs {
        let case: Case = serde_json::from_str(&raw).unwrap();
        let case = under(&mutants, || record(case, &input, &select));
        let text = format!("{}\n", serde_json::to_string_pretty(&case).unwrap());
        if !new && !holds(&raw, &select) && !holds(&text, &select) {
            continue;
        }
        std::fs::write(out.join(format!("{name}.json")), text).unwrap();
        n += 1;
    }
    println!("recorded {n} plan goldens into {}", out.display());
}
