//! What every compiler stage's suite shares: a [`Pair`] of programs that must
//! end in the same parameters, on the fixtures and, to count, not under a mutant.
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::fs;
use std::panic::{catch_unwind, resume_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::Instant;

use ktpu::{Backend, CardStack};
use ktpu_isa::{Isa, Machine as Card, UnitKind};
use ktpu_l0::{Facts, Package};
use ktpu_l1::text::{read, Module, Term};
use ktpu_sim::l0;
use ktpu_sim::l1::{run as run_l1, Binds};
use ktpu_sim::l2;
use ktpu_sim::l2::view::{Buf, Layout};
use ktpu_sim::machine::Machine;
use ktpu_sim::mem::WORD;
use ktpu_sim::num;
use ktpu_sim::num::mx7::{self, Entry};
use ktpu_sim::Dram;
use rayon::prelude::*;

fn proof_phase(name: &str, phase: &str) {
    if std::env::var_os("KTPU_VERIFY_STATS").is_some() {
        eprintln!("verify phase {name}: {phase}");
    }
}

/// The emitted card program must prove its physical accesses for the cold
/// package and retained replay, with all sysnode packages present together.
pub fn verify_card_l2(
    text: &str,
    name: &str,
    card: &Card,
    binds: BTreeMap<String, i128>,
    memory: &ktpu_verify::MemoryContext,
) {
    let backend = CardStack::default();
    proof_phase(name, "parse");
    let l2 = backend.parse_l2(text).expect("card L2");
    proof_phase(name, "passes");
    let passed = backend.passes(&l2, name, card).expect("card passes");
    proof_phase(name, "lower");
    let l1 = backend.lower(&passed, name, card).expect("card L1");
    verify_card_module(&l1, name, card, binds, memory);
}

/// A supplied L1 delivery motif is the program that is certified, rather than
/// the graph's alternative L2 lowering.
pub fn verify_card_l1(
    text: &str,
    name: &str,
    card: &Card,
    binds: BTreeMap<String, i128>,
    memory: &ktpu_verify::MemoryContext,
) {
    let backend = CardStack::default();
    let module = backend.parse_l1(text).expect("card L1 motif");
    verify_card_module(&module, name, card, binds, memory);
}

fn verify_card_module(
    module: &Module,
    name: &str,
    card: &Card,
    mut binds: BTreeMap<String, i128>,
    memory: &ktpu_verify::MemoryContext,
) {
    let mut backend = CardStack::default();
    proof_phase(name, "schedule");
    let scheduled = backend.schedule(module, card).expect("card schedule");
    // L1.BODY.005 and L0.MEM.007: generated storage has physical addresses;
    // staging retains its actual node/aperture, while DRAM has one card arena.
    // Explicit caller bindings, including aliases, are never rebased here.
    let body = scheduled.body(name, "l1").expect("scheduled body");
    let address = card.die.address.runtime();
    let special = 1u64 << address.special_bit;
    let route = ((1u64 << address.mesh_bits) - 1) << address.mesh_shift;
    let topology = ktpu_machine::card::Card::of(card, None, None).expect("card topology");
    let mut next = BTreeMap::<u64, u64>::new();
    for (param, ty) in &body.params {
        let buffer = ktpu_l1::layout::buffer::Buffer::of(ty).expect("buffer layout");
        if let Some(&base) = binds.get(param) {
            let base = u64::try_from(base).expect("physical binding");
            let (base, domain) = if base & special == 0 {
                (base & !route & !(1u64 << address.reserved_bit), 0)
            } else {
                (base, base & !((1u64 << address.aperture_shift) - 1))
            };
            let end = base
                .checked_add(buffer.bytes(card).expect("buffer extent"))
                .expect("physical buffer extent");
            let at = next.entry(domain).or_insert(domain | 4096);
            *at = (*at).max(end.div_ceil(4096) * 4096);
        }
    }
    for (param, ty) in &body.params {
        if binds.contains_key(param) {
            continue;
        }
        let buffer = ktpu_l1::layout::buffer::Buffer::of(ty).expect("scratch layout");
        let bytes = buffer.bytes(card).expect("scratch extent");
        let domain = if buffer.space == "stage" {
            let node = buffer.layout.get("node").and_then(|v| v.int()).unwrap_or(0);
            let node = u32::try_from(node).expect("staging node");
            topology.stage_addr(0, node).expect("staging owner")
        } else {
            0
        };
        let at = next.entry(domain).or_insert(domain | 4096);
        let end = at
            .checked_add(bytes.max(1))
            .expect("physical scratch extent");
        if buffer.space == "stage" {
            // L0.MEM.007: the whole allocation, not only its base address,
            // must fit the destination's staging SRAM. Caller spans may have
            // advanced this arena, but cannot enlarge its physical capacity.
            assert!(
                end - domain <= topology.stage_bytes,
                "generated staging extent"
            );
        }
        binds.insert(param.clone(), i128::from(*at));
        *at = end.div_ceil(4096) * 4096;
    }
    backend.binds = Some(binds);
    proof_phase(name, "programs");
    let programs = backend
        .programs(&scheduled, name, None, card)
        .expect("card programs");
    proof_phase(name, "build and check memory");
    let findings = backend
        .check_memory(&programs, card, memory)
        .expect("card verification");
    proof_phase(name, "checked");
    assert!(
        !findings.iter().any(|f| backend.refuses(f)),
        "{name}: {findings:?}"
    );
}

const ALIGN: i64 = 4096;
/// A float op a stage rewrites stays this near (PRECISION, not identity).
const ULPS: i32 = 8;
/// Of a buffer's largest value: E8M15 lane rounding, a few ulp, that a sum
/// cancelling near zero leaves showing in the small result.
const ROUNDING: f64 = 1.0 / (1 << 13) as f64;
/// Orders of the guards' freedom a stage's output is run under: a race ends elsewhere.
const SEEDS: [u64; 4] = [2, 3, 5, 7];
/// Fixtures a mutant is tried on, the smallest of those that reach it
/// (`KTPU_STAGE_REACH`): the audit's cost against what it can tell.
fn reach() -> usize {
    let given = std::env::var("KTPU_STAGE_REACH");
    given.map_or(40, |n| n.parse().expect("a fixture count"))
}
/// Seconds past which a pair's comparison is reported: the budget's trace.
const SLOW: f64 = 1.0;

/// A level's text and its simulator.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    L2,
    L1,
    /// A hex package a line, `MESH HEX`, bound at [`binds_of`]'s addresses.
    L0,
}

