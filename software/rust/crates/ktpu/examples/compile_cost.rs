//! The compile cost of the Rust stack on `golden/e2e` against the Python
//! stack's recorded times (each case's `seconds`).
//!
//! ```text
//! compile_cost stages --label L --cache off|DIR [--shard i/n] --out FILE
//! compile_cost many --label L --threads T|N --cache off|DIR [--shard i/n] --out FILE
//! compile_cost report --dir DIR
//! compile_cost passes
//! compile_cost snapshot --to DIR
//! compile_cost l1 --input GRAPH_OR_L2 --machine FILE --out PREFIX
//! ```
//!
//! Inputs: `KTPU_COST_GOLDEN` (the e2e cases) and `KTPU_MACHINES` (the
//! machine files), a `snapshot` so edits to the tree leave a run's inputs.
//!
//! `stages`: each case through `pipeline::compile` on one thread, every
//! backend call timed (a line a case; a case already in FILE under the same
//! label is skipped, so a killed run resumes). `many`: the shard's cases
//! through `compile_many` on a pool of T threads (N: every core), one call a
//! machine, the wall time and each case's output digest. `report`: the
//! markdown tables from every `*.jsonl` in DIR. `passes`: each L2 pass timed
//! alone. A label carries the build
//! profile (`dev` / `release`) in front of it.

use ktpu::pipeline::cache::Key;
use ktpu::{compile, compile_many, Backend, Cache, Compiled, Input, Refused, Stack, Stage};
use ktpu_l2::{Program, Search, Target};
use ktpu_machine::{Machine, Refusal};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Instant;

/// The timed backend calls, in pipeline order.
const CALLS: [&str; 12] = [
    "graph", "parse_l2", "print_l2", "passes", "lower", "parse_l1", "print_l1", "schedule",
    "build", "order", "verify", "other",
];
/// The report's stage columns: each the calls it sums (text parse / print
/// and the pipeline's own work, cache and JSON, are columns of their own).
const COLUMNS: [(&str, &[&str]); 8] = [
    ("graph", &["graph"]),
    ("passes", &["passes"]),
    ("lower", &["lower"]),
    ("schedule", &["schedule"]),
    ("build", &["build"]),
    ("order", &["order"]),
    ("verify", &["verify"]),
    ("text", &["parse_l2", "print_l2", "parse_l1", "print_l1"]),
];

thread_local! {
    static SPENT: RefCell<[f64; CALLS.len()]> = const { RefCell::new([0.0; CALLS.len()]) };
    static RAN: RefCell<[u32; CALLS.len()]> = const { RefCell::new([0; CALLS.len()]) };
}

/// `f`'s result, its time added to call `i` of this thread and counted (a
/// stage call runs only when its cache misses).
fn timed<T>(i: usize, f: impl FnOnce() -> T) -> T {
    let t0 = Instant::now();
    let out = f();
    let dt = t0.elapsed().as_secs_f64();
    SPENT.with(|s| s.borrow_mut()[i] += dt);
    RAN.with(|s| s.borrow_mut()[i] += 1);
    out
}

/// `Stack` with each call timed on its calling thread.
struct Timed;

impl Backend for Timed {
    type L2 = <Stack as Backend>::L2;
    type L1 = <Stack as Backend>::L1;
    type Programs = <Stack as Backend>::Programs;

