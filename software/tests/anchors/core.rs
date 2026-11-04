//! Core words against the hardware: each image of `fixtures/anchors/core/` on
//! `vec_replay_tb` (`KTPU_SIM_VSIM`) and on the simulator under three schedules.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::rc::Rc;

use ktpu_isa::unit::vector::VecCu;
use ktpu_isa::{Isa, Machine as Card, W256};
use ktpu_l1::lower::image::{image, isa_for, reset, CoreFacts};
use ktpu_l1::text::read;
use ktpu_sim::l0::words;
use ktpu_sim::machine::Machine;
use ktpu_sim::Dram;

/// The bench's memory, in 32-byte words.
const WORDS: usize = 4096;
const MEM_BYTES: usize = WORDS * 32;
/// Each pointer parameter's span, the first at SPAN.
const SPAN: usize = 0x4000;
const SEEDS: [u64; 3] = [0, 1, 2];
/// Beside plain values: zeros, one, the extremes, a subnormal, an infinity.
const SPECIALS: [u16; 8] = [
    0x0000, 0x8000, 0x3C00, 0xBC00, 0x7BFF, 0x0400, 0x0001, 0x7C00,
];
/// The seeds are a few ulp off exact (NUM.E8.003), so the simulator's exact
/// values are held to this bound over a sweep of every eighth f16 code.
const SEED_ULPS: i32 = 4;

/// `|x|` in [128, 256] (f16 codes), where `vexp2` wraps and the compiler guards.
const EXP2_WRAPS: std::ops::RangeInclusive<u16> = 0x5800..=0x6000;
const ONE: u16 = 0x3C00;

fn input(seeds: bool) -> Vec<u8> {
    let mut mem = vec![0u8; MEM_BYTES];
    let mut x = 0x9E37_79B9u32;
    for (i, pair) in mem[SPAN..2 * SPAN].chunks_mut(2).enumerate() {
        x = x.wrapping_mul(1664525).wrapping_add(1013904223);
        let plain = (0x3000 + ((x >> 16) as u16 & 0x1FFF)) | ((x >> 8) as u16 & 0x8000);
        let v = match (seeds, i % 8 == 7) {
            (true, _) if EXP2_WRAPS.contains(&((i * 8) as u16 & 0x7FFF)) => ONE,
            (true, _) => (i * 8) as u16,
            (false, true) => SPECIALS[(i / 8) % 8],
            (false, false) => plain,
        };
        pair.copy_from_slice(&v.to_le_bytes());
    }
    mem
}

/// Steps between two f16 codes on the number line.
fn ulps(a: u16, b: u16) -> i32 {
    let line = |c: u16| {
        let m = (c & 0x7FFF) as i32;
        if c & 0x8000 != 0 {
            -m
        } else {
            m
        }
    };
    (line(a) - line(b)).abs()
}

/// The first word offset past `bound` ulps, a NaN only against a NaN.
fn apart(hw: &[u8], sim: &[u8], bound: i32) -> Option<usize> {
    let nan = |c: u16| c & 0x7C00 == 0x7C00 && c & 0x3FF != 0;
    let code = |m: &[u8], i: usize| u16::from_le_bytes([m[2 * i], m[2 * i + 1]]);
    (0..hw.len() / 2).find_map(|i| {
        let (a, b) = (code(hw, i), code(sim, i));
        let same = (nan(a) && nan(b)) || (!nan(a) && !nan(b) && ulps(a, b) <= bound);
        (!same).then_some(2 * i)
    })
}

fn hex(path: &Path, words: impl Iterator<Item = W256>) {
    let lines: String = words
        .map(|w| {
            format!(
                "{:016x}{:016x}{:016x}{:016x}\n",
                w.0[3], w.0[2], w.0[1], w.0[0]
            )
        })
        .collect();
    std::fs::write(path, lines).unwrap();
}

fn memory_words(mem: &[u8]) -> impl Iterator<Item = W256> + '_ {
    mem.chunks(32).map(|w| {
        W256(std::array::from_fn(|l| {
            u64::from_le_bytes(w[8 * l..8 * l + 8].try_into().unwrap())
        }))
    })
}

struct Hardware {
    memory: Vec<u8>,
    cycles: u64,
}