impl Level {
    fn suffix(self) -> &'static str {
        match self {
            Level::L2 => "l2",
            Level::L1 | Level::L0 => "l1",
        }
    }
}

/// The program a stage took, and the one it made.
#[derive(Clone, Debug)]
pub struct Pair {
    pub name: String,
    pub before: (Level, String),
    pub after: (Level, String),
}

fn noise(bytes: &mut [u8]) {
    let mut x = 0x9E37_79B9u32;
    for pair in bytes.chunks_mut(2) {
        x = x.wrapping_mul(1664525).wrapping_add(1013904223);
        let v = (0x3000 + ((x >> 16) as u16 & 0x1FFF)) | ((x >> 8) as u16 & 0x8000);
        pair.copy_from_slice(&v.to_le_bytes()[..pair.len()]);
    }
}

/// `b`'s MXFP7 entries from small noise: raw bytes hold any block scale, and a
/// product past 128 leaves `exp2d`'s range (ISA.VC.065), not a stage's.
fn operands(bytes: &mut [u8], b: &Buf) {
    let side = b.dtype == "mx7b";
    let (from, n) = (b.at as usize, b.bytes() as usize);
    let mut raw = vec![0u8; mx7::LANES * mx7::KBLOCK * 2];
    for (i, e) in bytes[from..from + n]
        .chunks_mut(mx7::ENTRY_BYTES)
        .enumerate()
    {
        noise(&mut raw);
        let mut x = [[0u16; mx7::KBLOCK]; mx7::LANES];
        for (k, code) in x.iter_mut().flatten().enumerate() {
            let c = u16::from_le_bytes([raw[2 * k], raw[2 * k + 1]]);
            *code = (c ^ (i as u16).wrapping_mul(0x9E37)) & 0x83FF | 0x2000;
        }
        for (word, bytes) in mx7::entry(&x).write(side).iter().zip(e.chunks_mut(WORD)) {
            bytes.copy_from_slice(word);
        }
    }
}

