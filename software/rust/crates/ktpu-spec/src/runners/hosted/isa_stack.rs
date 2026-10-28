//! The `isa.*` runners that compile through the stack (L2 lowered, L1 built
//! into node programs), which ktpu-isa cannot depend on: each reads the
//! words the programs send and answers with ktpu-isa's decoders.

use ktpu::{Backend, Stack};
use ktpu_isa::layout::value;
use ktpu_isa::unit::cluster::{ClusterCfg, ClusterIsa, ADDR_LO_BITS};
use ktpu_isa::unit::vector::{VecCfg, VecCu, VecIsa};
use ktpu_isa::W256;
use ktpu_l0::Stmt;
use ktpu_l1::lower::image::{image, reset, UNKNOWN};
use ktpu_l1::lower::imem::{rewrite, Resident};
use ktpu_l2::{Program, Search, Strategy, Target};
use ktpu_machine::{Machine, UnitKind};
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// The runners this module answers.
pub const RUNNERS: &[&str] = &[
    "isa.cluster.fills",
    "isa.cluster.pairs",
    "isa.vector.descs",
    "isa.vector.resident",
    "isa.lower",
    "isa.cluster.l1",
    "isa.mover.l1",
    "isa.vector.image",
    "isa.verify",
];

/// Runner `runner` on `input` against `machine` (no mutant: the caller
/// scopes one).
pub fn run(runner: &str, input: &Value, machine: Option<&Machine>) -> Result<Value, String> {
    let m = machine.ok_or_else(|| format!("{runner} runs against a machine"))?;
    match runner {
        "isa.cluster.fills" => cluster_fills(input, m),
        "isa.cluster.pairs" => cluster_pairs(input, m),
        "isa.vector.descs" => vector_descs(input, m),
        "isa.vector.resident" => vector_resident(input, m),
        "isa.lower" => lower_text(input, m),
        "isa.cluster.l1" => cluster_l1(input, m),
        "isa.mover.l1" => mover_l1(input, m),
        "isa.vector.image" => vector_image(input, m),
        "isa.verify" => verify_run(input, m),
        _ => Err(format!("runner {runner} is not hosted here")),
    }
}

/// `{"error": "TextError"}`: what the Python runners answer when the L1
/// text layer (reading, expanding, building) refuses a program.
fn text_error() -> Value {
    json!({ "error": "TextError" })
}

/// The binds a vector names (`{param: address}`).
fn binds_of(v: &Value) -> BTreeMap<String, i128> {
    v.as_object()
        .map(|b| {
            b.iter()
                .map(|(k, v)| (k.clone(), v.as_i64().unwrap_or(0) as i128))
                .collect()
        })
        .unwrap_or_default()
}

/// The first node program of L1 `text`'s `fn`, at the vector's binds, on
/// `m` (as `nodes` dies of it when given); Err(None) when the text layer
/// refuses it.
fn l1_program(
    inp: &Value,
    m: &Machine,
) -> Result<Result<ktpu_l1::lower::node::NodeProgram, ()>, String> {
    let text = inp["text"].as_str().ok_or("no text")?;
    let name = inp["fn"].as_str().ok_or("no fn")?;
    let nodes = inp["nodes"].as_u64().map(|n| n as u32);
    let m = match nodes {
        Some(n) => m.with_nodes(n).map_err(|e| e.to_string())?,
        None => m.clone(),
    };
    let built = ktpu_l1::text::read(text, "<isa>").and_then(|module| {
        ktpu_l1::build::programs(&module, name, &binds_of(&inp["binds"]), &m, nodes, None)
    });
    Ok(match built {
        Ok(progs) => progs
            .into_iter()
            .next()
            .ok_or_else(|| "no node program".to_string())
            .map(Ok)?,
        Err(_) => Err(()),
    })
}

