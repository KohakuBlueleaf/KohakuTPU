//! The pipeline on a test backend: stage order, caching per stage, refusals
//! by stage, and `compile_many`'s output independent of the thread count.

use ktpu::{compile, compile_many, Backend, Cache, Finding, Input, PackageOut, Stage};
use ktpu_machine::{Machine, Refusal};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, AtomicUsize, Ordering};
use std::sync::Mutex;

/// L2 and L1 are lines; each stage appends a line naming itself, so the
/// output shows every stage ran, in order. Graph JSON is `{"l2": TEXT}`.
#[derive(Default)]
struct Toy {
    calls: Mutex<BTreeMap<&'static str, usize>>,
    passes_version: AtomicU32,
    /// Passes output independent of the input past its first line.
    passes_keep_first: bool,
}

impl Toy {
    fn hit(&self, what: &'static str) {
        *self.calls.lock().unwrap().entry(what).or_default() += 1;
    }
    fn calls(&self, what: &str) -> usize {
        self.calls.lock().unwrap().get(what).copied().unwrap_or(0)
    }
}

fn refuse_if(text: &str, word: &str, clause: &str) -> Result<(), Refusal> {
    match text.contains(word) {
        true => Err(Refusal::new(clause, format!("{word} refused"))),
        false => Ok(()),
    }
}

impl Backend for Toy {
    type L2 = Vec<String>;
    type L1 = Vec<String>;
    type Programs = Vec<String>;

    fn version(&self, stage: Stage) -> Option<String> {
        Some(match stage {
            Stage::Passes => self.passes_version.load(Ordering::SeqCst).to_string(),
            _ => String::new(),
        })
    }
    fn graph_to_l2(&self, graph_json: &str, _: &Machine) -> Result<Vec<String>, Refusal> {
        self.hit("graph");
        let v: serde_json::Value = serde_json::from_str(graph_json)
            .map_err(|e| Refusal::new("DSL.GRAPH.000", e.to_string()))?;
        self.parse_l2(v["l2"].as_str().unwrap_or_default())
    }
    fn parse_l2(&self, text: &str) -> Result<Vec<String>, Refusal> {
        refuse_if(text, "unparsable", "L2.TEXT.001")?;
        Ok(text.lines().map(str::to_string).collect())
    }
    fn print_l2(&self, l2: &Vec<String>) -> String {
        l2.join("\n")
    }
    fn l2_name(&self, l2: &Vec<String>) -> Result<String, Refusal> {
        l2.iter()
            .find_map(|l| l.strip_prefix("fn "))
            .map(str::to_string)
            .ok_or_else(|| Refusal::new("L2.FN.001", "no fn"))
    }
    fn caller(&self, _: &Vec<String>, _: &str) -> Result<Vec<String>, Refusal> {
        Ok(vec!["x".into()])
    }
    fn passes(&self, l2: &Vec<String>, name: &str, _: &Machine) -> Result<Vec<String>, Refusal> {
        self.hit("passes");
        refuse_if(&l2.join("\n"), "nopass", "L2.PASS.001")?;
        let mut out = match self.passes_keep_first {
            true => l2[..1].to_vec(),
            false => l2.clone(),
        };
        out.push(format!("passes {name}"));
        Ok(out)
    }
    fn lower(&self, l2: &Vec<String>, name: &str, m: &Machine) -> Result<Vec<String>, Refusal> {
        self.hit("lower");
        let mut out = l2.clone();
        out.push(format!("lower {name} {}", m.name));
        Ok(out)
    }
    fn parse_l1(&self, text: &str) -> Result<Vec<String>, Refusal> {
        Ok(text.lines().map(str::to_string).collect())
    }
    fn print_l1(&self, l1: &Vec<String>) -> String {
        l1.join("\n")
    }
    fn schedule(&self, l1: &Vec<String>, _: &Machine) -> Result<Vec<String>, Refusal> {
        self.hit("schedule");
        let mut out = l1.clone();
        out.push("schedule".into());
        Ok(out)
    }
    fn programs(
        &self,
        l1: &Vec<String>,
        _: &str,
        _: Option<&[String]>,
        _: &Machine,
    ) -> Result<Vec<String>, Refusal> {
        self.hit("programs");
        Ok(l1.clone())
    }
    fn scratch(&self, _: &Vec<String>) -> Vec<ktpu::Scratch> {
        Vec::new()
    }
    fn order(&self, p: &Vec<String>, _: &Machine) -> Result<BTreeMap<u32, PackageOut>, Refusal> {
        self.hit("order");
        let pkg = PackageOut {
            bytes: p.join("\n").into_bytes(),
            params: vec!["x".into()],
            images: serde_json::Value::Null,
        };
        Ok(BTreeMap::from([(0, pkg)]))
    }
    fn check(&self, p: &Vec<String>, _: &Machine) -> Result<Vec<Finding>, Refusal> {
        self.hit("check");
        Ok(p.iter()
            .filter(|l| l.contains("race"))
            .map(|l| Finding {
                kind: "race".into(),
                clause: "VER.RACE.001".into(),
                at: String::new(),
                text: l.clone(),
            })
            .collect())
    }
    fn refuses(&self, f: &Finding) -> bool {
        f.kind == "race"
    }
}

