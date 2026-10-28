//! The SRU configuration map, the pool schedule and E8M15 constants
//! (`docs/spec/stack/isa/sru.md` ISA.SR.020-033, `isa/sru.py`). A
//! configuration is 32-bit words at 8-bit addresses written by CFG; every
//! field is (word, low bit, width).

use ktpu_machine::{refuse, Machine, Refusal};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A field of the map: word (relative to its block), low bit, width.
pub type Slot = (u32, u32, u32);

/// Global words.
pub const G: &[(&str, Slot)] = &[
    ("NW", (0x00, 0, 12)),
    ("NPASS", (0x00, 12, 2)),
    ("BUF", (0x00, 14, 1)),
    ("USE_B", (0x00, 15, 1)),
    ("OUT", (0x00, 16, 2)),
    ("MASK", (0x00, 18, 1)),
    ("R1", (0x00, 19, 1)),
    ("NSR", (0x01, 0, 16)),
    ("ROW0", (0x01, 16, 9)),
    ("COL0", (0x02, 0, 9)),
    ("DHI", (0x02, 16, 16)),
    ("CLIM", (0x03, 0, 16)),
    ("MVAL", (0x04, 0, 24)),
    ("COLOFF", (0x05, 0, 16)),
    ("ROWOFF", (0x05, 16, 16)),
    ("K0", (0x08, 0, 24)),
    ("K1", (0x09, 0, 24)),
    ("K2", (0x0A, 0, 24)),
    ("K3", (0x0B, 0, 24)),
];

/// Pass p's block is at `PASS_BASE + PASS_STRIDE * p`.
pub const PASS_BASE: u32 = 0x10;
pub const PASS_STRIDE: u32 = 0x02;
pub const P: &[(&str, Slot)] = &[
    ("NOPS", (0, 0, 4)),
    ("OPBASE", (0, 4, 4)),
    ("R2EN", (0, 8, 1)),
    ("EMIT", (0, 9, 1)),
    ("FINLEN", (0, 10, 4)),
    ("FINBASE", (0, 14, 4)),
];

/// Op i of the op store is at `OP_BASE + OP_STRIDE * i`.
pub const OP_BASE: u32 = 0x40;
pub const OP_STRIDE: u32 = 0x04;
pub const O: &[(&str, Slot)] = &[
    ("KIND", (0, 0, 1)),
    ("A", (0, 1, 2)),
    ("B", (0, 3, 3)),
    ("C", (0, 6, 3)),
    ("NA", (0, 9, 1)),
    ("NC", (0, 10, 1)),
    ("RB", (0, 11, 2)),
    ("RC", (0, 13, 2)),
    ("CB", (0, 15, 1)),
    ("CC", (0, 16, 1)),
    ("FIX", (0, 17, 1)),
    ("CS", (0, 18, 2)),
    ("PRE", (0, 20, 6)),
    ("HA", (0, 26, 1)),
    ("HB", (0, 27, 1)),
    ("HC", (0, 28, 1)),
    ("MANT", (0, 29, 1)),
    ("NEG", (0, 30, 1)),
    ("KB", (1, 0, 24)),
    ("KW", (1, 24, 5)),
    ("KC", (2, 0, 24)),
    ("D0", (3, 0, 5)),
    ("D1", (3, 5, 5)),
];

/// A source: A in X, B, O (the pair partner's X), H; B and C in K, R, C,
/// B, X, H.
pub const ASRC: &[(&str, i64)] = &[("X", 0), ("B", 1), ("O", 2), ("H", 3)];
pub const BSRC: &[(&str, i64)] = &[("K", 0), ("R", 1), ("C", 2), ("B", 3), ("X", 4), ("H", 5)];

/// Finalise op i is word `FIN_BASE + i`: (low bit, width).
pub const FIN_BASE: u32 = 0x80;
pub const F: &[(&str, (u32, u32))] = &[
    ("OP", (0, 5)),
    ("DST", (5, 2)),
    ("A", (7, 4)),
    ("B", (11, 4)),
    ("C", (15, 4)),
];
pub const FOP: &[(&str, i64)] = &[
    ("mov", 0),
    ("add", 3),
    ("sub", 4),
    ("mul", 5),
    ("fma", 6),
    ("max", 8),
    ("min", 9),
    ("rcp", 10),
    ("rsqrt", 11),
];
pub const SRC: &[(&str, i64)] = &[
    ("R0", 0),
    ("R1", 1),
    ("R2", 2),
    ("T", 3),
    ("ST1", 4),
    ("ST2", 5),
    ("K0", 6),
    ("K1", 7),
    ("K2", 8),
    ("K3", 9),
];