struct Run {
    before: Module,
    after: Module,
    binds: Binds,
    params: Vec<(String, Buf)>,
    start: Vec<u8>,
}

/// Each parameter's place, in order: `(binds, spans, end)`.
/// `(binds, spans, end)` of laid-out parameters.
type Placed = (Binds, Vec<(String, Buf)>, i64);

fn laid_out(params: &[&[(String, Term)]]) -> Result<Placed, String> {
    let mut binds = Binds::new();
    let mut spans: Vec<(String, Buf)> = Vec::new();
    let mut at = ALIGN;
    for (name, ty) in params.iter().copied().flatten() {
        if binds.contains_key(name) {
            continue;
        }
        let b = Buf::of(ty, at)?;
        binds.insert(name.clone(), at);
        at += (b.bytes().max(ALIGN) + ALIGN - 1) / ALIGN * ALIGN;
        spans.push((name.clone(), b));
    }
    Ok((binds, spans, at))
}

/// Where a package's parameters sit: [`Pair`]s of `Level::L0` are bound here.
pub fn binds_of(l1: &Module, name: &str) -> Result<Binds, String> {
    let body = l1.body(name, "l1").map_err(|e| e.to_string())?;
    Ok(laid_out(&[&body.params])?.0)
}

fn start(p: &Pair) -> Result<Run, String> {
    let before = read(&p.before.1, "before").map_err(|e| e.to_string())?;
    let after = match p.after.0 {
        Level::L0 => before.clone(),
        _ => read(&p.after.1, "after").map_err(|e| e.to_string())?,
    };
    let body = |m: &Module, l: Level| {
        m.body(&p.name, l.suffix())
            .map(|b| b.params.clone())
            .map_err(|e| e.to_string())
    };
    let (first, second) = (body(&before, p.before.0)?, body(&after, p.after.0)?);
    let (binds, spans, at) = laid_out(&[&first, &second])?;
    let mut start = vec![0u8; at as usize];
    noise(&mut start);
    for (_, b) in spans
        .iter()
        .filter(|(_, b)| matches!(b.layout, Layout::Mx7 { .. }))
    {
        operands(&mut start, b);
    }
    for (_, b) in spans
        .iter()
        .filter(|(_, b)| b.dtype == "f32" || b.dtype == "u32")
    {
        let (from, n) = (b.at as usize, b.bytes() as usize);
        for (i, v) in start[from..from + n].chunks_mut(4).enumerate() {
            let index = (i % 2) as u32;
            let bits = if b.dtype == "f32" {
                (index as f32).to_bits()
            } else {
                index
            };
            v.copy_from_slice(&bits.to_le_bytes());
        }
    }
    let params = spans
        .into_iter()
        .filter(|(n, _)| first.iter().any(|(p, _)| p == n))
        .collect();
    Ok(Run {
        before,
        after,
        binds,
        params,
        start,
    })
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

/// A `Level::L0` text's packages run in turn on one mesh's units.
fn packages(text: &str, mem: Dram, seed: u64) -> Result<Vec<u8>, String> {
    let card = card();
    let mut machine = Machine::new(
        mem,
        card.units(UnitKind::Vc).len(),
        card.units(UnitKind::Mg).len(),
        128,
        seed,
    );
    machine.isa = Some(Rc::new(Isa::reference(&card).map_err(|e| e.to_string())?));
    for line in text.lines() {
        let (mesh, hex) = line.split_once(' ').ok_or("MESH HEX")?;
        let mesh: u32 = mesh.parse().map_err(|_| "a mesh")?;
        let facts = Facts::of_mesh(&card, mesh).map_err(|e| e.to_string())?;
        let raw: Vec<u8> = (0..hex.len() / 2)
            .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).map_err(|e| e.to_string()))
            .collect::<Result<_, _>>()?;
        let pkg = Package::from_bytes(&raw, &facts).map_err(|e| e.to_string())?;
        let units = l0::Units {
            vc: coords(&card, UnitKind::Vc),
            mg: coords(&card, UnitKind::Mg),
            src_mover: facts.src_mover(),
            counter_bits: facts.counter_bits,
        };
        l0::run(&pkg, &units, &mut machine)?;
    }
    Ok(machine.mem.bytes)
}