    fn version(&self, stage: Stage) -> Option<String> {
        Stack.version(stage)
    }
    fn graph_to_l2(&self, g: &str, m: &Machine) -> Result<Self::L2, Refusal> {
        timed(0, || Stack.graph_to_l2(g, m))
    }
    fn parse_l2(&self, t: &str) -> Result<Self::L2, Refusal> {
        timed(1, || Stack.parse_l2(t))
    }
    fn print_l2(&self, p: &Self::L2) -> String {
        timed(2, || Stack.print_l2(p))
    }
    fn l2_name(&self, p: &Self::L2) -> Result<String, Refusal> {
        Stack.l2_name(p)
    }
    fn caller(&self, p: &Self::L2, n: &str) -> Result<Vec<String>, Refusal> {
        Stack.caller(p, n)
    }
    fn passes(&self, p: &Self::L2, n: &str, m: &Machine) -> Result<Self::L2, Refusal> {
        timed(3, || Stack.passes(p, n, m))
    }
    fn lower(&self, p: &Self::L2, n: &str, m: &Machine) -> Result<Self::L1, Refusal> {
        timed(4, || Stack.lower(p, n, m))
    }
    fn parse_l1(&self, t: &str) -> Result<Self::L1, Refusal> {
        timed(5, || Stack.parse_l1(t))
    }
    fn print_l1(&self, l: &Self::L1) -> String {
        timed(6, || Stack.print_l1(l))
    }
    fn schedule(&self, l: &Self::L1, m: &Machine) -> Result<Self::L1, Refusal> {
        timed(7, || Stack.schedule(l, m))
    }
    fn programs(
        &self,
        l: &Self::L1,
        n: &str,
        c: Option<&[String]>,
        m: &Machine,
    ) -> Result<Self::Programs, Refusal> {
        timed(8, || Stack.programs(l, n, c, m))
    }
    fn scratch(&self, p: &Self::Programs) -> Vec<ktpu::Scratch> {
        Stack.scratch(p)
    }
    fn order(
        &self,
        p: &Self::Programs,
        m: &Machine,
    ) -> Result<BTreeMap<u32, ktpu::PackageOut>, Refusal> {
        timed(9, || Stack.order(p, m))
    }
    fn check(&self, p: &Self::Programs, m: &Machine) -> Result<Vec<ktpu::Finding>, Refusal> {
        timed(10, || Stack.check(p, m))
    }
    fn refuses(&self, f: &ktpu::Finding) -> bool {
        Stack.refuses(f)
    }
}

/// One golden case the Rust stack compiles.
struct Case {
    name: String,
    machine: (String, u32),
    input: Input,
    kernel: Option<String>,
    python: Option<f64>,
}

/// `KTPU_COST_GOLDEN` (a snapshot), else `golden/e2e`.
fn golden_dir() -> PathBuf {
    match std::env::var_os("KTPU_COST_GOLDEN") {
        Some(dir) => PathBuf::from(dir),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../../golden/e2e"),
    }
}

/// Copies golden/e2e's cases and the machine files into `<to>/e2e` and
/// `<to>/machines`: the inputs a run reads with `KTPU_COST_GOLDEN` and
/// `KTPU_MACHINES` pointed there.
fn snapshot(args: &BTreeMap<String, String>) {
    let to = Path::new(&args["to"]);
    for (from, sub) in [
        (golden_dir(), "e2e"),
        (ktpu_machine::machines_dir(), "machines"),
    ] {
        let dir = to.join(sub);
        std::fs::create_dir_all(&dir).unwrap();
        let mut n = 0;
        for e in std::fs::read_dir(&from).unwrap().flatten() {
            let p = e.path();
            if p.extension().is_some_and(|x| x == "json") {
                std::fs::copy(&p, dir.join(p.file_name().unwrap())).unwrap();
                n += 1;
            }
        }
        println!("{n} files {} -> {}", from.display(), dir.display());
    }
}

/// Every case with an input (L2 text or the tracer's graph), sorted; the
/// others (Python refused at the trace, or too slow) are counted.
fn cases() -> (Vec<Case>, Vec<String>) {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(golden_dir())
        .expect("golden/e2e")
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    let (mut out, mut without) = (Vec::new(), Vec::new());
    for p in paths {
        let name = p.file_stem().unwrap().to_string_lossy().into_owned();
        let v: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        let input = match (v.get("graph").filter(|g| !g.is_null()), v["l2"].as_str()) {
            (Some(g), _) => Input::Graph(g.to_string()),
            (None, Some(t)) => Input::L2(t.to_string()),
            (None, None) => {
                without.push(name);
                continue;
            }
        };
        out.push(Case {
            name,
            machine: (
                v["machine"].as_str().unwrap().to_string(),
                v["nodes"].as_u64().unwrap() as u32,
            ),
            input,
            kernel: v["name"].as_str().map(str::to_string),
            python: v["seconds"].as_f64(),
        });
    }
    (out, without)
}

