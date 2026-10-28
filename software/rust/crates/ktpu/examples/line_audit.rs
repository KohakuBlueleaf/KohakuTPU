//! Offline write-line audit of the available frozen L1/L2 inputs.
//! Prints findings without accepting or rewriting any compiler witness.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use ktpu::pipeline::stage::Backend;
use ktpu::Stack;
use ktpu_l1::build::{programs_in, Builder, Opts};
use ktpu_l1::layout::{buffer::Buffer, scratch::arenas};
use ktpu_machine::Machine;
use serde_json::{json, Value};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../..");
    let mut paths: Vec<_> = std::fs::read_dir(root.join("software/rust/golden/e2e"))?
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .collect();
    paths.sort();
    let mut flagged = BTreeMap::new();
    let mut count = 0;
    for path in paths {
        let c: Value = serde_json::from_str(&std::fs::read_to_string(&path)?)?;
        let (Some(l1), Some(l2)) = (c["l1"].as_str(), c["l2"].as_str()) else {
            continue;
        };
        let m = Machine::named(c["machine"].as_str().unwrap())?
            .with_nodes(c["nodes"].as_u64().unwrap_or(4) as u32)?;
        let mo = ktpu_l1::text::read(l1, "<audit>")?;
        let p = Stack.parse_l2(l2)?;
        let name = c["name"].as_str().unwrap_or(&p.name);
        let caller: BTreeSet<_> = Stack.caller(&p, name)?.into_iter().collect();
        let ar = arenas(&mo, name, &caller, &m)?;
        let mut binds = BTreeMap::new();
        let mut next = 0x1000000u64;
        for (n, t) in &mo.body(name, "l1")?.params {
            if !caller.contains(n) {
                continue;
            }
            binds.insert(n.clone(), next as i128);
            next += Buffer::of(t)?.bytes(&m)?.div_ceil(4096) * 4096;
        }
        let mut bases = Vec::new();
        for a in ar {
            let size = a.bytes.div_ceil(4096) * 4096;
            bases.push((a, next));
            next += size;
        }
        let progs = match programs_in(&mo, name, &binds, &bases, &m, Some(m.nodes()), None) {
            Ok(p) => p,
            Err(e) => {
                eprintln!("{}: build refused: {e}", path.display());
                continue;
            }
        };
        let builder = Builder::new(&m)?;
        let target = ktpu_verify::Target::from_machine(&m, None)?;
        let mut findings = Vec::new();
        for np in &progs {
            let built = builder.build(np, Opts::default())?.build(true, false);
            let pkg = built.bind(&[])?;
            findings.extend(
                ktpu_verify::lines::check(&pkg, np.mesh, &target)
                    .iter()
                    .map(ToString::to_string),
            );
        }
        count += 1;
        if !findings.is_empty() {
            flagged.insert(
                path.file_stem().unwrap().to_string_lossy().to_string(),
                findings,
            );
        }
    }
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({"cases":count, "flagged":flagged}))?
    );
    Ok(())
}
