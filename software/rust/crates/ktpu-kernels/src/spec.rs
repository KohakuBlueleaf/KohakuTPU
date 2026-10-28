//! Rust ports of the tier-0 runners the generators own
//! (`software/tests/spec/runners/l2.py` `l2.lanes`, `isa.py`
//! `isa.cluster.plan`, `num.py` `num.stat.words`), in ktpu-isa's `spec`
//! shape: a
//! port takes a vector's `input` and its machine and returns what the
//! Python runner returns.

use crate::gen::lanes::{self, Plan};
use crate::gen::Hw;
use crate::gen::{matmul, vector};
use crate::mutants;
use ktpu_machine::Machine;
use serde_json::{json, Map, Value};

/// Every mutant ktpu-kernels carries.
pub fn mutants() -> Vec<&'static str> {
    mutants::NAMES.to_vec()
}

/// Mutants a ported vector names whose mutated point is another crate's.
pub fn skipped_mutants() -> Vec<(&'static str, &'static str)> {
    vec![]
}

/// A runner port: `(input, machine) -> output or why it cannot run`.
pub type Port = fn(&Value, Option<&Machine>) -> Result<Value, String>;

const PORTS: &[(&str, Port)] = &[
    ("l2.lanes", l2_lanes),
    ("isa.cluster.plan", cluster_plan),
    ("num.stat.words", stat_words),
];

/// The machine a vector names, else the base machine file (a vector with
/// no machine: `rs.BASE`).
fn machine_or_base(m: Option<&Machine>) -> Result<Machine, String> {
    match m {
        Some(m) => Ok(m.clone()),
        None => Machine::named("multimesh_v9t6").map_err(|r| r.to_string()),
    }
}

/// `num.stat.words`: a statistic's values a word, and the words R values
/// take.
fn stat_words(inp: &Value, m: Option<&Machine>) -> Result<Value, String> {
    let hw = Hw::of(&machine_or_base(m)?);
    let rows = inp["rows"].as_array().ok_or("no rows")?;
    let words = rows
        .iter()
        .map(|r| r.as_i64().map(|r| vector::stat_words(r, &hw)))
        .collect::<Option<Vec<_>>>()
        .ok_or("a row count")?;
    Ok(json!({"per_word": hw.stat_rows(), "words": words}))
}

/// Every ported runner's name.
pub fn runners() -> Vec<&'static str> {
    PORTS.iter().map(|(n, _)| *n).collect()
}

/// Every ported runner, by its runner name, without a mutant.
pub fn ports() -> Vec<(&'static str, Port)> {
    PORTS.to_vec()
}

/// Run runner `runner` on `input` against `machine`, under `mutant` when
/// given (one of [`mutants`]).
pub fn run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    let port = PORTS
        .iter()
        .find(|(n, _)| *n == runner)
        .map(|(_, p)| *p)
        .ok_or_else(|| format!("runner {runner} has no Rust port"))?;
    let Some(name) = mutant else {
        return port(input, machine);
    };
    let Some(name) = mutants::NAMES.iter().copied().find(|n| *n == name) else {
        return Err(format!("unknown mutant {name}"));
    };
    #[cfg(feature = "mutants")]
    {
        mutants::with(name, || port(input, machine))
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = name;
        Err("ktpu-kernels is built without the `mutants` feature".into())
    }
}

fn int(v: &Value, k: &str) -> Result<i64, String> {
    v.get(k)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("no int {k}"))
}

fn walk(v: &Value, k: &str) -> Result<Vec<(i64, i64)>, String> {
    v.get(k)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("no walk {k}"))?
        .iter()
        .map(|l| match (l[0].as_i64(), l[1].as_i64()) {
            (Some(n), Some(s)) => Ok((n, s)),
            _ => Err(format!("a walk level {l}")),
        })
        .collect()
}