fn machine((file, nodes): &(String, u32)) -> Machine {
    let m = Machine::named(file).expect("machine file");
    match m.nodes() == *nodes {
        true => m,
        false => m.with_nodes(*nodes).expect("a card of that many nodes"),
    }
}

/// A digest of everything a compile returns (or its refusal).
fn digest(r: &Result<Compiled, Refused>) -> String {
    let text = match r {
        Ok(c) => json!({
            "name": c.name, "l2": c.l2, "l1": c.l1,
            "packages": c.packages, "verdict": c.verdict,
        })
        .to_string(),
        Err(e) => format!(
            "refused {}: {} {}",
            e.stage.name(),
            e.refusal.clause,
            e.refusal.message
        ),
    };
    Key::of("compile_cost", "", &[], text.as_bytes()).hex()
}

fn profile() -> &'static str {
    match cfg!(debug_assertions) {
        true => "dev",
        false => "release",
    }
}

fn cache_of(arg: &str) -> Cache {
    match arg {
        "off" => Cache::off(),
        dir => Cache::new(dir),
    }
}

fn shard<T>(all: Vec<T>, s: &str) -> Vec<T> {
    let (i, n) = s.split_once('/').expect("--shard i/n");
    let (i, n): (usize, usize) = (i.parse().unwrap(), n.parse().unwrap());
    all.into_iter().skip(i).step_by(n).collect()
}

fn append(out: &str, line: &Value) {
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(out)
        .expect("--out");
    writeln!(f, "{line}").unwrap();
}

/// The lines of a JSONL file (none when it does not exist).
fn lines(path: &Path) -> Vec<Value> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect()
}

fn stages(args: &BTreeMap<String, String>) {
    let label = format!("{}-{}", profile(), args["label"]);
    let out = &args["out"];
    let done: BTreeSet<String> = lines(Path::new(out))
        .iter()
        .filter(|l| l["label"] == label.as_str())
        .filter_map(|l| l["case"].as_str().map(str::to_string))
        .collect();
    let cache = cache_of(&args["cache"]);
    // `--like FILE`: only the cases an earlier run's JSONL holds.
    let like: Option<BTreeSet<String>> = args.get("like").map(|f| {
        lines(Path::new(f))
            .iter()
            .filter_map(|l| l["case"].as_str().map(str::to_string))
            .collect()
    });
    let mut machines: BTreeMap<(String, u32), Machine> = BTreeMap::new();
    for c in shard(cases().0, args.get("shard").map_or("0/1", String::as_str)) {
        if done.contains(&c.name) || like.as_ref().is_some_and(|l| !l.contains(&c.name)) {
            continue;
        }
        let m = machines
            .entry(c.machine.clone())
            .or_insert_with(|| machine(&c.machine));
        SPENT.with(|s| *s.borrow_mut() = [0.0; CALLS.len()]);
        RAN.with(|s| *s.borrow_mut() = [0; CALLS.len()]);
        let t0 = Instant::now();
        let r = compile(&Timed, &c.input, c.kernel.as_deref(), m, &cache);
        let total = t0.elapsed().as_secs_f64();
        let mut spent = SPENT.with(|s| *s.borrow());
        spent[CALLS.len() - 1] = total - spent.iter().sum::<f64>();
        let calls: BTreeMap<&str, f64> = CALLS.iter().copied().zip(spent).collect();
        let ran: BTreeMap<&str, u32> = CALLS
            .iter()
            .copied()
            .zip(RAN.with(|s| *s.borrow()))
            .collect();
        append(
            out,
            &json!({
                "mode": "stages", "label": label, "case": c.name, "total": total,
                "calls": calls, "ran": ran, "digest": digest(&r),
                "refused": r.as_ref().err().map(|e| e.stage.name()),
            }),
        );
    }
}