fn simulate(
    r: &Run,
    m: &Module,
    text: &(Level, String),
    name: &str,
    seed: u64,
) -> Result<Vec<u8>, String> {
    let mut mem = Dram::new(r.start.len());
    mem.bytes.copy_from_slice(&r.start);
    match text.0 {
        Level::L0 => packages(&text.1, mem, seed),
        Level::L1 => {
            let nodes = m.body(name, "l1").map_err(|e| e.to_string())?.stmts.iter();
            let meshes = nodes.filter(|s| s.op == "node").count().max(1);
            let mut machine = Machine::of_meshes(mem, meshes, 2, 4, 128, seed);
            run_l1(m, name, &r.binds, &mut machine)?;
            Ok(machine.mem.bytes)
        }
        Level::L2 => {
            l2::run(m, name, &r.binds, &mut mem, seed)?;
            Ok(mem.bytes)
        }
    }
}

fn float(c: u16) -> f64 {
    num::value(num::from_f16(c))
}

fn line(c: u16) -> i32 {
    let m = (c & 0x7FFF) as i32;
    if c & 0x8000 != 0 {
        -m
    } else {
        m
    }
}

/// MXFP7 entries as values: a zero block's scale is free, so codes may differ
/// by what a value does not read; within one quantisation step.
fn entries_apart(b: &Buf, want: &[u8], got: &[u8]) -> Option<usize> {
    let (from, n) = (b.at as usize, b.bytes() as usize);
    let side = b.dtype == "mx7b";
    (0..n).step_by(mx7::ENTRY_BYTES).find(|i| {
        let read = |m: &[u8]| Entry::read(&m[from + i..from + i + mx7::ENTRY_BYTES], side);
        let (x, y) = (read(want), read(got));
        (0..mx7::LANES).any(|l| {
            let step = mx7::value(1, x.field[l]).max(mx7::value(1, y.field[l]));
            (0..mx7::KBLOCK).any(|k| {
                (mx7::value(x.q[l][k], x.field[l]) - mx7::value(y.q[l][k], y.field[l])).abs() > step
            })
        })
    })
}

/// The parameter, offset, count, and values of the first difference past the bound.
pub type Apart = (String, usize, usize, u16, u16);

fn apart(params: &[(String, Buf)], want: &[u8], got: &[u8]) -> Option<Apart> {
    let mut first = None;
    let mut count = 0;
    for (p, b) in params {
        let (from, n) = (b.at as usize, b.bytes() as usize);
        if matches!(b.layout, Layout::Mx7 { .. }) {
            if let Some(i) = entries_apart(b, want, got) {
                count += 1;
                first.get_or_insert((p.clone(), i, 0, 0, 0));
            }
            continue;
        }
        let exact = b.dtype != "f16";
        let code = |m: &[u8], i: usize| u16::from_le_bytes([m[from + i], m[from + i + 1]]);
        let peak = (0..n)
            .step_by(2)
            .map(|i| float(code(want, i)).abs())
            .fold(0.0, f64::max);
        for i in (0..n).step_by(2) {
            let (x, y) = (code(want, i), code(got, i));
            let bound = if exact { 0 } else { ULPS };
            let cancelled = !exact && (float(x) - float(y)).abs() <= peak * ROUNDING;
            if (line(x) - line(y)).abs() > bound && !cancelled {
                count += 1;
                first.get_or_insert((p.clone(), i, 0, x, y));
            }
        }
    }
    first.map(|(p, i, _, x, y)| (p, i, count, x, y))
}