/// Configuration word address bits.
pub const ADDR_BITS: u32 = 8;

pub fn lookup<T: Copy>(table: &[(&str, T)], name: &str) -> Option<T> {
    table.iter().find(|(n, _)| *n == name).map(|(_, v)| *v)
}

/// The whole configuration map, as `words` and `schedule` read it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigMap {
    pub g: Vec<(&'static str, Slot)>,
    pub p: Vec<(&'static str, Slot)>,
    pub o: Vec<(&'static str, Slot)>,
    pub f: Vec<(&'static str, (u32, u32))>,
    pub asrc: Vec<(&'static str, i64)>,
    pub bsrc: Vec<(&'static str, i64)>,
    pub fop: Vec<(&'static str, i64)>,
    pub src: Vec<(&'static str, i64)>,
    pub pass_base: u32,
    pub pass_stride: u32,
    pub op_base: u32,
    pub op_stride: u32,
    pub fin_base: u32,
}

fn set<T>(table: &mut [(&'static str, T)], name: &str, v: T) {
    if let Some(e) = table.iter_mut().find(|(n, _)| *n == name) {
        e.1 = v;
    }
}

/// The configuration map (the tables above).
pub fn map() -> ConfigMap {
    let mut m = ConfigMap {
        g: G.to_vec(),
        p: P.to_vec(),
        o: O.to_vec(),
        f: F.to_vec(),
        asrc: ASRC.to_vec(),
        bsrc: BSRC.to_vec(),
        fop: FOP.to_vec(),
        src: SRC.to_vec(),
        pass_base: PASS_BASE,
        pass_stride: PASS_STRIDE,
        op_base: OP_BASE,
        op_stride: OP_STRIDE,
        fin_base: FIN_BASE,
    };
    if crate::mutant::on("isa.sru/map") {
        set(&mut m.g, "NW", (0x01, 0, 12));
        set(&mut m.g, "NSR", (0x00, 0, 16));
        set(&mut m.g, "OUT", (0x00, 17, 2));
        set(&mut m.g, "K0", (0x0C, 0, 24));
        m.pass_stride = 4;
        set(&mut m.o, "KIND", (0, 1, 1));
        set(&mut m.o, "A", (0, 0, 2));
        set(&mut m.o, "KW", (1, 25, 5));
        set(&mut m.o, "D1", (3, 6, 5));
        set(&mut m.f, "OP", (2, 5));
        set(&mut m.f, "DST", (0, 2));
        set(&mut m.fop, "add", 2);
        set(&mut m.fop, "rsqrt", 12);
        set(&mut m.src, "ST1", 5);
        set(&mut m.src, "K0", 7);
        set(&mut m.bsrc, "C", 3);
        m.op_base = 0x41;
        m.fin_base = 0x81;
    }
    m
}

/// The pool the schedule is built for: the profile's lanes and taps, the
/// timing of `sru_pool.v`, the elements of one word.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pool {
    pub f_lanes: u32,
    pub h_taps: u32,
    pub ldep: i64,
    pub wlead: i64,
    pub taps: i64,
    pub word_elems: u32,
}

impl Pool {
    /// The machine's SRU pool, or the refusal on a die without SR units.
    pub fn of(m: &Machine) -> Result<Pool, Refusal> {
        let sr = m.sr_facts().or_else(|| {
            if crate::mutant::on("rul.014/profile-reloaded") {
                Machine::named(&m.name).ok()?.sr_facts()
            } else {
                None
            }
        });
        let Some(sr) = sr else {
            return refuse("ISA.SR.001", format!("{} has no SR units", m.name));
        };
        let schedule = crate::mutant::on("isa.sru/schedule");
        Ok(Pool {
            f_lanes: sr.profile.f_lanes,
            h_taps: sr.profile.h_taps,
            ldep: if schedule { 17 } else { sr.ldep as i64 },
            wlead: if schedule { 3 } else { sr.wlead as i64 },
            taps: sr.taps as i64,
            word_elems: m.die.word_bytes / 2,
        })
    }
}

/// `x` as an E8M15 word ([`crate::numerics::e8m15`]).
pub fn e8(x: f64) -> u32 {
    crate::numerics::e8m15(x)
}

/// `{word address: value}` of `values` (field -> value) over `fields`.
pub fn pack(
    fields: &[(&str, Slot)],
    values: &BTreeMap<String, i64>,
) -> Result<BTreeMap<u32, u32>, Refusal> {
    let mut words = BTreeMap::new();
    for (name, v) in values {
        let Some((a, lo, w)) = lookup(fields, name) else {
            return refuse("ISA.SR.024", format!("no configuration field {name}"));
        };
        if !(0..1i64 << w).contains(v) {
            return refuse("ISA.SR.024", format!("{name}={v} does not fit {w} bits"));
        }
        *words.entry(a).or_insert(0) |= (*v as u32) << lo;
    }
    Ok(words)
}

/// One pool-op entry of a program, `isa/sru.py`'s form: O fields by name,
/// and the sources `A`, `B`, `C` by name (`"X"`) or as an earlier op's
/// result (`["H", k]`). A source left out is X (A) or K (B, C).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum OpValue {
    Int(i64),
    Name(String),
    Tap(String, usize),
}

pub type PoolOp = BTreeMap<String, OpValue>;

/// A program's op-store fields, its II, and each op's issue slot sigma.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Schedule {
    pub fields: Vec<BTreeMap<String, i64>>,
    pub ii: i64,
    pub sigma: Vec<i64>,
}

fn source(op: &PoolOp, s: &str) -> OpValue {
    op.get(s)
        .cloned()
        .unwrap_or_else(|| OpValue::Name(if s == "A" { "X" } else { "K" }.into()))
}

fn tap_of(v: &OpValue) -> Option<usize> {
    match v {
        OpValue::Tap(_, k) => Some(*k),
        _ => None,
    }
}

/// Schedule `prog` on `pool`: issue offsets on the modulo schedule, word-slot
/// depths and result-tap depths (ISA.SR.030-032).
pub fn schedule(prog: &[PoolOp], pool: &Pool) -> Result<Schedule, Refusal> {
    if pool.f_lanes == 0 || !pool.word_elems.is_multiple_of(pool.f_lanes) {
        return refuse(
            "ISA.SR.030",
            format!("a pool of {} lanes runs no program", pool.f_lanes),
        );
    }
    let m = (pool.word_elems / pool.f_lanes) as i64;
    let ii = prog.len() as i64 * m;
    let mut sig: Vec<i64> = Vec::new();
    for (i, op) in prog.iter().enumerate() {
        let srcs: Vec<usize> = ["A", "B", "C"]
            .iter()
            .filter_map(|s| tap_of(&source(op, s)))
            .collect();
        if srcs.iter().any(|&k| k >= i) {
            return refuse(
                "ISA.SR.032",
                format!("op {i}: reads the result of a later op"),
            );
        }
        let lo = srcs
            .iter()
            .map(|&k| sig[k] + pool.ldep)
            .max()
            .unwrap_or(0)
            .max(0);
        let mut k = 0;
        while i as i64 * m + k * ii < lo {
            k += 1;
        }
        sig.push(i as i64 * m + k * ii);
    }
    let lead = if ii > 0 {
        (pool.wlead + ii - 1) / ii
    } else {
        0
    };
    let cmap = map();
    let mut out = Vec::with_capacity(prog.len());
    for (i, op) in prog.iter().enumerate() {
        let mut f: BTreeMap<String, i64> = BTreeMap::new();
        for (name, v) in op {
            if matches!(name.as_str(), "A" | "B" | "C") {
                continue;
            }
            match v {
                OpValue::Int(x) => {
                    f.insert(name.clone(), *x);
                }
                _ => return refuse("ISA.SR.022", format!("op {i}: field {name} takes a number")),
            }
        }
        let mut taps: Vec<usize> = Vec::new();
        for s in ["A", "B", "C"] {
            let table = if s == "A" { &cmap.asrc } else { &cmap.bsrc };
            match source(op, s) {
                OpValue::Tap(_, k) => {
                    if !taps.contains(&k) {
                        taps.push(k);
                    }
                    if taps.len() > pool.h_taps as usize {
                        return refuse(
                            "ISA.SR.032",
                            format!("op {i}: more than {} earlier results", pool.h_taps),
                        );
                    }
                    f.insert(s.into(), lookup(table, "H").unwrap());
                    f.insert(
                        format!("H{s}"),
                        taps.iter().position(|t| *t == k).unwrap() as i64,
                    );
                }
                OpValue::Name(n) => match lookup(table, &n) {
                    Some(code) => {
                        f.insert(s.into(), code);
                    }
                    None => return refuse("ISA.SR.022", format!("op {i}: {s} has no source {n}")),
                },
                OpValue::Int(x) => {
                    return refuse("ISA.SR.022", format!("op {i}: {s} is a source, not {x}"))
                }
            }
        }
        for (t, &kk) in taps.iter().enumerate() {
            let d = sig[i] - sig[kk] - pool.ldep;
            if !(0..pool.taps).contains(&d) {
                return refuse(
                    "ISA.SR.032",
                    format!("op {i}: result of op {kk} is {d} deep"),
                );
            }
            f.insert(format!("D{t}"), d);
        }
        let kw = sig[i] / ii + lead;
        if kw >= pool.taps {
            return refuse("ISA.SR.032", format!("op {i}: {kw} word slots deep"));
        }
        f.insert("KW".into(), kw);
        out.push(f);
    }
    Ok(Schedule {
        fields: out,
        ii,
        sigma: sig,
    })
}

/// A job's configuration, `words`'s inputs: G fields, up to `sr.npass`
/// passes of P fields, the op store (programs by OPBASE / NOPS) and the
/// finalise program (F fields).
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Job {
    pub g: BTreeMap<String, i64>,
    pub passes: Vec<BTreeMap<String, i64>>,
    pub ops: Vec<PoolOp>,
    pub fin: Vec<BTreeMap<String, i64>>,
}

/// Every configuration word of `job`: `{address: value}`. Each pass's
/// program (its NOPS ops from OPBASE) is scheduled on `pool`.
pub fn words(job: &Job, pool: &Pool) -> Result<BTreeMap<u32, u32>, Refusal> {
    let cmap = map();
    let mask = |w: u32| (1i64 << w) - 1;
    let mut g = BTreeMap::new();
    for (k, v) in &job.g {
        let Some((_, _, w)) = lookup(&cmap.g, k) else {
            return refuse("ISA.SR.024", format!("no global field {k}"));
        };
        g.insert(k.clone(), v & mask(w));
    }
    let mut out = pack(&cmap.g, &g)?;
    for (p, d) in job.passes.iter().enumerate() {
        let base = cmap.pass_base + cmap.pass_stride * p as u32;
        for (a, v) in pack(&cmap.p, d)? {
            *out.entry(base + a).or_insert(0) |= v;
        }
        let nops = d.get("NOPS").copied().unwrap_or(0);
        if nops != 0 {
            let opbase = d.get("OPBASE").copied().unwrap_or(0).max(0) as usize;
            let end = (opbase + nops as usize).min(job.ops.len());
            let prog = &job.ops[opbase.min(end)..end];
            for (i, f) in schedule(prog, pool)?.fields.into_iter().enumerate() {
                let mut masked = BTreeMap::new();
                for (k, v) in f {
                    let Some((_, _, w)) = lookup(&cmap.o, &k) else {
                        return refuse("ISA.SR.024", format!("no op field {k}"));
                    };
                    masked.insert(k, v & mask(w));
                }
                for (a, v) in pack(&cmap.o, &masked)? {
                    out.insert(cmap.op_base + cmap.op_stride * (opbase + i) as u32 + a, v);
                }
            }
        }
    }
    for (i, f) in job.fin.iter().enumerate() {
        let mut w = 0u32;
        for (k, (lo, wd)) in &cmap.f {
            w |= ((f.get(*k).copied().unwrap_or(0) & mask(*wd)) as u32) << lo;
        }
        out.insert(cmap.fin_base + i as u32, w);
    }
    Ok(out)
}