fn many(args: &BTreeMap<String, String>) {
    let threads = match args["threads"].as_str() {
        "N" => std::thread::available_parallelism().map_or(1, |n| n.get()),
        t => t.parse().expect("--threads T|N"),
    };
    let label = format!("{}-{}", profile(), args["label"]);
    let shard_arg = args.get("shard").map_or("0/1", String::as_str);
    let cache = cache_of(&args["cache"]);
    let pool = rayon::ThreadPoolBuilder::new()
        .num_threads(threads)
        .build()
        .unwrap();
    // compile_many takes one machine: one call per machine.
    let mut groups: BTreeMap<(String, u32), Vec<Case>> = BTreeMap::new();
    for c in shard(cases().0, shard_arg) {
        groups.entry(c.machine.clone()).or_default().push(c);
    }
    let mut digests = BTreeMap::new();
    let t0 = Instant::now();
    for (mk, group) in &groups {
        let m = machine(mk);
        let inputs: Vec<(Input, Option<String>)> = group
            .iter()
            .map(|c| (c.input.clone(), c.kernel.clone()))
            .collect();
        let rs = pool.install(|| compile_many(&Stack, &inputs, &m, &cache));
        for (c, r) in group.iter().zip(&rs) {
            digests.insert(c.name.clone(), digest(r));
        }
    }
    let wall = t0.elapsed().as_secs_f64();
    append(
        &args["out"],
        &json!({
            "mode": "many", "label": label, "threads": threads, "shard": shard_arg,
            "wall": wall, "digests": digests,
        }),
    );
}

fn secs(s: f64) -> String {
    match s {
        s if s >= 10.0 => format!("{s:.1}"),
        s if s >= 0.1 => format!("{s:.2}"),
        s => format!("{s:.3}"),
    }
}

fn column(l: &Value, calls: &[&str]) -> f64 {
    calls
        .iter()
        .map(|c| l["calls"][*c].as_f64().unwrap_or(0.0))
        .sum()
}

