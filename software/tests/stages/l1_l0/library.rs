//! L1 to L0 over a template library (`KTPU_SIM_LIBRARY`, machine
//! `KTPU_SIM_MACHINE`): each kernel's L1 and its package end in the same memory.

use std::collections::BTreeMap;
use std::path::Path;
use std::rc::Rc;
use std::time::Instant;

use ktpu_isa::{Isa, Machine as Card, UnitKind};
use ktpu_l1::build::{programs, Builder, Opts};
use ktpu_l1::lower::imem::Cores;
use ktpu_l1::text::{read, Module, Term};
use ktpu_sim::l0;
use ktpu_sim::l1::{run, Binds};
use ktpu_sim::machine::Machine;
use ktpu_sim::units::mover::Move;
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

fn fp32(t: &Term) -> bool {
    let ty = match t {
        Term::Placed(x, _) => x.as_ref(),
        other => other,
    };
    matches!(ty, Term::View(name, _) if name == "f32")
}

fn noise(mem: &mut Dram) {
    let mut x = 0x9E37_79B9u32;
    for pair in mem.bytes.chunks_mut(2) {
        x = x.wrapping_mul(1664525).wrapping_add(1013904223);
        let v = 0x3000 + ((x >> 16) as u16 & 0x1FFF) | ((x >> 8) as u16 & 0x8000);
        pair.copy_from_slice(&v.to_le_bytes()[..pair.len()]);
    }
}

struct Case {
    module: Module,
    name: String,
    binds: Binds,
    size: usize,
    /// Spans of fp32 parameters, which a gather may read as indices.
    indices: Vec<(usize, usize)>,
}

fn case(text: &str) -> Result<Case, String> {
    let module = read(text, "library").map_err(|e| e.to_string())?;
    let name = module.names()[0].clone();
    let body = module.body(&name, "l1").map_err(|e| e.to_string())?;
    let mut at = ALIGN;
    let mut binds = Binds::new();
    let mut indices = Vec::new();
    for (p, t) in &body.params {
        binds.insert(p.clone(), at);
        let n = bytes(t);
        if fp32(t) {
            indices.push((at as usize, n as usize));
        }
        at += (n + ALIGN - 1) / ALIGN * ALIGN;
    }
    let size = at as usize;
    Ok(Case {
        module,
        name,
        binds,
        size,
        indices,
    })
}

fn machine(c: &Case, seed: u64) -> Machine {
    let mut mem = Dram::new(c.size);
    noise(&mut mem);
    for (at, n) in &c.indices {
        for (i, v) in mem.bytes[*at..at + n].chunks_mut(4).enumerate() {
            let index = (i % 2) as f32;
            v.copy_from_slice(&index.to_le_bytes()[..v.len()]);
        }
    }
    Machine::new(mem, 2, 2, 128, seed)
}

fn on_l1(c: &Case, seed: u64) -> Result<(Vec<u8>, Vec<Move>), String> {
    let mut m = machine(c, seed);
    run(&c.module, &c.name, &c.binds, &mut m)?;
    Ok((m.mem.bytes, m.posted))
}

fn coords(card: &Card, kind: UnitKind) -> Vec<(u64, u64)> {
    let mut c: Vec<_> = card
        .units(kind)
        .iter()
        .map(|u| (u.x() as u64, u.y() as u64))
        .collect();
    c.sort();
    c
}

fn on_l0(c: &Case, card: &Card, isa: &Rc<Isa>, seed: u64) -> Result<(Vec<u8>, Vec<Move>), String> {
    let binds: BTreeMap<String, i128> = c
        .binds
        .iter()
        .map(|(k, v)| (k.clone(), *v as i128))
        .collect();
    let nps =
        programs(&c.module, &c.name, &binds, card, Some(1), None).map_err(|e| e.to_string())?;
    let builder = Builder::new(card).map_err(|e| e.to_string())?;
    let mut cores = Cores::new(card).map_err(|e| e.to_string())?;
    let mut m = machine(c, seed);
    m.isa = Some(isa.clone());
    for np in &nps {
        let facts = ktpu_l0::Facts::of_mesh(card, np.mesh).map_err(|e| e.to_string())?;
        let opts = Opts {
            fetch: Some((card.die.fetch_port.x(), card.die.fetch_port.y())),
            cores: Some(&mut cores),
            relocate: false,
        };
        let (pkg, _) = builder.package(np, opts).map_err(|e| e.to_string())?;
        let units = l0::Units {
            vc: coords(card, UnitKind::Vc),
            mg: coords(card, UnitKind::Mg),
            src_mover: facts.src_mover(),
            counter_bits: facts.counter_bits,
        };
        l0::run(&pkg, &units, &mut m)?;
    }
    Ok((m.mem.bytes, m.posted))
}

#[test]
fn every_package_ends_as_its_l1() {
    let (Ok(root), Ok(path)) = (
        std::env::var("KTPU_SIM_LIBRARY"),
        std::env::var("KTPU_SIM_MACHINE"),
    ) else {
        return;
    };
    let card = Card::load(path).unwrap();
    let isa = Rc::new(Isa::reference(&card).unwrap());
    let t0 = Instant::now();
    let mut stops: BTreeMap<String, usize> = BTreeMap::new();
    let (mut same, mut differ) = (0, Vec::new());
    for f in std::fs::read_dir(Path::new(&root)).unwrap() {
        let path = f.unwrap().path();
        let name = path.file_name().unwrap().to_string_lossy().to_string();
        if name.len() < 60 {
            continue;
        }
        let doc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let text = doc["compiled"]["l1"].as_str().unwrap();
        let parsed = case(text);
        let outcome = parsed.as_ref().map_err(Clone::clone).and_then(|c| {
            let want = on_l1(c, 1)?;
            on_l0(c, &card, &isa, 2).map(|got| (want, got))
        });
        match outcome {
            Err(e) => {
                let tail = e.split(": ").skip(1).collect::<Vec<_>>().join(": ");
                let key: String = tail.chars().take(90).collect();
                *stops.entry(key).or_default() += 1;
            }
            Ok(((want, _), (got, _))) if want == got => same += 1,
            Ok(((want, moves), (got, posted))) => {
                let at = want.iter().zip(&got).position(|(a, b)| a != b).unwrap();
                let first = moves.iter().zip(&posted).position(|(a, b)| a != b);
                if let Some(i) = first.filter(|_| differ.len() < 3) {
                    println!(
                        "move {i} of {}:\n  {:?}\n  {:?}",
                        moves.len(),
                        moves[i],
                        posted[i]
                    );
                }
                let c = parsed.as_ref().unwrap();
                let mut params: Vec<_> = c.binds.iter().map(|(k, v)| (*v as usize, k)).collect();
                params.sort();
                let mut spans = String::new();
                for (i, (from, p)) in params.iter().enumerate() {
                    let to = params.get(i + 1).map_or(want.len(), |x| x.0);
                    let n = (*from..to).filter(|&b| want[b] != got[b]).count();
                    if n > 0 {
                        spans += &format!(" {p}:{n}");
                    }
                }
                differ.push((name[..12].to_string(), at, text.len(), spans));
            }
        }
    }
    differ.sort_by_key(|d| d.2);
    println!("{same} same, {:.1} s", t0.elapsed().as_secs_f64());
    for (k, n) in &stops {
        println!("{n:5} stop: {k}");
    }
    println!(
        "{} differ: {:?}",
        differ.len(),
        &differ[..differ.len().min(12)]
    );
    assert!(stops.is_empty() && differ.is_empty());
}