/// Where a pair parts, if it does; an error is what a simulator stopped on.
pub fn compare(p: &Pair) -> Result<Option<Apart>, String> {
    let t0 = Instant::now();
    let r = start(p)?;
    let want = simulate(&r, &r.before, &p.before, &p.name, 1)?;
    for seed in SEEDS {
        let got = simulate(&r, &r.after, &p.after, &p.name, seed)?;
        if let Some(parted) = apart(&r.params, &want, &got) {
            return Ok(Some(parted));
        }
    }
    if t0.elapsed().as_secs_f64() > SLOW {
        println!(
            "  slow: {} {:.2} s, {} KB",
            p.name,
            t0.elapsed().as_secs_f64(),
            r.start.len() >> 10
        );
    }
    Ok(None)
}

/// What `p`'s program (`after`, else `before`) stopped on from the seeded start
/// that `poke` changed, if it stopped.
pub fn stops(p: &Pair, after: bool, poke: &dyn Fn(&mut [u8], &Binds)) -> Option<String> {
    let mut r = start(p).expect("a pair's start");
    poke(&mut r.start, &r.binds);
    let (m, text) = if after {
        (&r.after, &p.after)
    } else {
        (&r.before, &p.before)
    };
    simulate(&r, m, text, &p.name, 1).err()
}

/// Compares every pair, reports, and refuses a stop or a difference.
pub fn judge(pairs: &[(String, Result<Pair, String>)]) {
    let t0 = Instant::now();
    let trace = std::env::var("KTPU_STAGE_TRACE").is_ok();
    let outcomes: Vec<_> = pairs
        .par_iter()
        .map(|(label, p)| {
            if trace {
                eprintln!("  {label}");
            }
            (label, p.as_ref().map_err(Clone::clone).and_then(compare))
        })
        .collect();
    let mut stops: BTreeMap<String, Vec<&String>> = BTreeMap::new();
    let mut differ = Vec::new();
    let mut same = 0;
    for (label, o) in outcomes {
        match o {
            Err(e) => {
                let key: String = e.chars().take(120).collect();
                stops.entry(key).or_default().push(label);
            }
            Ok(None) => same += 1,
            Ok(Some(d)) => differ.push((label, d)),
        }
    }
    println!("{same} same, {:.2} s", t0.elapsed().as_secs_f64());
    for (k, who) in &stops {
        println!("{:5} stop: {k} [{}]", who.len(), who[0]);
    }
    println!("{} differ:", differ.len());
    for d in differ.iter().take(16) {
        println!("  {d:?}");
    }
    assert!(stops.is_empty() && differ.is_empty());
}

/// The tree's `software/` directory.
pub fn software() -> PathBuf {
    let crate_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    crate_dir
        .ancestors()
        .nth(4)
        .expect("software/")
        .to_path_buf()
}

pub fn card() -> Card {
    let root = software().parent().expect("the repository").to_path_buf();
    Card::load(root.join("machines/multimesh_v9t8.json")).expect("the machine")
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    for f in fs::read_dir(dir).unwrap_or_else(|e| panic!("{}: {e}", dir.display())) {
        let path = f.expect("a directory entry").path();
        if path.is_dir() {
            walk(&path, out);
        } else if path.extension().is_some_and(|e| e == "ktpu") {
            out.push(path);
        }
    }
}

/// One kernel of a corpus file.
#[derive(Clone, Debug)]
pub struct Fixture {
    pub label: String,
    pub source: String,
    pub name: String,
}

/// Parameter bytes past which an example is a whole page of silicon, out of
/// budget: `tests/fixtures/l2` holds the same forms tiny.
const LARGE: i64 = 1 << 18;