/// An L1 image's IMEM words and the descriptor fields it leads with, from
/// the core's reset state (`entry: "reset"`) or an unknown one.
fn vector_image(inp: &Value, m: &Machine) -> Result<Value, String> {
    let text = inp["text"].as_str().ok_or("no text")?;
    let name = inp["name"].as_str().ok_or("no name")?;
    let entry = if inp["entry"] == "reset" {
        reset(m.vc.vlmax)
    } else {
        UNKNOWN
    };
    let args: Vec<i128> = inp["args"]
        .as_array()
        .map(|a| a.iter().filter_map(Value::as_i64).map(i128::from).collect())
        .unwrap_or_default();
    let got = ktpu_l1::text::read(text, "<isa.vector.image>")
        .and_then(|module| image(&module, m, name, &args, entry));
    let Ok((words, descs)) = got else {
        return Ok(text_error());
    };
    let mut d = Map::new();
    for ((ad, fld), v) in descs {
        d.insert(format!("{ad},{fld}"), json!(format!("{v:#x}")));
    }
    Ok(json!({
        "words": words.iter().map(|w| format!("{w:#x}")).collect::<Vec<_>>(),
        "descs": d,
    }))
}

/// The static checker on an L1 program built at the vector's binds: its
/// finding kinds, and with `l1` the L1 words each vector core read and
/// wrote on the first run (`ktpu_verify::spec::verify_json`).
fn verify_run(inp: &Value, m: &Machine) -> Result<Value, String> {
    let Ok(np) = l1_program(inp, m)? else {
        return Ok(text_error());
    };
    let builder = ktpu_l1::build::Builder::new(m).map_err(|e| e.to_string())?;
    let mut cores = ktpu_l1::lower::imem::Cores::new(m).map_err(|e| e.to_string())?;
    let opts = ktpu_l1::build::Opts {
        fetch: None,
        cores: Some(&mut cores),
        relocate: false,
    };
    let Ok(built) = builder.build(&np, opts) else {
        return Ok(text_error());
    };
    let mut nodes = ktpu_verify::Nodes::new();
    nodes.insert(np.mesh, built.build(false, false));
    let t = ktpu_verify::Target::from_machine(m, None)?;
    let want_l1 = inp["l1"].as_bool().unwrap_or(false);
    Ok(ktpu_verify::spec::verify_json(&[nodes], &t, want_l1))
}

/// One `(register, value)` write as the Python runners show it.
fn write(r: u64, v: u64) -> Value {
    json!([format!("{r:#x}"), format!("{v:#x}")])
}

/// A move step's writes, op by op: each op ends at its CTRL write (register
/// 0, the GO); `MOVER_SKIP` placeholders are no writes.
fn ops(writes: &[(u64, u64)]) -> Vec<Vec<Value>> {
    let mut out = vec![Vec::new()];
    for &(r, v) in writes
        .iter()
        .filter(|(r, _)| *r != ktpu_l0::format::MOVER_SKIP)
    {
        out.last_mut().unwrap().push(write(r, v));
        if r == 0 {
            out.push(Vec::new());
        }
    }
    out.retain(|o| !o.is_empty());
    out
}

/// An L1 program's move steps: each step's name, each move's ops' register
/// writes; with `payloads`, the bound package's MOVER steps (payloads,
/// flags).
fn mover_l1(inp: &Value, m: &Machine) -> Result<Value, String> {
    let Ok(np) = l1_program(inp, m)? else {
        return Ok(text_error());
    };
    let kind = |_: (u32, u32)| String::new();
    let steps: Vec<Value> = np
        .program
        .steps
        .iter()
        .map(|s| ktpu_l1::spec::json::prog_step(&kind, s)[0].clone())
        .collect();
    let moves: Vec<Value> = np
        .program
        .steps
        .iter()
        .filter_map(|s| match s {
            Stmt::Move { writes, .. } | Stmt::Post { writes, .. } => Some(json!(ops(writes))),
            _ => None,
        })
        .collect();
    let mut out = Map::new();
    out.insert("steps".into(), json!(steps));
    out.insert("moves".into(), json!(moves));
    if inp["payloads"] == Value::Bool(true) {
        let builder = ktpu_l1::build::Builder::new(m).map_err(|e| e.to_string())?;
        let mut cores = ktpu_l1::lower::imem::Cores::new(m).map_err(|e| e.to_string())?;
        let opts = ktpu_l1::build::Opts {
            fetch: None,
            cores: Some(&mut cores),
            relocate: false,
        };
        let pkg = builder
            .build(&np, opts)
            .map_err(|e| e.to_string())?
            .build(false, false);
        let mover: Vec<Value> = pkg
            .steps
            .iter()
            .filter(|s| s.op == ktpu_l0::Op::Mover)
            .map(|s| {
                let (a, n) = (s.arg as usize, s.count as usize);
                json!({
                    "payloads": pkg.payloads[a..a + n].iter().map(|w| w.to_hex()).collect::<Vec<_>>(),
                    "flags": s.flags,
                })
            })
            .collect();
        out.insert("mover".into(), json!(mover));
    }
    Ok(Value::Object(out))
}