fn hardware(tag: &str, flits: &[W256], mem: &[u8]) -> Hardware {
    let vsim = std::env::var("KTPU_SIM_VSIM").unwrap();
    let dir = PathBuf::from(std::env::var("KTPU_SIM_RUNS").unwrap()).join(tag);
    std::fs::create_dir_all(&dir).unwrap();
    hex(&dir.join("prog.hex"), flits.iter().copied());
    hex(&dir.join("mem.hex"), memory_words(mem));
    let plus = |k: &str| format!("+{k}={}", dir.join(format!("{k}.hex")).display());
    let done = Command::new(&vsim)
        .args([
            plus("prog"),
            plus("mem"),
            plus("out"),
            "+memwait=1".to_string(),
        ])
        .output()
        .unwrap();
    let log = String::from_utf8_lossy(&done.stdout).to_string();
    let result = log.lines().find(|l| l.contains("@@@ REPLAY"));
    let faults = result.and_then(|l| {
        let f: Vec<&str> = l.split_whitespace().collect();
        f.iter()
            .position(|x| *x == "faults")
            .map(|i| f[i + 1].to_string())
    });
    assert_eq!(
        faults.as_deref(),
        Some("0"),
        "{tag}: {}",
        &log[log.len().saturating_sub(800)..]
    );
    let cycles = log
        .lines()
        .find_map(|l| l.split("@@@ ELAPSED ").nth(1))
        .and_then(|t| t.trim().parse().ok())
        .unwrap();
    let text = std::fs::read_to_string(dir.join("out.hex")).unwrap();
    let mut memory = Vec::with_capacity(MEM_BYTES);
    for line in text
        .split_whitespace()
        .filter(|l| !l.starts_with(['@', '/']))
    {
        for limb in (0..4).rev() {
            let at = limb * 16;
            let v = u64::from_str_radix(&line[at..at + 16], 16).unwrap();
            memory.extend_from_slice(&v.to_le_bytes());
        }
    }
    std::fs::remove_dir_all(&dir).unwrap();
    Hardware { memory, cycles }
}

fn simulator(isa: &Rc<Isa>, flits: &[W256], mem: &[u8], seed: u64) -> Vec<u8> {
    let mut dram = Dram::new(MEM_BYTES);
    dram.bytes.copy_from_slice(mem);
    let mut m = Machine::new(dram, 1, 0, 128, seed);
    m.isa = Some(isa.clone());
    for f in flits {
        m.send_vc(0, words::vector(isa, *f).unwrap()).unwrap();
    }
    m.until(Machine::idle).unwrap();
    m.mem.bytes
}

/// The bench's cycles, or the first bytes that differ.
fn anchor(
    card: &Card,
    isa: &Rc<Isa>,
    source: &str,
    name: &str,
    bound: Option<i32>,
) -> Result<u64, String> {
    let module = read(source, "anchor").map_err(|e| e.to_string())?;
    let state = reset(CoreFacts::of(card, module.vregs).vlmax);
    let params = module.images[name].params.len();
    let args: Vec<i128> = (1..=params).map(|i| (i * SPAN) as i128).collect();
    let (words, descs) = image(&module, card, name, &args, state).map_err(|e| e.to_string())?;
    let flits = flits(card, &descs, &words);
    let mem = input(bound.is_some());
    let hw = hardware(name, &flits, &mem);
    for seed in SEEDS {
        let sim = simulator(isa, &flits, &mem, seed);
        let first = match bound {
            Some(b) => apart(&hw.memory, &sim, b),
            None => sim.iter().zip(&hw.memory).position(|(a, b)| a != b),
        };
        if let Some(at) = first {
            let end = (at + 8).min(MEM_BYTES);
            return Err(format!(
                "{name} seed {seed} at {at:#x}: hardware {:02x?} simulator {:02x?}",
                &hw.memory[at..end],
                &sim[at..end]
            ));
        }
    }
    Ok(hw.cycles)
}

fn flits(card: &Card, descs: &BTreeMap<(u32, u32), u64>, words: &[u64]) -> Vec<W256> {
    let isa = isa_for(card, card.vc.regs).unwrap();
    let mut cu: Vec<VecCu> = descs
        .iter()
        .map(|(&(ad, fld), &value)| VecCu::Desc { ad, fld, value })
        .collect();
    cu.extend(words.iter().enumerate().map(|(i, &word)| VecCu::Imem {
        addr: i as u32,
        word,
    }));
    cu.push(VecCu::Run { pc: 0 });
    cu.iter().map(|c| c.encode(&isa).unwrap()).collect()
}

#[test]
fn every_image_ends_as_the_hardware() {
    let Ok(machine) = std::env::var("KTPU_SIM_MACHINE") else {
        return;
    };
    let card = Card::load(machine).unwrap();
    let isa = Rc::new(Isa::reference(&card).unwrap());
    let root =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../../tests/fixtures/anchors/core");
    let (mut wrong, mut total) = (Vec::new(), 0u64);
    for f in std::fs::read_dir(root).unwrap() {
        let path = f.unwrap().path();
        let bound = (path.file_stem().unwrap() == "seeds").then_some(SEED_ULPS);
        let source = std::fs::read_to_string(path).unwrap();
        let module = read(&source, "anchor").unwrap();
        for name in module.images.keys() {
            match anchor(&card, &isa, &source, name, bound) {
                Ok(cycles) => {
                    println!("{name}: {cycles} cycles");
                    total += cycles;
                }
                Err(e) => wrong.push(e),
            }
        }
    }
    println!("{total} cycles");
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
}