fn machine(name: &str) -> Machine {
    Machine::named(name).expect("machine file")
}

/// A fresh cache directory under the target dir.
fn fresh_cache() -> (Cache, PathBuf) {
    static N: AtomicUsize = AtomicUsize::new(0);
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!(
        "rcache-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    (Cache::new(&dir), dir)
}

fn l2(text: &str) -> Input {
    Input::L2(text.to_string())
}

#[test]
fn every_stage_runs_in_order() {
    let b = Toy::default();
    let got = compile(
        &b,
        &l2("fn k\nbody"),
        None,
        &machine("multimesh_v9t6"),
        &Cache::off(),
    )
    .unwrap();
    assert_eq!(got.name, "k");
    assert_eq!(got.l2, "fn k\nbody");
    assert_eq!(
        got.l1,
        "fn k\nbody\npasses k\nlower k multimesh_v9t6\nschedule"
    );
    assert_eq!(got.packages[&0].bytes, got.l1.as_bytes());
    assert!(!got.verdict.refused);
    assert_eq!(b.calls("programs"), 1, "order and verify share one build");
}

#[test]
fn graph_input_is_traced_to_l2() {
    let b = Toy::default();
    let input = Input::detect(r#"{"l2": "fn g\nbody"}"#);
    assert!(matches!(input, Input::Graph(_)));
    let got = compile(&b, &input, None, &machine("multimesh_v9t6"), &Cache::off()).unwrap();
    assert_eq!((got.name.as_str(), got.l2.as_str()), ("g", "fn g\nbody"));
}

#[test]
fn a_refusal_names_its_stage_and_clause() {
    let b = Toy::default();
    let m = machine("multimesh_v9t6");
    let e = compile(&b, &l2("fn k\nnopass"), None, &m, &Cache::off()).unwrap_err();
    assert_eq!(
        (e.stage, e.refusal.clause.as_str()),
        (Stage::Passes, "L2.PASS.001")
    );
    let e = compile(&b, &l2("fn k\nunparsable"), None, &m, &Cache::off()).unwrap_err();
    assert_eq!(
        (e.stage, e.refusal.clause.as_str()),
        (Stage::Passes, "L2.TEXT.001")
    );
}

#[test]
fn a_refused_finding_refuses_the_run() {
    let b = Toy::default();
    let got = compile(
        &b,
        &l2("fn k\nrace"),
        None,
        &machine("multimesh_v9t6"),
        &Cache::off(),
    )
    .unwrap();
    assert!(got.verdict.refused);
    assert_eq!(got.verdict.findings[0].clause, "VER.RACE.001");
}

#[test]
fn a_second_compile_runs_no_stage() {
    let (cache, _dir) = fresh_cache();
    let b = Toy::default();
    let m = machine("multimesh_v9t6");
    let input = Input::detect(r#"{"l2": "fn g\nbody"}"#);
    let first = compile(&b, &input, None, &m, &cache).unwrap();
    let calls = b.calls.lock().unwrap().clone();
    let again = compile(&b, &input, None, &m, &cache).unwrap();
    assert_eq!(first, again);
    assert_eq!(*b.calls.lock().unwrap(), calls, "every stage hit its cache");
}

#[test]
fn a_refusal_is_cached_too() {
    let (cache, _dir) = fresh_cache();
    let b = Toy::default();
    let m = machine("multimesh_v9t6");
    let first = compile(&b, &l2("fn k\nnopass"), None, &m, &cache).unwrap_err();
    let calls = b.calls("passes");
    let again = compile(&b, &l2("fn k\nnopass"), None, &m, &cache).unwrap_err();
    assert_eq!(first, again);
    assert_eq!(b.calls("passes"), calls, "the refusal came from the cache");
}

/// Every entry file under `dir`, and the temporaries beside them.
#[cfg(windows)]
fn entries(dir: &std::path::Path) -> (Vec<PathBuf>, usize) {
    let (mut out, mut tmp) = (Vec::new(), 0);
    let mut todo = vec![dir.to_path_buf()];
    while let Some(d) = todo.pop() {
        for e in std::fs::read_dir(&d).into_iter().flatten().flatten() {
            let p = e.path();
            match (p.is_dir(), p.extension().and_then(|x| x.to_str())) {
                (true, _) => todo.push(p),
                (false, Some("bin")) => out.push(p),
                (false, Some("tmp")) => tmp += 1,
                _ => {}
            }
        }
    }
    (out, tmp)
}

/// A compile through a cache whose every entry another process holds open
/// with no sharing (Windows): each read is a miss and each write is refused,
/// and the compile is the same, the held entries unchanged, no temporary
/// left.
#[cfg(windows)]
fn compiles_beside_held_entries() -> Result<(), String> {
    use std::os::windows::fs::OpenOptionsExt;
    let (cache, dir) = fresh_cache();
    let b = Toy::default();
    let m = machine("multimesh_v9t6");
    let first = compile(&b, &l2("fn k\nbody"), None, &m, &cache).unwrap();
    let (held, _) = entries(&dir);
    let before: Vec<Vec<u8>> = held.iter().map(|p| std::fs::read(p).unwrap()).collect();
    let _open: Vec<std::fs::File> = held
        .iter()
        .map(|p| {
            std::fs::OpenOptions::new()
                .read(true)
                .share_mode(0)
                .open(p)
                .unwrap()
        })
        .collect();
    let calls = b.calls("passes");
    let again = compile(&b, &l2("fn k\nbody"), None, &m, &cache).map_err(|e| e.to_string())?;
    drop(_open);
    let after: Vec<Vec<u8>> = held.iter().map(|p| std::fs::read(p).unwrap()).collect();
    let (_, tmp) = entries(&dir);
    match (
        again == first,
        b.calls("passes") > calls,
        after == before,
        tmp,
    ) {
        (true, true, true, 0) => Ok(()),
        got => Err(format!("same, missed, kept, temporaries: {got:?}")),
    }
}

#[cfg(windows)]
#[test]
fn an_entry_held_open_is_a_miss_and_is_kept() {
    compiles_beside_held_entries().unwrap();
}

#[cfg(all(windows, feature = "mutants"))]
#[test]
fn a_cache_error_refusing_the_compile_is_caught() {
    let got = ktpu::mutant::with(
        ktpu::pipeline::CACHE_ERRORS_REFUSE,
        compiles_beside_held_entries,
    )
    .unwrap();
    assert!(got.is_err(), "the mutant compiled beside held entries");
}

#[test]
fn a_new_pass_version_keeps_the_other_stages_entries() {
    let (cache, _dir) = fresh_cache();
    let b = Toy {
        passes_keep_first: true,
        ..Toy::default()
    };
    let m = machine("multimesh_v9t6");
    let input = Input::detect(r#"{"l2": "fn g\nbody"}"#);
    let first = compile(&b, &input, None, &m, &cache).unwrap();
    b.passes_version.store(1, Ordering::SeqCst);
    let again = compile(&b, &input, None, &m, &cache).unwrap();
    assert_eq!(first, again);
    assert_eq!(b.calls("graph"), 1, "the graph stage kept its entry");
    assert_eq!(b.calls("passes"), 2, "the passes ran again");
    assert_eq!(
        b.calls("lower"),
        1,
        "the same passes output keeps lower's entry"
    );
    // Another body with the same passes output reuses every later stage.
    let other = Input::detect(r#"{"l2": "fn g\nother body"}"#);
    let third = compile(&b, &other, None, &m, &cache).unwrap();
    assert_eq!(third.l1, first.l1);
    assert_eq!(b.calls("lower"), 1);
    assert_eq!(b.calls("schedule"), 1);
    assert_eq!(b.calls("order"), 1);
}

#[test]
fn another_machine_misses() {
    let (cache, _dir) = fresh_cache();
    let b = Toy::default();
    let input = l2("fn k\nbody");
    compile(&b, &input, None, &machine("multimesh_v9t6"), &cache).unwrap();
    let got = compile(&b, &input, None, &machine("multimesh_v9"), &cache).unwrap();
    assert_eq!(b.calls("passes"), 2);
    assert!(got.l1.contains("lower k multimesh_v9\n"));
}

#[test]
fn compile_many_is_ordered_and_independent_of_threads() {
    let m = machine("multimesh_v9t6");
    let inputs: Vec<(Input, Option<String>)> = (0..64)
        .map(|i| {
            let body = if i % 7 == 3 { "nopass" } else { "body" };
            (l2(&format!("fn k{i}\n{body} {i}")), None)
        })
        .collect();
    let run = |threads: usize| {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .build()
            .unwrap();
        pool.install(|| compile_many(&Toy::default(), &inputs, &m, &Cache::off()))
    };
    let one = run(1);
    let many = run(8);
    assert_eq!(one, many);
    for (i, got) in one.iter().enumerate() {
        match got {
            Ok(c) => assert_eq!(c.name, format!("k{i}")),
            Err(e) => {
                assert_eq!(i % 7, 3);
                assert_eq!(e.stage, Stage::Passes);
            }
        }
    }
}