/// An L1 program's sends to clusters: the unit and each payload.
fn cluster_l1(inp: &Value, m: &Machine) -> Result<Value, String> {
    let Ok(np) = l1_program(inp, m)? else {
        return Ok(text_error());
    };
    let mg = units(m, UnitKind::Mg);
    Ok(Value::Array(
        np.program
            .steps
            .iter()
            .filter_map(|s| match s {
                Stmt::Send { coord, words, .. } if mg.contains(&(coord.0, coord.1)) => {
                    Some(json!({
                        "unit": [coord.0, coord.1],
                        "payloads": words.iter().map(|w| w.to_hex()).collect::<Vec<_>>(),
                    }))
                }
                _ => None,
            })
            .collect(),
    ))
}

/// The L2 example `name` (one file of that stem under the examples' `l2`).
fn example(name: &str) -> Result<String, String> {
    fn find(dir: &Path, file: &str, out: &mut Vec<PathBuf>) {
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
        [one] => std::fs::read_to_string(one).map_err(|e| format!("{}: {e}", one.display())),
        _ => Err(format!("{} L2 examples named {name}", hits.len())),
    }
}

/// Sends in step order: each unit's `(x, y)` and the words sent it.
type Sends = Vec<((u32, u32), Vec<W256>)>;

/// An L2 example compiled as `kernel.compile` does (passes, lowering, image
/// schedule): every send of its node programs.
fn compiled_sends(example_name: &str, m: &Machine) -> Result<Sends, String> {
    let s = Stack;
    let l2 = s
        .parse_l2(&example(example_name)?)
        .map_err(|r| r.to_string())?;
    let l1 = s
        .passes(&l2, example_name, m)
        .and_then(|p| s.lower(&p, example_name, m))
        .and_then(|l| s.schedule(&l, m))
        .map_err(|r| r.to_string())?;
    sends(&l1, example_name, m)
}

/// Every `send` of `fn name.l1` of `l1`, node by node in step order: the
/// unit and its words, each parameter bound at its own 1 MB-aligned address
/// (the Python runners' binds).
fn sends(l1: &ktpu_l1::text::Module, name: &str, m: &Machine) -> Result<Sends, String> {
    let params = &l1.body(name, "l1").map_err(|e| e.to_string())?.params;
    let binds: BTreeMap<String, i128> = params
        .iter()
        .enumerate()
        .map(|(i, (p, _))| (p.clone(), 0x10_0000 * (i as i128 + 1)))
        .collect();
    let progs =
        ktpu_l1::build::programs(l1, name, &binds, m, None, None).map_err(|e| e.to_string())?;
    Ok(progs
        .iter()
        .flat_map(|np| &np.program.steps)
        .filter_map(|s| match s {
            Stmt::Send { coord, words, .. } => Some(((coord.0, coord.1), words.clone())),
            _ => None,
        })
        .collect())
}

/// The cluster formats as the machine defines them, no mutant applied: the
/// reference a mutated encoder's words are read against (the Python runners
/// decode with `ISA.set`, which the mutants leave alone).
fn cluster_decoder(m: &Machine) -> Result<ClusterIsa, String> {
    Ok(ClusterIsa::of(
        ClusterCfg::of(m).map_err(|r| r.to_string())?,
    ))
}

/// The vector CU formats as the machine defines them, no mutant applied.
fn vector_decoder(m: &Machine) -> Result<VecIsa, String> {
    Ok(VecIsa::of(VecCfg::of(m).map_err(|r| r.to_string())?))
}

/// `m`'s units of `kind` on mesh 0, as `(x, y)`.
fn units(m: &Machine, kind: UnitKind) -> Vec<(u32, u32)> {
    m.units(kind).iter().map(|c| (c.x(), c.y())).collect()
}