/// `isa.cluster.plan`: the contraction tiling (`kernels.matmul.plan`), or
/// `{"refused": ..}`.
fn cluster_plan(inp: &Value, m: Option<&Machine>) -> Result<Value, String> {
    let m = m.ok_or("the runner needs a machine")?;
    let p = &inp["plan"];
    let shared = p["shared"].as_bool().ok_or("no bool shared")?;
    match matmul::plan(
        int(p, "bt")?,
        int(p, "m")?,
        int(p, "n")?,
        int(p, "k")?,
        shared,
        int(p, "quantum")?,
        m,
    ) {
        Ok(mm) => Ok(json!({
            "bt": mm.bt, "m": mm.m, "n": mm.n, "k": mm.k, "mp": mm.mp, "kp": mm.kp,
            "tm": mm.tm, "tn": mm.tn, "nk": mm.nk, "shared": mm.shared, "quantum": mm.quantum,
        })),
        Err(r) => Ok(json!({"refused": r.clause})),
    }
}

/// `l2.lanes`: a lane relayout's plan (L2.REL) and what its gathers,
/// rotations, selects and scatters make of the destination, the source
/// holding element i = i and the destination `dst`.
fn l2_lanes(inp: &Value, m: Option<&Machine>) -> Result<Value, String> {
    let m = m.ok_or("the runner needs a machine")?;
    let keep = inp.get("keep").and_then(Value::as_bool).unwrap_or(false);
    let pl = lanes::plan(
        int(inp, "src_n")?,
        &walk(inp, "walk")?,
        int(inp, "at")?,
        &walk(inp, "dwalk")?,
        int(inp, "dat")?,
        int(inp, "m")?,
        keep,
        m,
    )
    .map_err(|r| r.to_string())?;
    let hw = Hw::of(m);
    let n = hw.lanes as usize;
    let got = json!({
        "classes": pl.classes.iter().map(|c| json!({
            "taps": c.taps.iter().map(|(r, b)| json!([r, b])).collect::<Vec<_>>(),
            "words": c.words,
            "keep": c.keep,
        })).collect::<Vec<_>>(),
        "fill": pl.fill,
        "index": pl.index.iter().map(|x| *x as i64).collect::<Vec<_>>(),
        "masks": pl.masks.chunks(hw.vlmax as usize)
            .map(|row| row[..n].iter().map(|b| i64::from(*b != 0)).collect::<Vec<_>>())
            .collect::<Vec<_>>(),
        "result": apply(&pl, inp, &hw)?,
    });
    let Some(want) = inp.get("want").and_then(Value::as_array) else {
        return Ok(got);
    };
    let mut out = Map::new();
    for k in want {
        let k = k.as_str().ok_or("a wanted key")?;
        out.insert(k.into(), got.get(k).cloned().ok_or(format!("no fact {k}"))?);
    }
    Ok(Value::Object(out))
}

/// The plan run on the host as the card runs it (`_apply`).
fn apply(pl: &Plan, inp: &Value, hw: &Hw) -> Result<Vec<i64>, String> {
    let n = hw.lanes as usize;
    let half = (hw.vlmax / hw.lanes) as usize;
    let fill = if pl.fill {
        0
    } else {
        inp.get("dst").and_then(Value::as_i64).unwrap_or(-1)
    };
    let mut dst = vec![vec![fill; n]; pl.dst_words as usize];
    let src_word = |w: usize| -> Vec<i64> { (0..n).map(|l| (w * n + l) as i64).collect() };
    let full = (1i64 << n) - 1;
    let (mut at, mut m0) = (0usize, 0usize);
    for c in &pl.classes {
        let ne = lanes::entries(c, hw) as usize;
        let nt = c.taps.len();
        let list = |t: usize, j: usize, r: usize| pl.index[at + (t * half + j) * ne + r] as usize;
        for k in 0..c.words as usize {
            let (r, j) = (k / half, k % half);
            let w = list(nt, j, r);
            let mut base = if c.keep { dst[w].clone() } else { vec![0; n] };
            for (t, (rot, bits)) in c.taps.iter().enumerate() {
                let s = src_word(list(t, j, r));
                let v: Vec<i64> = (0..n).map(|l| s[(l + *rot as usize) % n]).collect();
                let row = &pl.masks[(m0 + t) * hw.vlmax as usize..];
                base = if *bits == full {
                    v
                } else {
                    (0..n)
                        .map(|l| if row[l] != 0 { v[l] } else { base[l] })
                        .collect()
                };
            }
            dst[w] = base;
        }
        at += (nt + 1) * half * ne;
        m0 += nt;
    }
    let m = int(inp, "m")? as usize;
    Ok(dst.into_iter().flatten().take(m).collect())
}
