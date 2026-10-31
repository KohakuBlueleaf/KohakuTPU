//! Every compiler stage timed alone on each L2 of a corpus, a JSON line a kernel.
//! Smallest first, so a bounded run covers the most; a rerun goes on from its output,
//! and a kernel still running when the bound ended is reported as over it.
//!
//!     compile_speed --machine machines/multimesh_v9t6.json --out OUT.jsonl \
//!         --seconds 100 --jobs 8 DIR...

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use ktpu::{Backend, CardStack};
use ktpu_isa::Machine as Card;
use ktpu_l2::passes::{pass, pipeline_passes};
use ktpu_l2::target::Target;
use ktpu_l2::Search;
use serde_json::{json, Map, Value};

fn timed<T, E: ToString>(
    row: &mut Map<String, Value>,
    stage: &str,
    run: impl FnOnce() -> Result<T, E>,
) -> Result<T, String> {
    let t0 = Instant::now();
    let got = run();
    let ms = t0.elapsed().as_secs_f64() * 1e3;
    row.insert(stage.to_string(), json!((ms * 1e3).round() / 1e3));
    got.map_err(|e| format!("{stage}: {}", e.to_string()))
}

fn stages(source: &str, card: &Card, row: &mut Map<String, Value>) -> Result<(), String> {
    let s = CardStack::default();
    let l2 = timed(row, "parse", || s.parse_l2(source))?;
    let name = s.l2_name(&l2).map_err(|e| e.to_string())?;
    row.insert("name".to_string(), json!(name));

    let t = Target::of(card);
    let mut p = l2.clone().with_vregs(t.vregs, t.vcopies);
    let search = Search::default();
    for name in pipeline_passes() {
        p = timed(row, &format!("pass.{name}"), || pass(name, &p, &t, &search))?;
    }
    let l1 = timed(row, "lower", || s.lower(&p, &name, card))?;
    row.insert("l1_bytes".to_string(), json!(s.print_l1(&l1).len()));
    let l1 = timed(row, "schedule", || s.schedule(&l1, card))?;
    let caller = s.caller(&l2, &name).map_err(|e| e.to_string())?;
    let programs = timed(row, "programs", || {
        s.programs(&l1, &name, Some(&caller), card)
    })?;
    let packages = timed(row, "order", || s.order(&programs, card))?;
    row.insert("packages".to_string(), json!(packages.len()));
    timed(row, "check", || s.check(&programs, card))?;
    Ok(())
}

fn corpus(dirs: &[PathBuf]) -> Vec<(String, String)> {
    let mut seen = HashSet::new();
    let mut found = Vec::new();
    let mut todo = dirs.to_vec();
    while let Some(d) = todo.pop() {
        for entry in fs::read_dir(&d).unwrap_or_else(|e| panic!("{}: {e}", d.display())) {
            let path = entry.expect("a directory entry").path();
            if path.is_dir() {
                todo.push(path);
            } else if path.extension().is_some_and(|x| x == "l2" || x == "ktpu") {
                let source = fs::read_to_string(&path).expect("a kernel's text");
                if seen.insert(source.clone()) {
                    found.push((path.display().to_string(), source));
                }
            }
        }
    }
    found.sort_by_key(|(_, s)| s.len());
    found
}

fn main() {
    let mut args = std::env::args().skip(1);
    let (mut machine, mut out, mut seconds, mut jobs) = (None, None, 100u64, 8usize);
    let mut dirs = Vec::new();
    while let Some(a) = args.next() {
        match a.as_str() {
            "--machine" => machine = args.next(),
            "--out" => out = args.next(),
            "--seconds" => seconds = args.next().expect("seconds").parse().expect("seconds"),
            "--jobs" => jobs = args.next().expect("jobs").parse().expect("jobs"),
            dir => dirs.push(PathBuf::from(dir)),
        }
    }
    let card = Arc::new(Card::load(machine.expect("--machine")).expect("the machine"));
    let out = out.expect("--out");

    let done: HashSet<String> = fs::read_to_string(&out)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
        .filter_map(|v| v["path"].as_str().map(str::to_string))
        .collect();
    let todo: Vec<_> = corpus(&dirs)
        .into_iter()
        .filter(|(path, _)| !done.contains(path))
        .collect();
    println!("{} kernels to time, {} done", todo.len(), done.len());

    let file = OpenOptions::new().create(true).append(true).open(&out);
    let file = Arc::new(Mutex::new(file.expect("the output")));
    let todo = Arc::new(todo);
    let next = Arc::new(AtomicUsize::new(0));
    let end = Instant::now() + Duration::from_secs(seconds);
    let workers: Vec<_> = (0..jobs)
        .map(|_| {
            let (todo, next, file, card) = (todo.clone(), next.clone(), file.clone(), card.clone());
            thread::Builder::new()
                .stack_size(1 << 28)
                .spawn(move || loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= todo.len() || Instant::now() >= end {
                        return;
                    }
                    let (path, source) = &todo[i];
                    let mut row = Map::new();
                    row.insert("path".to_string(), json!(path));
                    row.insert("l2_bytes".to_string(), json!(source.len()));
                    let t0 = Instant::now();
                    let got = stages(source, &card, &mut row);
                    row.insert("total".to_string(), json!(t0.elapsed().as_secs_f64() * 1e3));
                    if let Err(e) = got {
                        row.insert("error".to_string(), json!(e));
                    }
                    let line = Value::Object(row).to_string();
                    writeln!(file.lock().expect("the output"), "{line}").expect("a row");
                })
                .expect("a worker")
        })
        .collect();

    while Instant::now() < end && !workers.iter().all(|w| w.is_finished()) {
        thread::sleep(Duration::from_millis(50));
    }
    let left = todo.len().saturating_sub(next.load(Ordering::Relaxed));
    let running = workers.iter().filter(|w| !w.is_finished()).count();
    println!("{left} not started, {running} still running at the bound");
    std::process::exit(0);
}