/// L2 text lowered (no passes) and built: each cluster FILL's (side, bank,
/// entry offset, entries), in order (ISA.MG.011, .012).
fn cluster_fills(inp: &Value, m: &Machine) -> Result<Value, String> {
    let text = inp["text"].as_str().ok_or("no text")?;
    let name = inp["name"].as_str().ok_or("no name")?;
    let s = Stack;
    let l1 = s
        .parse_l2(text)
        .and_then(|l2| s.lower(&l2, name, m))
        .map_err(|r| r.to_string())?;
    let isa = cluster_decoder(m)?;
    let mg = units(m, UnitKind::Mg);
    let mut out = Vec::new();
    for (u, words) in sends(&l1, name, m)? {
        if !mg.contains(&u) {
            continue;
        }
        for w in words {
            let (fmt, f) = isa.set.decode(w).map_err(|r| r.to_string())?;
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
    Ok(Value::Array(out))
}

/// `sends`, each unit's words in order, the units of `kind` only.
fn by_unit(sends: Sends, m: &Machine, kind: UnitKind) -> BTreeMap<(u32, u32), Vec<W256>> {
    let of = units(m, kind);
    let mut out: BTreeMap<(u32, u32), Vec<W256>> = BTreeMap::new();
    for (u, words) in sends.into_iter().filter(|(u, _)| of.contains(u)) {
        out.entry(u).or_default().extend(words);
    }
    out
}

/// A compiled L2 example's cluster words, per cluster: whether it emits,
/// whether each emitting GEMM is collected by the next fused DRAIN of
/// `n = gm * gn` at the same address, how many drains are plain, and the
/// acc bit of each cluster's first GEMM (ISA.MG.030, .082).
fn cluster_pairs(inp: &Value, m: &Machine) -> Result<Value, String> {
    let name = inp["example"].as_str().ok_or("no example")?;
    let isa = cluster_decoder(m)?;
    let (mut emits, mut paired, mut plain) = (0, true, 0);
    let mut first_acc = std::collections::BTreeSet::new();
    for words in by_unit(compiled_sends(name, m)?, m, UnitKind::Mg).into_values() {
        let mut open = std::collections::VecDeque::new();
        let mut gemms = 0;
        for w in words {
            let (fmt, f) = isa.set.decode(w).map_err(|r| r.to_string())?;
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
                    paired &= open.pop_front() == Some((value(&f, "n"), addr));
                }
                "DRAIN" => plain += 1,
                _ => {}
            }
        }
        paired &= open.is_empty();
    }
    Ok(json!({
        "emits": emits > 0,
        "paired": paired,
        "plain": plain,
        "first_acc": first_acc.into_iter().collect::<Vec<_>>(),
    }))
}

/// A compiled L2 example's DESC words to vector cores: whether there are
/// any, and whether every base and stride is a whole number of words
/// (ISA.VC.123).
fn vector_descs(inp: &Value, m: &Machine) -> Result<Value, String> {
    let name = inp["example"].as_str().ok_or("no example")?;
    let isa = vector_decoder(m)?;
    let word = m.die.word_bytes as i64;
    let (mut n, mut whole) = (0, true);
    for words in by_unit(compiled_sends(name, m)?, m, UnitKind::Vc).into_values() {
        for w in words {
            let Some(VecCu::Desc { fld, value, .. }) = isa.decode_cu(w) else {
                continue;
            };
            n += 1;
            let v = if fld == 0 {
                value as i64
            } else {
                let s = ((value >> 16) & 0x3FFFF) as i64;
                s - ((s >> 17) << 18)
            };
            whole &= v % word == 0;
        }
    }
    Ok(json!({ "descs": n > 0, "whole_words": whole }))
}