fn report(args: &BTreeMap<String, String>) {
    let mut all = Vec::new();
    let mut files: Vec<PathBuf> = std::fs::read_dir(&args["dir"])
        .expect("--dir")
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
        .collect();
    files.sort();
    for f in files {
        all.extend(lines(&f));
    }
    let (cases, without) = cases();
    let python: BTreeMap<&str, Option<f64>> =
        cases.iter().map(|c| (c.name.as_str(), c.python)).collect();
    // stages: label -> case -> line (the last line wins)
    let mut by_label: BTreeMap<String, BTreeMap<String, &Value>> = BTreeMap::new();
    for l in all.iter().filter(|l| l["mode"] == "stages") {
        by_label
            .entry(l["label"].as_str().unwrap().to_string())
            .or_default()
            .insert(l["case"].as_str().unwrap().to_string(), l);
    }
    println!(
        "{} cases compile ({} without an input: {}).\n",
        cases.len(),
        without.len(),
        without.join(", ")
    );
    // Per-stage sums.
    let py_cases: Vec<&str> = cases
        .iter()
        .filter(|c| c.python.is_some())
        .map(|c| c.name.as_str())
        .collect();
    let py_total: f64 = py_cases.iter().map(|c| python[c].unwrap()).sum();
    print!("| run | cases |");
    for (c, _) in COLUMNS {
        print!(" {c} |");
    }
    println!(" pipeline | total | Python | Python / Rust |");
    println!("|{}", "---|".repeat(COLUMNS.len() + 6));
    for (label, rows) in &by_label {
        let n = rows.len();
        print!("| {label} | {n} |");
        let mut sum = 0.0;
        for (_, calls) in COLUMNS {
            let s: f64 = rows.values().map(|l| column(l, calls)).sum();
            sum += s;
            print!(" {} |", secs(s));
        }
        let total: f64 = rows.values().map(|l| l["total"].as_f64().unwrap()).sum();
        let py: f64 = rows
            .keys()
            .filter_map(|c| python.get(c.as_str()).copied().flatten())
            .sum();
        println!(
            " {} | {} | {} | {:.0}x |",
            secs(total - sum),
            secs(total),
            secs(py),
            py / total
        );
    }
    println!(
        "\nPython (golden/e2e `seconds`, {} cases): {} s.\n",
        py_cases.len(),
        secs(py_total)
    );
    // Backend calls run: a stage call runs only on its cache's miss.
    print!("| run (backend calls run) |");
    for c in &CALLS[..CALLS.len() - 1] {
        print!(" {c} |");
    }
    println!("\n|{}", "---|".repeat(CALLS.len()));
    for (label, rows) in &by_label {
        print!("| {label} |");
        for c in &CALLS[..CALLS.len() - 1] {
            let n: u64 = rows
                .values()
                .map(|l| l["ran"][*c].as_u64().unwrap_or(0))
                .sum();
            print!(" {n} |");
        }
        println!();
    }
    println!();
    // compile_many: (label, threads) -> wall summed over shards, cases.
    let mut runs: BTreeMap<(String, u64), (f64, usize, Vec<String>)> = BTreeMap::new();
    let mut reference: BTreeMap<String, String> = BTreeMap::new();
    for rows in by_label.values() {
        for (c, l) in rows {
            reference
                .entry(c.clone())
                .or_insert_with(|| l["digest"].as_str().unwrap().to_string());
        }
    }
    let mut differ: BTreeSet<String> = BTreeSet::new();
    for rows in by_label.values() {
        for (c, l) in rows {
            if reference[c] != l["digest"].as_str().unwrap() {
                differ.insert(format!("{c} ({})", l["label"]));
            }
        }
    }
    for l in all.iter().filter(|l| l["mode"] == "many") {
        let key = (
            l["label"].as_str().unwrap().to_string(),
            l["threads"].as_u64().unwrap(),
        );
        let e = runs.entry(key).or_default();
        e.0 += l["wall"].as_f64().unwrap();
        e.2.push(l["shard"].as_str().unwrap().to_string());
        for (c, d) in l["digests"].as_object().unwrap() {
            e.1 += 1;
            let want = reference
                .entry(c.clone())
                .or_insert_with(|| d.as_str().unwrap().to_string());
            if want != d.as_str().unwrap() {
                differ.insert(format!("{c} ({} x{})", l["label"], l["threads"]));
            }
        }
    }
    println!("| compile_many run | threads | cases | wall s (summed over shards) | shards |");
    println!("|---|---|---|---|---|");
    for ((label, t), (wall, n, shards)) in &runs {
        println!(
            "| {label} | {t} | {n} | {} | {} |",
            secs(*wall),
            shards.join(" ")
        );
    }
    println!(
        "\nOutputs compared: {} cases, {} runs; differing: {}.\n",
        reference.len(),
        by_label.len() + runs.len(),
        match differ.is_empty() {
            true => "none".to_string(),
            false => differ.into_iter().collect::<Vec<_>>().join(", "),
        }
    );
    // The slowest 10 by the first release label (else the first label).
    let Some((label, rows)) = by_label
        .iter()
        .find(|(l, _)| l.starts_with("release"))
        .or_else(|| by_label.iter().next())
    else {
        return;
    };
    let mut slow: Vec<(&String, &&Value)> = rows.iter().collect();
    slow.sort_by(|a, b| {
        b.1["total"]
            .as_f64()
            .unwrap()
            .total_cmp(&a.1["total"].as_f64().unwrap())
    });
    let others: Vec<&String> = by_label.keys().filter(|l| *l != label).collect();
    print!("| case ({label}) | Rust s | dominant stage | share |");
    for o in &others {
        print!(" {o} s |");
    }
    println!(" Python s | Python / Rust |");
    println!("|{}", "---|".repeat(6 + others.len()));
    for (c, l) in slow.iter().take(10) {
        let total = l["total"].as_f64().unwrap();
        let (stage, s) = COLUMNS
            .iter()
            .map(|(n, calls)| (*n, column(l, calls)))
            .max_by(|a, b| a.1.total_cmp(&b.1))
            .unwrap();
        print!(
            "| {c} | {} | {stage} | {:.0}% |",
            secs(total),
            100.0 * s / total
        );
        for o in &others {
            let t = by_label[*o].get(*c).and_then(|l| l["total"].as_f64());
            print!(" {} |", t.map_or("-".into(), secs));
        }
        let py = python.get(c.as_str()).copied().flatten();
        println!(
            " {} | {} |",
            py.map_or("-".into(), secs),
            py.map_or("-".into(), |p| format!("{:.0}x", p / total))
        );
    }
}

