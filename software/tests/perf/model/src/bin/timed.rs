//! Each `NAME.l1` of a trace in time, a JSON line a kernel.
//!
//!     timed --machine machines/multimesh_v9t6.json --params tests/perf/params.json \
//!         --out OUT.jsonl [--card] DIR...
//!
//! `--card` reads each parameter's card value: real DDR4 and a step's cold cache
//! in place of Verilator's memory.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use ktpu_isa::Machine as Card;
use ktpu_l1::text::read;
use ktpu_perf::model::{kernel, Params};
use rayon::prelude::*;
use serde_json::{json, Value};

/// One row a kernel: a trace's `NAME.l1`, or each `fn` of a hand `.ktpu`.
fn rows(path: &Path, card: &Card, p: &Params) -> Vec<Value> {
    let stem = path.file_stem().unwrap().to_string_lossy().to_string();
    let module = fs::read_to_string(path)
        .map_err(|e| e.to_string())
        .and_then(|text| read(&text, "trace").map_err(|e| e.to_string()));
    let module = match module {
        Ok(m) => m,
        Err(e) => return vec![json!({"name": stem, "error": e})],
    };
    let names = match path.extension().is_some_and(|x| x == "ktpu") {
        true => module.names(),
        false => vec![stem],
    };
    names
        .into_iter()
        .map(|name| {
            kernel(&module, &name, card, p).unwrap_or_else(|e| json!({"name": name, "error": e}))
        })
        .collect()
}

fn main() {
    let mut args = std::env::args().skip(1);
    let (mut machine, mut params, mut out) = (None, None, None);
    let mut tier = "value".to_string();
    let mut dirs: Vec<PathBuf> = Vec::new();
    while let Some(a) = args.next() {
        match a.as_str() {
            "--machine" => machine = args.next(),
            "--params" => params = args.next(),
            "--out" => out = args.next(),
            "--card" => tier = "card".to_string(),
            dir => dirs.push(PathBuf::from(dir)),
        }
    }
    let card = Card::load(machine.expect("--machine")).expect("the machine");
    let text = fs::read_to_string(params.expect("--params")).expect("the params");
    let p = Params::of(&serde_json::from_str(&text).expect("JSON params"), &tier);

    let mut files = Vec::new();
    for d in dirs {
        for e in fs::read_dir(&d).unwrap_or_else(|e| panic!("{}: {e}", d.display())) {
            let path = e.expect("a directory entry").path();
            if path.extension().is_some_and(|x| x == "l1" || x == "ktpu") {
                files.push(path);
            }
        }
    }
    let found: Vec<Value> = files.par_iter().flat_map(|f| rows(f, &card, &p)).collect();
    let mut f = fs::File::create(out.expect("--out")).expect("the output");
    let errors = found.iter().filter(|r| r.get("error").is_some()).count();
    for r in &found {
        writeln!(f, "{r}").expect("a row");
    }
    println!("{} kernels, {errors} errors", found.len());
}