fn small(module: &Module, name: &str, level: Level) -> Result<bool, String> {
    let body = module
        .body(name, level.suffix())
        .map_err(|e| e.to_string())?;
    let sizes = body.params.iter().map(|(_, t)| Ok(Buf::of(t, 0)?.bytes()));
    Ok(sizes
        .collect::<Result<Vec<i64>, String>>()?
        .iter()
        .sum::<i64>()
        <= LARGE)
}

/// Every fn of the `.ktpu` files under `dirs` (relative to `software/`).
pub fn fixtures(dirs: &[&str], level: Level) -> Vec<Fixture> {
    let mut files = Vec::new();
    for d in dirs {
        walk(&software().join(d), &mut files);
    }
    files.sort();
    let mut out = Vec::new();
    for path in files {
        let stem = path
            .file_stem()
            .expect("a file name")
            .to_string_lossy()
            .to_string();
        let source = fs::read_to_string(&path).expect("a fixture");
        let module = read(&source, "fixture").unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        for name in module.names() {
            if small(&module, &name, level) == Ok(true) {
                out.push(Fixture {
                    label: format!("{stem}/{name}"),
                    source: source.clone(),
                    name,
                });
            }
        }
    }
    out
}

/// A compiler stage on one fixture.
pub type Stage = fn(&Fixture, &Card) -> Result<Pair, String>;

/// A fixture, its stage's output and the mutants that stage asked after.
type Case<'a> = (&'a Fixture, &'a Pair, &'a [String]);

/// How a mutant fared against a stage's fixtures.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Fate {
    /// Refused, stopped, or ended elsewhere on this fixture.
    Caught(String),
    /// Some fixture's program changed, none ended differently.
    SameValues(String),
    /// No fixture's program changed.
    Untouched,
}

fn fate(mutant: &str, cases: &[Case], stage: Stage, card: &Card) -> Fate {
    let mut seen = Fate::Untouched;
    let watchable = ktpu::mutant::watchable(mutant);
    let reached = cases
        .iter()
        .filter(|(_, _, asked)| !watchable || asked.iter().any(|n| n == mutant));
    for (f, base, _) in reached.take(reach()) {
        let made = catch_unwind(AssertUnwindSafe(|| {
            ktpu::mutant::with(mutant, || stage(f, card)).expect("a mutant this build carries")
        }));
        let Ok(Ok(made)) = made else {
            return Fate::Caught(f.label.clone());
        };
        if made.after.1 == base.after.1 {
            continue;
        }
        let pair = Pair {
            name: base.name.clone(),
            before: base.before.clone(),
            after: made.after,
        };
        match catch_unwind(AssertUnwindSafe(|| compare(&pair))) {
            Ok(Ok(None)) => seen = Fate::SameValues(f.label.clone()),
            _ => return Fate::Caught(f.label.clone()),
        }
    }
    seen
}

/// `f` of each item in order, the pool's thread count at a time: each compile holds
/// a one-thread pool of its own, and a rayon worker waiting on it starts the next.
fn bounded<T: Sync, R: Send>(items: &[T], f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    let next = AtomicUsize::new(0);
    let worker = || {
        let mut got = Vec::new();
        loop {
            let i = next.fetch_add(1, Ordering::Relaxed);
            let Some(item) = items.get(i) else {
                return got;
            };
            got.push((i, f(item)));
        }
    };
    let mut done: Vec<(usize, R)> = thread::scope(|s| {
        let workers: Vec<_> = (0..rayon::current_num_threads())
            .map(|_| s.spawn(worker))
            .collect();
        workers
            .into_iter()
            .flat_map(|w| w.join().unwrap_or_else(|p| resume_unwind(p)))
            .collect()
    });
    done.sort_by_key(|(i, _)| *i);
    done.into_iter().map(|(_, r)| r).collect()
}