/// Each L2 pass timed alone over every case (one thread): the sum a pass and
/// its three slowest cases.
/// A pass's total seconds and its `(seconds, case)` per case.
type Spent<'a> = BTreeMap<&'a str, (f64, Vec<(f64, String)>)>;

fn passes() {
    let mut spent: Spent = BTreeMap::new();
    for c in cases().0 {
        let m = machine(&c.machine);
        let Ok(p) = (match &c.input {
            Input::Graph(g) => Stack.graph_to_l2(g, &m),
            Input::L2(t) => Stack.parse_l2(t),
            // Hand-written L1 runs no L2 pass.
            Input::L1(_) => continue,
        }) else {
            continue;
        };
        let name = c.kernel.clone().unwrap_or_else(|| p.name.clone());
        let Ok(p) = (match p.name == name {
            true => Ok(p),
            false => Program::parse(p.text(), "<l2>", Some(&name)),
        }) else {
            continue;
        };
        let t = Target::of(&m);
        let mut p = p.with_vregs(t.vregs, t.vcopies);
        for pass in ktpu_l2::PASSES {
            let t0 = Instant::now();
            let r = ktpu_l2::passes::pass(pass, &p, &t, &Search::default());
            let dt = t0.elapsed().as_secs_f64();
            let e = spent.entry(pass).or_default();
            e.0 += dt;
            e.1.push((dt, c.name.clone()));
            match r {
                Ok(next) => p = next,
                Err(_) => break,
            }
        }
    }
    println!("| pass | total s | slowest cases (s) |\n|---|---|---|");
    for pass in ktpu_l2::PASSES {
        let Some((total, mut each)) = spent.remove(pass) else {
            continue;
        };
        each.sort_by(|a, b| b.0.total_cmp(&a.0));
        let top: Vec<String> = each
            .iter()
            .take(3)
            .map(|(s, n)| format!("{n} {}", secs(*s)))
            .collect();
        println!("| {pass} | {} | {} |", secs(total), top.join(", "));
    }
}

/// The L2 (`<out>.l2`) and scheduled L1 (`<out>.l1`) of `--input` (graph
/// JSON or L2 text) for the machine file `--machine`.
fn l1(args: &BTreeMap<String, String>) {
    let m = Machine::load(&args["machine"]).expect("--machine");
    let input = Input::detect(&std::fs::read_to_string(&args["input"]).expect("--input"));
    let p = match &input {
        Input::Graph(g) => Stack.graph_to_l2(g, &m),
        Input::L2(t) => Stack.parse_l2(t),
        Input::L1(_) => panic!("--input: graph JSON or L2 text"),
    }
    .expect("L2");
    let name = p.name.clone();
    let l1 = Stack
        .lower(&Stack.passes(&p, &name, &m).expect("passes"), &name, &m)
        .and_then(|l| Stack.schedule(&l, &m))
        .expect("L1");
    let out = &args["out"];
    std::fs::write(format!("{out}.l2"), Stack.print_l2(&p)).unwrap();
    std::fs::write(format!("{out}.l1"), Stack.print_l1(&l1)).unwrap();
    println!("{name}: {out}.l2, {out}.l1");
}

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let mode = argv.first().expect("stages | many | report").clone();
    let args: BTreeMap<String, String> = argv[1..]
        .chunks(2)
        .map(|kv| (kv[0].trim_start_matches("--").to_string(), kv[1].clone()))
        .collect();
    match mode.as_str() {
        "stages" => stages(&args),
        "many" => many(&args),
        "report" => report(&args),
        "passes" => passes(),
        "snapshot" => snapshot(&args),
        "l1" => l1(&args),
        m => panic!("unknown mode {m}: stages | many | report | passes | snapshot"),
    }
}
