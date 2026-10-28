//! ISA properties observed after L1 building or L2 lowering.

use crate::{Backend, Stack};
use ktpu_isa::layout::value;
use ktpu_isa::unit::cluster::{ClusterCfg, ClusterIsa, ADDR_LO_BITS};
use ktpu_isa::unit::vector::{VecCfg, VecCu, VecIsa};
use ktpu_isa::W256;
use ktpu_l0::Stmt;
use ktpu_l1::lower::imem::{rewrite, Resident};
use ktpu_machine::{Machine, UnitKind};
use serde_json::{json, Map, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::path::Path;

pub const RUNNERS: &[&str] = &[
    "isa.cluster.fills",
    "isa.cluster.pairs",
    "isa.vector.descs",
    "isa.vector.resident",
    "isa.lower",
    "isa.cluster.l1",
    "isa.mover.l1",
];
type Sends = Vec<((u32, u32), Vec<W256>)>;

pub fn run(name: &str, inp: &Value, m: &Machine) -> Result<Value, String> {
    match name {
        "isa.cluster.l1" | "isa.mover.l1" => l1(name, inp, m),
        "isa.vector.resident" => resident(inp, m),
        "isa.lower" => lower(inp, m),
        "isa.cluster.fills" => fills(inp, m),
        "isa.cluster.pairs" => pairs(inp, m),
        "isa.vector.descs" => descs(inp, m),
        _ => Err(format!("no hosted ISA port for {name}")),
    }
}

fn binds(v: &Value) -> Result<BTreeMap<String, i128>, String> {
    v.as_object()
        .into_iter()
        .flatten()
        .map(|(k, v)| super::integer(v).map(|v| (k.clone(), v)))
        .collect()
}

fn units(m: &Machine, kind: UnitKind) -> Vec<(u32, u32)> {
    m.units(kind).iter().map(|c| (c.x(), c.y())).collect()
}

fn l1(name: &str, inp: &Value, machine: &Machine) -> Result<Value, String> {
    let m = match inp["nodes"].as_u64() {
        Some(n) => machine.with_nodes(n as u32).map_err(|e| e.to_string())?,
        None => machine.clone(),
    };
    let args = binds(&inp["binds"])?;
    let result =
        ktpu_l1::text::read(inp["text"].as_str().ok_or("no text")?, "<isa>").and_then(|module| {
            ktpu_l1::build::programs(
                &module,
                inp["fn"].as_str().unwrap_or(""),
                &args,
                &m,
                inp["nodes"].as_u64().map(|n| n as u32),
                None,
            )
        });
    let Ok(progs) = result else {
        return Ok(json!({"error": "TextError"}));
    };
    let np = progs.first().ok_or("no node program")?;
    if name == "isa.cluster.l1" {
        let mg = units(&m, UnitKind::Mg);
        return Ok(json!(np.program.steps.iter().filter_map(|s| match s {
            Stmt::Send { coord, words, .. } if mg.contains(&(coord.0, coord.1)) => Some(json!({
                "unit": [coord.0, coord.1], "payloads": words.iter().map(|w| w.to_hex()).collect::<Vec<_>>()
            })),
            _ => None,
        }).collect::<Vec<_>>()));
    }
    let kind = |_: (u32, u32)| String::new();
    let steps = np
        .program
        .steps
        .iter()
        .map(|s| ktpu_l1::spec::json::prog_step(&kind, s)[0].clone())
        .collect::<Vec<_>>();
    let moves = np
        .program
        .steps
        .iter()
        .filter_map(|s| match s {
            Stmt::Move { writes, .. } | Stmt::Post { writes, .. } => {
                let mut ops = vec![Vec::new()];
                for &(r, v) in writes
                    .iter()
                    .filter(|(r, _)| *r != ktpu_l0::format::MOVER_SKIP)
                {
                    ops.last_mut()
                        .unwrap()
                        .push(json!([format!("{r:#x}"), format!("{v:#x}")]));
                    if r == 0 {
                        ops.push(Vec::new());
                    }
                }
                ops.retain(|o| !o.is_empty());
                Some(ops)
            }
            _ => None,
        })
        .collect::<Vec<_>>();
    let mut out = json!({"steps": steps, "moves": moves});
    if inp["payloads"] == true {
        let builder = ktpu_l1::build::Builder::new(&m).map_err(|e| e.to_string())?;
        let mut cores = ktpu_l1::lower::imem::Cores::new(&m).map_err(|e| e.to_string())?;
        let pkg = builder
            .build(
                np,
                ktpu_l1::build::Opts {
                    fetch: None,
                    cores: Some(&mut cores),
                    relocate: false,
                },
            )
            .map_err(|e| e.to_string())?
            .build(false, false);
        out["mover"] = json!(pkg.steps.iter().filter(|s| s.op == ktpu_l0::Op::Mover).map(|s| {
            let (a, n) = (s.arg as usize, s.count as usize);
            json!({"payloads": pkg.payloads[a..a+n].iter().map(|w| w.to_hex()).collect::<Vec<_>>(), "flags": s.flags})
        }).collect::<Vec<_>>());
    }
    Ok(out)
}

fn example(name: &str) -> Result<String, String> {
    fn find(dir: &Path, file: &str, out: &mut Vec<std::path::PathBuf>) {
        for e in std::fs::read_dir(dir).into_iter().flatten().flatten() {
            let p = e.path();
            if p.is_dir() {
                find(&p, file, out);
            } else if p.file_name().is_some_and(|n| n == file) {
                out.push(p);
            }
        }
    }
    let root =
        ktpu_machine::machines_dir().join("../software/language/kohakutpu/language/examples/l2");
    let mut hits = Vec::new();
    find(&root, &format!("{name}.ktpu"), &mut hits);
    match hits.as_slice() {
        [one] => std::fs::read_to_string(one).map_err(|e| e.to_string()),
        _ => Err(format!("{} examples named {name}", hits.len())),
    }
}

fn sends(l1: &ktpu_l1::text::Module, name: &str, m: &Machine) -> Result<Sends, String> {
    let params = &l1.body(name, "l1").map_err(|e| e.to_string())?.params;
    let args = params
        .iter()
        .enumerate()
        .map(|(i, (p, _))| (p.clone(), 0x10_0000 * (i as i128 + 1)))
        .collect();
    let progs =
        ktpu_l1::build::programs(l1, name, &args, m, None, None).map_err(|e| e.to_string())?;
    Ok(progs
        .iter()
        .flat_map(|np| &np.program.steps)
        .filter_map(|s| match s {
            Stmt::Send { coord, words, .. } => Some(((coord.0, coord.1), words.clone())),
            _ => None,
        })
        .collect())
}

fn compiled(
    inp: &Value,
    m: &Machine,
    kind: UnitKind,
) -> Result<BTreeMap<(u32, u32), Vec<W256>>, String> {
    let name = inp["example"].as_str().ok_or("no example")?;
    let b = Stack;
    let l2 = b.parse_l2(&example(name)?).map_err(|e| e.to_string())?;
    let l1 = b
        .passes(&l2, name, m)
        .and_then(|p| b.lower(&p, name, m))
        .and_then(|l| b.schedule(&l, m))
        .map_err(|e| e.to_string())?;
    let of = units(m, kind);
    let mut out: BTreeMap<_, Vec<_>> = BTreeMap::new();
    for (u, words) in sends(&l1, name, m)?
        .into_iter()
        .filter(|(u, _)| of.contains(u))
    {
        out.entry(u).or_default().extend(words);
    }
    Ok(out)
}

fn fills(inp: &Value, m: &Machine) -> Result<Value, String> {
    let name = inp["name"].as_str().ok_or("no name")?;
    let b = Stack;
    let l1 = b
        .parse_l2(inp["text"].as_str().ok_or("no text")?)
        .and_then(|p| b.lower(&p, name, m))
        .map_err(|e| e.to_string())?;
    let isa = ClusterIsa::of(ClusterCfg::of(m).map_err(|e| e.to_string())?);
    let mg = units(m, UnitKind::Mg);
    let mut out = Vec::new();
    for (u, words) in sends(&l1, name, m)? {
        if !mg.contains(&u) {
            continue;
        }
        for w in words {
            let (fmt, f) = isa.set.decode(w).map_err(|e| e.to_string())?;
            if fmt == "FILL" {
                out.push(json!([
                    value(&f, "sel"),
                    value(&f, "fbank"),
                    value(&f, "eoff"),
                    value(&f, "n")
                ]));
            }
        }
    }
    Ok(json!(out))
}

fn pairs(inp: &Value, m: &Machine) -> Result<Value, String> {
    let isa = ClusterIsa::of(ClusterCfg::of(m).map_err(|e| e.to_string())?);
    let (mut emits, mut paired, mut plain) = (0, true, 0);
    let mut first_acc = BTreeSet::new();
    for words in compiled(inp, m, UnitKind::Mg)?.into_values() {
        let mut open = VecDeque::new();
        let mut gemms = 0;
        for w in words {
            let (fmt, f) = isa.set.decode(w).map_err(|e| e.to_string())?;
            let addr = value(&f, "addr") | value(&f, "addr_hi") << ADDR_LO_BITS;
            match fmt {
                "GEMM" => {
                    if gemms == 0 {
                        first_acc.insert(value(&f, "acc"));
                    }
                    gemms += 1;
                    if value(&f, "emit") != 0 {
                        emits += 1;
                        open.push_back((value(&f, "gm") * value(&f, "gn"), addr));
                    }
                }
                "DRAIN" if value(&f, "fuse") != 0 => {
                    paired &= open.pop_front() == Some((value(&f, "n"), addr))
                }
                "DRAIN" => plain += 1,
                _ => {}
            }
        }
        paired &= open.is_empty();
    }
    Ok(
        json!({"emits": emits > 0, "paired": paired, "plain": plain, "first_acc": first_acc.into_iter().collect::<Vec<_>>()}),
    )
}

fn descs(inp: &Value, m: &Machine) -> Result<Value, String> {
    let isa = VecIsa::of(VecCfg::of(m).map_err(|e| e.to_string())?);
    let (mut n, mut whole) = (0, true);
    for words in compiled(inp, m, UnitKind::Vc)?.into_values() {
        for w in words {
            let Some(VecCu::Desc { fld, value, .. }) = isa.decode_cu(w) else {
                continue;
            };
            n += 1;
            let v = if fld == 0 {
                value as i64
            } else {
                let s = ((value >> 16) & 0x3ffff) as i64;
                s - ((s >> 17) << 18)
            };
            whole &= v % m.die.word_bytes as i64 == 0;
        }
    }
    Ok(json!({"descs": n > 0, "whole_words": whole}))
}

fn resident(inp: &Value, m: &Machine) -> Result<Value, String> {
    let isa = VecIsa::new(m).map_err(|e| e.to_string())?;
    let reference = VecIsa::of(VecCfg::of(m).map_err(|e| e.to_string())?);
    let core = *units(m, UnitKind::Vc).first().ok_or("no vector core")?;
    let mut held = Resident::new(m.vc.imem_words, m.vc.run_reach);
    let mut out = Vec::new();
    for call in inp["calls"].as_array().ok_or("no calls")? {
        let module = ktpu_l1::text::read(call["text"].as_str().ok_or("no text")?, "<resident>")
            .map_err(|e| e.to_string())?;
        let progs = ktpu_l1::build::programs(
            &module,
            call["fn"].as_str().ok_or("no fn")?,
            &binds(&call["binds"])?,
            m,
            None,
            None,
        )
        .map_err(|e| e.to_string())?;
        let words = progs
            .iter()
            .flat_map(|np| &np.program.steps)
            .filter_map(|s| match s {
                Stmt::Send { coord, words, .. } if (coord.0, coord.1) == core => Some(words),
                _ => None,
            })
            .flatten()
            .copied()
            .collect::<Vec<_>>();
        let got = rewrite(&isa, &words, &mut held).map_err(|e| e.to_string())?;
        let (mut imem, mut desc, mut run_pc) = (0, 0, Vec::new());
        for w in got {
            match reference.decode_cu(w) {
                Some(VecCu::Imem { .. }) => imem += 1,
                Some(VecCu::Desc { .. }) => desc += 1,
                Some(VecCu::Run { pc }) => run_pc.push(pc),
                None => {}
            }
        }
        out.push(json!({"imem": imem, "desc": desc, "run_pc": run_pc}));
    }
    Ok(json!(out))
}

fn lower(inp: &Value, m: &Machine) -> Result<Value, String> {
    use ktpu_l2::{Program, Search, Strategy, Target};
    use regex::Regex;
    let name = inp["name"].as_str().ok_or("no name")?;
    let text = match inp["text"].as_str() {
        Some(t) => t.to_string(),
        None => example(inp["example"].as_str().ok_or("no example")?)?,
    };
    let strategy = match inp["strategy"].as_str() {
        Some(s) => Strategy::parse(s).ok_or("no strategy")?,
        None => Strategy::Auto,
    };
    let t = Target::of(m);
    let p = Program::parse(&text, "<isa.lower>", Some(name))
        .map_err(|e| e.to_string())?
        .with_vregs(t.vregs, t.vcopies);
    let l1 = ktpu_l2::lower::lower(&p, &t, &Search::new(strategy)).map_err(|e| e.to_string())?;
    let lines = l1.lines().map(str::trim).collect::<Vec<_>>();
    let pats = inp["patterns"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(Regex::new)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    let first = pats
        .iter()
        .map(|p| {
            lines
                .iter()
                .position(|l| p.is_match(l))
                .map_or(-1, |i| i as i64)
        })
        .collect::<Vec<_>>();
    let mut out = Map::new();
    out.insert(
        "found".into(),
        json!(first.iter().map(|i| *i >= 0).collect::<Vec<_>>()),
    );
    if first.len() > 1 {
        out.insert(
            "in_order".into(),
            json!(first.windows(2).all(|w| 0 <= w[0] && w[0] < w[1])),
        );
        if let Some(absent) = inp["absent"]
            .as_str()
            .filter(|_| first.iter().all(|i| *i >= 0))
        {
            let re = Regex::new(absent).map_err(|e| e.to_string())?;
            out.insert(
                "between".into(),
                json!(lines[first[0] as usize..first[1] as usize]
                    .iter()
                    .filter(|l| re.is_match(l))
                    .count()),
            );
        }
    }
    if let Some(keep) = inp["keep"].as_str() {
        let re = Regex::new(keep).map_err(|e| e.to_string())?;
        out.insert(
            "kept".into(),
            json!(lines
                .iter()
                .filter_map(|l| re.captures(l))
                .map(|c| c
                    .iter()
                    .skip(1)
                    .map(|g| g.map_or(Value::Null, |g| json!(g.as_str())))
                    .collect::<Vec<_>>())
                .collect::<Vec<_>>()),
        );
    }
    Ok(Value::Object(out))
}