/// A stage's `known.txt`, one mutant a line, its first word: the mutants its
/// corpus cannot tell, each with what it did there.
pub fn known(text: &'static str) -> Vec<&'static str> {
    text.lines()
        .filter_map(|l| l.split_whitespace().next())
        .collect()
}

/// `KTPU_STAGE_MUTANTS=I/N`: the I-th of N shards of a suite's mutants.
fn shard() -> (usize, usize) {
    let given = std::env::var("KTPU_STAGE_MUTANTS").unwrap_or_default();
    let (i, n) = given.split_once('/').unwrap_or(("0", "1"));
    (
        i.parse().expect("a shard"),
        n.parse().expect("a shard count"),
    )
}

/// Runs `stage` over `corpus`, then with `KTPU_STAGE_MUTANTS` each mutant
/// `prefixes` names: one no outcome tells from the real stage must be in
/// `known`, and only there.
pub fn stage_suite(corpus: &[Fixture], stage: Stage, prefixes: &[&str], known: &[&str]) {
    let card = card();
    let t0 = Instant::now();
    let made = bounded(corpus, |f| {
        let t = Instant::now();
        let (p, asked) = ktpu::mutant::watched(|| stage(f, &card));
        if t.elapsed().as_secs_f64() > SLOW / 4.0 {
            println!(
                "  slow compile: {} {:.2} s",
                f.label,
                t.elapsed().as_secs_f64()
            );
        }
        ((f.label.clone(), p), asked)
    });
    type Made = Vec<(String, Result<Pair, String>)>;
    let (pairs, asked): (Made, Vec<_>) = made.into_iter().unzip();
    println!(
        "{} fixtures compiled, {:.2} s",
        pairs.len(),
        t0.elapsed().as_secs_f64()
    );
    if let Ok(dir) = std::env::var("KTPU_STAGE_DUMP") {
        for (label, p) in pairs
            .iter()
            .filter_map(|(l, p)| Some((l, p.as_ref().ok()?)))
        {
            let path = Path::new(&dir).join(label.replace('/', "-"));
            fs::write(path, format!("{}\n{}", p.before.1, p.after.1)).expect("a dump");
        }
    }
    judge(&pairs);
    if std::env::var("KTPU_STAGE_MUTANTS").is_err() {
        return;
    }
    let t1 = Instant::now();
    let mut order: Vec<usize> = (0..corpus.len()).collect();
    order.sort_by_key(|&i| corpus[i].source.len());
    let base: Vec<Case> = order
        .iter()
        .map(|&i| {
            (
                &corpus[i],
                pairs[i].1.as_ref().expect("judged"),
                asked[i].as_slice(),
            )
        })
        .collect();
    let (shard, shards) = shard();
    let names: Vec<&str> = ktpu::mutant::names()
        .into_iter()
        .filter(|n| prefixes.iter().any(|p| n.starts_with(p)))
        .enumerate()
        .filter(|(i, _)| i % shards == shard)
        .map(|(_, n)| n)
        .collect();
    let fates: Vec<(&str, Fate)> = bounded(&names, |n| {
        let t = Instant::now();
        let f = fate(n, &base, stage, &card);
        if std::env::var("KTPU_STAGE_TRACE").is_ok() {
            eprintln!("  {n} {:.1} s", t.elapsed().as_secs_f64());
        }
        (*n, f)
    });
    let caught = fates
        .iter()
        .filter(|(_, f)| matches!(f, Fate::Caught(_)))
        .count();
    println!(
        "{caught} of {} mutants caught, {:.2} s",
        fates.len(),
        t1.elapsed().as_secs_f64()
    );
    let survivors: Vec<_> = fates
        .iter()
        .filter(|(_, f)| !matches!(f, Fate::Caught(_)))
        .collect();
    for (n, f) in &survivors {
        println!("  {n}: {f:?}");
    }
    let unexpected: Vec<_> = survivors
        .iter()
        .filter(|(n, _)| !known.contains(n))
        .collect();
    let stale: Vec<_> = known
        .iter()
        .filter(|n| names.contains(n) && !survivors.iter().any(|(s, _)| s == *n))
        .collect();
    assert!(
        unexpected.is_empty() && stale.is_empty(),
        "{unexpected:?} {stale:?}"
    );
}
