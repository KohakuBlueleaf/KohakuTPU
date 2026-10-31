//! Every kernel of a template library (`KTPU_SIM_LIBRARY`) on the L1 simulator
//! under three schedules: what stops, and what a schedule changes.

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Instant;

use ktpu_l1::text::{read, Term};
use ktpu_sim::l1::{run, Binds};
use ktpu_sim::machine::Machine;
use ktpu_sim::Dram;

const ALIGN: i64 = 4096;

/// Bytes a parameter of this type may take, its tile layouts included.
fn bytes(t: &Term) -> i64 {
    let ty = match t {
        Term::Placed(x, _) => x.as_ref(),
        other => other,
    };
    let Term::View(name, dims) = ty else {
        return ALIGN;
    };
    let n: i64 = dims
        .iter()
        .map(|d| d.as_int().unwrap_or(1) as i64)
        .product();
    let item = match name.as_str() {
        "f32" | "u32" | "i32" => 4,
        "u64" => 8,
        _ => 2,
    };
    (n * item).max(ALIGN)
}

fn noise(mem: &mut Dram) {
    let mut x = 0x9E37_79B9u32;
    for pair in mem.bytes.chunks_mut(2) {
        x = x.wrapping_mul(1664525).wrapping_add(1013904223);
        let v = 0x3000 + ((x >> 16) as u16 & 0x1FFF) | ((x >> 8) as u16 & 0x8000);
        pair.copy_from_slice(&v.to_le_bytes()[..pair.len()]);
    }
}

fn one(text: &str, seed: u64) -> Result<(Vec<u8>, usize), String> {
    let module = read(text, "library").map_err(|e| e.to_string())?;
    let name = module.names()[0].clone();
    let body = module.body(&name, "l1").map_err(|e| e.to_string())?;
    let mut at = ALIGN;
    let mut binds = Binds::new();
    for (p, t) in &body.params {
        binds.insert(p.clone(), at);
        at += (bytes(t) + ALIGN - 1) / ALIGN * ALIGN;
    }
    let mut mem = Dram::new(at as usize);
    noise(&mut mem);
    let mut m = Machine::new(mem, 2, 2, 128, seed);
    run(&module, &name, &binds, &mut m)?;
    Ok((m.mem.bytes, at as usize))
}

#[test]
fn every_kernel_runs_under_every_schedule() {
    let Ok(root) = std::env::var("KTPU_SIM_LIBRARY") else {
        return;
    };
    let t0 = Instant::now();
    let mut stops: BTreeMap<String, usize> = BTreeMap::new();
    let mut differ = Vec::new();
    let mut ran = 0;
    for f in std::fs::read_dir(Path::new(&root)).unwrap() {
        let path = f.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        if name.len() < 60 {
            continue;
        }
        let doc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let text = doc["compiled"]["l1"].as_str().unwrap();
        match one(text, 0) {
            Err(e) => {
                let key: String = e
                    .split(": ")
                    .last()
                    .unwrap_or(&e)
                    .chars()
                    .take(70)
                    .collect();
                *stops.entry(key).or_default() += 1;
            }
            Ok((base, _)) => {
                ran += 1;
                for seed in [1, 2] {
                    match one(text, seed) {
                        Ok((b, _)) if b == base => {}
                        Ok(_) => differ.push((name.clone(), seed)),
                        Err(e) => differ.push((format!("{name}: {e}"), seed)),
                    }
                }
            }
        }
    }
    println!("{ran} kernels ran, {:.1} s", t0.elapsed().as_secs_f64());
    for (k, n) in &stops {
        println!("{n:5} stop: {k}");
    }
    println!(
        "{} schedules differ: {:?}",
        differ.len(),
        &differ[..differ.len().min(6)]
    );
    assert!(stops.is_empty() && differ.is_empty());
}