/// Each call's words to vector core 0 rewritten against what the core
/// holds from the calls before: IMEM, DESC and RUN counts and the RUN pcs
/// (ISA.VC.021, .037).
fn vector_resident(inp: &Value, m: &Machine) -> Result<Value, String> {
    let isa = VecIsa::new(m).map_err(|r| r.to_string())?;
    let reference = vector_decoder(m)?;
    let core = *units(m, UnitKind::Vc).first().ok_or("no vector core")?;
    let mut held = Resident::new(m.vc.imem_words, m.vc.run_reach);
    let mut out = Vec::new();
    for call in inp["calls"].as_array().ok_or("no calls")? {
        let text = call["text"].as_str().ok_or("a call has no text")?;
        let name = call["fn"].as_str().ok_or("a call has no fn")?;
        let binds: BTreeMap<String, i128> = call["binds"]
            .as_object()
            .map(|b| {
                b.iter()
                    .map(|(k, v)| (k.clone(), v.as_i64().unwrap_or(0) as i128))
                    .collect()
            })
            .unwrap_or_default();
        let module =
            ktpu_l1::text::read(text, "<isa.vector.resident>").map_err(|e| e.to_string())?;
        let progs = ktpu_l1::build::programs(&module, name, &binds, m, None, None)
            .map_err(|e| e.to_string())?;
        let words: Vec<W256> = progs
            .iter()
            .flat_map(|np| &np.program.steps)
            .filter_map(|s| match s {
                Stmt::Send { coord, words, .. } if (coord.0, coord.1) == core => Some(words),
                _ => None,
            })
            .flatten()
            .copied()
            .collect();
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
        out.push(json!({ "imem": imem, "desc": desc, "run_pc": run_pc }));
    }
    Ok(Value::Array(out))
}

/// An L2 example (or text) lowered to L1 text with no passes: whether each
/// pattern is found, whether their first matches come in order, the
/// captures of `keep`, and the lines matching `absent` between the first
/// two patterns' matches (ISA.VC.024, .026).
fn lower_text(inp: &Value, m: &Machine) -> Result<Value, String> {
    let name = inp["name"].as_str().ok_or("no name")?;
    let text = match inp["text"].as_str() {
        Some(t) => t.to_string(),
        None => example(inp["example"].as_str().ok_or("no text or example")?)?,
    };
    let strategy = match inp["strategy"].as_str() {
        Some(s) => Strategy::parse(s).ok_or_else(|| format!("no strategy {s}"))?,
        None => Strategy::Auto,
    };
    let t = Target::of(m);
    let p = Program::parse(&text, "<isa.lower>", Some(name))
        .map_err(|e| e.to_string())?
        .with_vregs(t.vregs, t.vcopies);
    let l1 = ktpu_l2::lower::lower(&p, &t, &Search::new(strategy)).map_err(|e| e.to_string())?;
    let lines: Vec<&str> = l1.lines().map(str::trim).collect();
    let pats = inp["patterns"]
        .as_array()
        .map(|a| a.iter().filter_map(Value::as_str).collect::<Vec<_>>())
        .unwrap_or_default()
        .into_iter()
        .map(Regex::new)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    let first: Vec<i64> = pats
        .iter()
        .map(|p| {
            lines
                .iter()
                .position(|l| p.is_match(l))
                .map_or(-1, |i| i as i64)
        })
        .collect();
    let mut out = Map::new();
    out.insert(
        "found".into(),
        json!(first.iter().map(|i| *i >= 0).collect::<Vec<_>>()),
    );
    if first.len() > 1 {
        let ordered = first.windows(2).all(|w| 0 <= w[0] && w[0] < w[1]);
        out.insert("in_order".into(), json!(ordered));
    }
    if let (Some(absent), true) = (inp["absent"].as_str(), first.len() > 1) {
        if first.iter().all(|i| *i >= 0) {
            let bad = Regex::new(absent).map_err(|e| e.to_string())?;
            let between = lines[first[0] as usize..first[1] as usize]
                .iter()
                .filter(|l| bad.is_match(l))
                .count();
            out.insert("between".into(), json!(between));
        }
    }
    if let Some(keep) = inp["keep"].as_str() {
        let keep = Regex::new(keep).map_err(|e| e.to_string())?;
        let kept: Vec<Vec<Value>> = lines
            .iter()
            .filter_map(|l| keep.captures(l))
            .map(|c| {
                c.iter()
                    .skip(1)
                    .map(|g| g.map_or(Value::Null, |g| json!(g.as_str())))
                    .collect()
            })
            .collect();
        out.insert("kept".into(), json!(kept));
    }
    Ok(Value::Object(out))
}
