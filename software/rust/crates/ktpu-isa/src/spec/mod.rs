//! Rust ports of the tier-0 ISA runners (`software/tests/spec/runners/isa.py`).
//!
//! A port takes a vector's `input` (the Python call it describes: function or
//! op name, positional `args`, keyword `kw`) and the [`Knobs`] built from its
//! machine, and returns what the runner returns. A refusal is
//! `{"refused": <the Python exception name>}`. Under a mutant
//! ([`crate::mutant`], the `mutants` feature) the whole run sees it.

pub mod num;

use crate::layout::W256;
use crate::mutant::on;
use crate::unit::cluster::ClusterInst;
use crate::unit::mover::{MoveMode, MoverOp};
use crate::unit::sru::config::{self, lookup, Job, Pool, PoolOp};
use crate::unit::vector::core::{e8m15, VWord};
use crate::unit::vector::{dim, VecCu};
use crate::{Isa, Machine, Refusal};
use serde_json::{json, Map, Value};

/// Every mutant ktpu-isa carries.
pub fn mutants() -> Vec<&'static str> {
    crate::mutant::NAMES
        .iter()
        .chain(ktpu_machine::mutant::NAMES)
        .copied()
        .collect()
}

/// Mutants a ported vector names whose mutated point is another crate's,
/// and that crate.
pub fn skipped_mutants() -> Vec<(&'static str, &'static str)> {
    vec![(
        "isa.vector/links",
        "the L2 lowering's landing pool (ktpu-l2)",
    )]
}

/// What a port runs against: the machine and the encoders built from it.
#[derive(Clone, Debug)]
pub struct Knobs {
    pub machine: Machine,
    pub isa: Isa,
}

impl Knobs {
    pub fn new(machine: &Machine) -> Result<Knobs, String> {
        Ok(Knobs {
            isa: Isa::new(machine).map_err(|r| r.to_string())?,
            machine: machine.clone(),
        })
    }
}

/// A runner port: `(input, knobs) -> output or why it cannot run`.
type KPort = fn(&Value, Option<&Knobs>) -> Result<Value, String>;

const PORTS: &[(&str, KPort)] = &[
    ("isa.cluster.word", cluster_word),
    ("isa.vector.word", vector_word),
    ("isa.vector.decode", vector_decode),
    ("isa.vector.cu", vector_cu),
    ("isa.vector.e8m15", vector_e8m15),
    ("isa.vector.params", vector_params),
    ("isa.mover.move", mover_move),
    ("isa.sru.word", sru_word),
    ("isa.sru.words", sru_words),
    ("isa.sru.schedule", sru_schedule),
    ("isa.sru.map", sru_map),
    ("isa.sru.defaults", sru_defaults),
    ("isa.vector.machine_of", vector_machine_of),
];

/// Every ported runner's name.
pub fn runners() -> Vec<&'static str> {
    PORTS.iter().chain(num::PORTS).map(|(n, _)| *n).collect()
}

/// Run runner `runner` on `input` against `machine`, under `mutant` when
/// given (one of [`mutants`]).
pub fn run(
    runner: &str,
    input: &Value,
    machine: Option<&Machine>,
    mutant: Option<&str>,
) -> Result<Value, String> {
    #[cfg(feature = "mutants")]
    if let Some(name) = mutant.and_then(|mu| {
        ktpu_machine::mutant::NAMES
            .iter()
            .copied()
            .find(|n| *n == mu)
    }) {
        return ktpu_machine::mutant::with(name, || run(runner, input, machine, None));
    }
    let port = PORTS
        .iter()
        .chain(num::PORTS)
        .find(|(n, _)| *n == runner)
        .map(|(_, p)| *p)
        .ok_or_else(|| format!("runner {runner} has no Rust port"))?;
    let go = || {
        let knobs = match machine {
            Some(m) => Some(Knobs::new(m)?),
            None => None,
        };
        port(input, knobs.as_ref())
    };
    let Some(name) = mutant else {
        return go();
    };
    let Some(name) = crate::mutant::NAMES.iter().copied().find(|n| *n == name) else {
        return Err(match skipped_mutants().iter().find(|(n, _)| *n == name) {
            Some((_, owner)) => format!("mutant {name} mutates {owner}"),
            None => format!("unknown mutant {name}"),
        });
    };
    #[cfg(feature = "mutants")]
    {
        crate::mutant::with(name, go)
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = (name, go);
        Err("ktpu-isa is built without the `mutants` feature".into())
    }
}

fn hx(v: u64) -> Value {
    Value::from(format!("{v:#x}"))
}

/// The exception the Python stack raises for `r`.
fn exception(r: &Refusal) -> &'static str {
    let c = r.clause.as_str();
    match c {
        _ if c.starts_with("ISA.MV.") || c == "RUL.049" || c == "RUL.050" => "PackageError",
        _ if c.starts_with("ISA.VC.") => "ISAError",
        "ISA.MG.002" | "ISA.MG.004" | "ISA.MG.005" | "ISA.MG.094" => "ISAError",
        "ISA.SR.010" | "ISA.SR.011" | "ISA.SR.012" | "ISA.SR.013" | "ISA.SR.014" => "ISAError",
        "ISA.SR.022" => "KeyError",
        _ if r.message.starts_with("no ") => "KeyError",
        _ => "ValueError",
    }
}

fn refused(r: Refusal) -> Value {
    json!({ "refused": exception(&r) })
}

fn need(k: Option<&Knobs>) -> Result<&Knobs, String> {
    k.ok_or_else(|| "the runner needs a machine".to_string())
}

/// A Python call's arguments by parameter name: `args` positionally over
/// `params`, then `kw`.
fn bind(input: &Value, params: &[&str]) -> Result<Map<String, Value>, String> {
    let mut out = Map::new();
    let args = input
        .get("args")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if args.len() > params.len() {
        return Err(format!(
            "{} positional arguments for {params:?}",
            args.len()
        ));
    }
    for (p, a) in params.iter().zip(args) {
        out.insert(p.to_string(), a);
    }
    if let Some(kw) = input.get("kw").and_then(Value::as_object) {
        for (k, v) in kw {
            if !params.contains(&k.as_str()) {
                return Err(format!("no parameter {k} in {params:?}"));
            }
            out.insert(k.clone(), v.clone());
        }
    }
    Ok(out)
}

/// `fields` given as Python ints (0 / 1) to bools.
fn bools(map: &mut Map<String, Value>, fields: &[&str]) {
    for f in fields {
        if let Some(v) = map.get_mut(*f) {
            if let Some(i) = v.as_i64() {
                *v = Value::from(i != 0);
            }
        }
    }
}

fn typed<T: serde::de::DeserializeOwned>(
    variant: &str,
    fields: Map<String, Value>,
) -> Result<T, String> {
    serde_json::from_value(json!({ variant: fields })).map_err(|e| format!("{variant}: {e}"))
}

// ---------------------------------------------------------------- cluster
fn cluster_word(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let isa = &need(k)?.isa;
    let op = input
        .get("op")
        .and_then(Value::as_str)
        .ok_or("only `op` calls are ported")?;
    let (params, flags): (&[&str], &[&str]) = match op {
        "Fill" => (
            &["addr", "n", "sel", "eoff", "fbank", "peers", "recv"],
            &["recv"],
        ),
        "Gemm" => (
            &[
                "gm", "gn", "nk", "acc", "aoff", "boff", "abank", "bbank", "emit", "addr",
                "anchor", "to",
            ],
            &["acc", "emit"],
        ),
        "Drain" => (
            &[
                "addr", "n", "fuse", "dst", "dflags", "ack", "buf", "mesh", "fin", "credit",
            ],
            &["fuse", "credit"],
        ),
        "Post" => (&["dst", "n"], &[]),
        other => return Err(format!("no cluster op {other}")),
    };
    let mut fields = bind(input, params)?;
    bools(&mut fields, flags);
    let inst: ClusterInst = typed(op, fields)?;
    Ok(match inst.encode(&isa.cluster) {
        Ok(w) => json!({ "payload": w.to_hex() }),
        Err(r) => refused(r),
    })
}

// ----------------------------------------------------------------- vector
fn src_name(v: &Value) -> Result<Value, String> {
    match v.as_i64() {
        Some(0) => Ok("v".into()),
        Some(1) => Ok("s".into()),
        Some(3) => Ok("k".into()),
        _ => Err(format!("operand selector {v} has no Rust form")),
    }
}

fn walk_mode(v: &Value) -> Result<Value, String> {
    ["f16", "gt4", "f32", "mx7"]
        .get(v.as_u64().ok_or("mode")? as usize)
        .map(|s| Value::from(*s))
        .ok_or_else(|| format!("walk mode {v}"))
}

/// One `encode.vector.<fn>` call as a typed word, and the register count it
/// names (`regs=`).
fn vword(call: &Value) -> Result<(VWord, Option<u32>), String> {
    let f = call.get("fn").and_then(Value::as_str).ok_or("no fn")?;
    let (variant, params, flags): (&str, &[&str], &[&str]) = match f {
        "math" => (
            "Math",
            &["op", "vd", "va", "vb", "vc", "sa", "sb", "sc", "om", "regs"],
            &["om"],
        ),
        "vcfg" => ("Vcfg", &["sel", "payload"], &[]),
        "chk" => ("Chk", &["a", "b", "c", "d"], &[]),
        "chkvl" => ("Chkvl", &["vl", "a", "b", "c", "d"], &[]),
        "xb" => ("Xb", &["xm", "xk", "sh", "xc", "pm", "pr"], &["xc"]),
        "cfg_ptr" => ("Ptr", &["reg", "value"], &[]),
        "cfg_stride" => ("Stride", &["reg", "s0", "s1"], &[]),
        "ainc" => ("Ainc", &["ad", "inc"], &[]),
        "getstk" => ("Getstk", &["sreg"], &[]),
        "vunpk" => (
            "Unpk",
            &["vd", "off", "n", "mode", "cb", "rel", "q", "regs"],
            &["rel"],
        ),
        "vpack" => (
            "Pack",
            &["vs", "off", "n", "mode", "cb", "rel", "q", "regs"],
            &["rel"],
        ),
        "vpack_mx7" => (
            "PackMx7",
            &["vs", "off", "b_layout", "rel", "q", "regs"],
            &["b_layout", "rel"],
        ),
        "vsync" => ("Sync", &["waiter", "on", "slack", "mark", "q"], &[]),
        "vwait" => ("Wait", &["waiter", "counter", "n", "q"], &[]),
        "vpost" => ("Post", &["dst", "n"], &[]),
        "vmark" => ("Mark", &["mark", "on", "slack"], &[]),
        "vskipz" => ("Skipz", &["sreg", "n"], &[]),
        "vsetvl" => ("Setvl", &["sreg"], &[]),
        "vseti" => ("Seti", &["sreg", "to_k"], &["to_k"]),
        "vloop" => ("Loop", &["sreg", "body"], &[]),
        "vbar" => ("Bar", &["q"], &[]),
        "vhalt" => return Ok((VWord::Halt, None)),
        "vfill" => ("Fill", &["ad", "l1off", "rel"], &["rel"]),
        "vdrain" => (
            "Drain",
            &["ad", "l1off", "rel", "node", "buf_id", "signal"],
            &["rel", "signal"],
        ),
        other => return Err(format!("no vector word fn {other}")),
    };
    let mut fields = bind(call, params)?;
    bools(&mut fields, flags);
    let regs = fields
        .remove("regs")
        .and_then(|v| v.as_u64())
        .map(|v| v as u32);
    if variant == "Math" {
        if let Some(code) = fields.get("op").and_then(Value::as_u64) {
            let op = crate::unit::vector::MathOp::from_code(code).ok_or("math opcode")?;
            fields.insert("op".into(), op.mnemonic().into());
        }
        for s in ["sa", "sb", "sc"] {
            if let Some(v) = fields.get(s).cloned() {
                fields.insert(s.into(), src_name(&v)?);
            }
        }
    }
    if matches!(variant, "Unpk" | "Pack") {
        if let Some(v) = fields.get("mode").cloned() {
            fields.insert("mode".into(), walk_mode(&v)?);
        }
    }
    if matches!(variant, "Ptr" | "Stride") {
        let sel = fields
            .get("reg")
            .and_then(Value::as_u64)
            .ok_or("selector")?;
        let name = match sel {
            2 => "uptr",
            3 => "uinc",
            5 => "pptr",
            6 => "pinc",
            4 => "ustr",
            7 => "pstr",
            _ => return Err(format!("VCFG selector {sel} is not a pointer or stride")),
        };
        fields.insert("reg".into(), name.into());
    }
    Ok((typed(variant, fields)?, regs))
}

fn vector_word(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let base = &need(k)?.isa.vector;
    let calls: Vec<Value> = match input.get("calls").and_then(Value::as_array) {
        Some(c) => c.clone(),
        None => vec![input.clone()],
    };
    let mut words = Vec::new();
    for call in &calls {
        let (word, regs) = vword(call)?;
        let mut v = base.clone();
        if let Some(regs) = regs {
            v.cfg.regs = regs;
        }
        match word.encode(&v) {
            Ok(w) => words.push(hx(w)),
            Err(r) => return Ok(refused(r)),
        }
    }
    Ok(if input.get("calls").is_some() {
        json!({ "words": words })
    } else {
        json!({ "word": words[0] })
    })
}

fn parse_hex(v: &Value) -> Result<u64, String> {
    let s = v.as_str().ok_or("a hex string")?;
    u64::from_str_radix(s.trim_start_matches("0x"), 16).map_err(|e| format!("{s}: {e}"))
}

fn vector_decode(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let v = &need(k)?.isa.vector;
    let decode = |w: u64| v.core.set.decode(W256::from_u64(w));
    if let Some(words) = input.get("words").and_then(Value::as_array) {
        let mut out = Vec::new();
        for w in words {
            match decode(parse_hex(w)?) {
                Ok((name, _)) => out.push(Value::from(name)),
                Err(r) => return Ok(refused(r)),
            }
        }
        return Ok(json!({ "formats": out }));
    }
    match decode(parse_hex(&input["word"])?) {
        Ok((name, fields)) => {
            let f: Map<String, Value> = fields
                .into_iter()
                .map(|(k, v)| (k.to_string(), v.into()))
                .collect();
            Ok(json!({ "format": name, "fields": f }))
        }
        Err(r) => Ok(refused(r)),
    }
}

fn vector_cu(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let v = &need(k)?.isa.vector;
    let f = input.get("fn").and_then(Value::as_str).ok_or("no fn")?;
    let args = input
        .get("args")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let int = |i: usize| -> Result<i64, String> {
        args.get(i)
            .and_then(Value::as_i64)
            .ok_or_else(|| format!("{f}: argument {i}"))
    };
    let cu = match f {
        "imem" | "imem_flit" => VecCu::Imem {
            addr: int(0)? as u32,
            word: int(1)? as u64,
        },
        "desc" | "desc_flit" | "desc_base" => {
            let (ad, fld, value) = match f {
                "desc_base" => (int(0)?, 0, int(1)?),
                _ => (int(0)?, int(1)?, int(2)?),
            };
            if f == "desc" && fld == 0 {
                return Err("desc to field 0 takes the low bits only; no Rust form".into());
            }
            VecCu::Desc {
                ad: ad as u32,
                fld: fld as u32,
                value: value as u64,
            }
        }
        "run" | "run_flit" => VecCu::Run {
            pc: if args.is_empty() { 0 } else { int(0)? as u32 },
        },
        "dim" => {
            return Ok(match dim(int(0)?, int(1)? as u32) {
                Ok(d) => json!({ "payload": hx(d) }),
                Err(r) => refused(r),
            })
        }
        other => return Err(format!("no CU fn {other}")),
    };
    Ok(match cu.encode(v) {
        Ok(p) => json!({ "payload": p.to_hex() }),
        Err(r) => refused(r),
    })
}

fn vector_e8m15(input: &Value, _k: Option<&Knobs>) -> Result<Value, String> {
    let x = input.get("x").and_then(Value::as_f64).ok_or("no x")?;
    Ok(json!({ "e8m15": hx(e8m15(x) as u64) }))
}

/// The stack's value of a named parameter; `m:PATH` reads the machine.
fn vector_params(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let k = need(k)?;
    let mut out = Map::new();
    for name in input
        .get("names")
        .and_then(Value::as_array)
        .ok_or("no names")?
    {
        let name = name.as_str().ok_or("a name")?;
        let v = if let Some(path) = name.strip_prefix("m:") {
            fact_read(
                k.machine
                    .get(path)
                    .cloned()
                    .ok_or_else(|| format!("machine has no {path}"))?,
            )
        } else {
            mutated(name, param(name, k)?)
        };
        out.insert(name.to_string(), v);
    }
    Ok(Value::Object(out))
}

/// A machine fact as read, or off by one under `machine/fact_read`
/// (numbers +1, flags flipped, lists one longer).
fn fact_read(v: Value) -> Value {
    if !on("machine/fact_read") {
        return v;
    }
    match v {
        Value::Bool(b) => Value::Bool(!b),
        Value::Number(n) => match n.as_i64() {
            Some(i) => (i + 1).into(),
            None => (n.as_f64().unwrap_or(0.0) + 1.0).into(),
        },
        Value::Array(mut a) => {
            a.push(a.last().cloned().unwrap_or(0.into()));
            Value::Array(a)
        }
        other => other,
    }
}

/// Parameter `name`'s value `v` as the active mutant breaks it (the
/// parameters the Python mutants patch in place).
fn mutated(name: &str, mut v: Value) -> Value {
    let mut put = |key: &str, x: Value| {
        if let Some(slot) = v.get_mut(key) {
            *slot = x;
        }
    };
    if on("isa.vector/model_queues") {
        match name {
            "queue" => {
                put("VBAR", "M".into());
                put("VPOST", "U".into());
            }
            "depth" => {
                put("U", 2.into());
                put("F", 8.into());
            }
            "kind" => {
                put("1", "P".into());
                put("3", "U".into());
                put("5", "P".into());
            }
            "waiter" => {
                put("1", "P".into());
                put("3", "U".into());
            }
            _ => {}
        }
    }
    if on("isa.cluster/limits") && name == "mg.buffers" {
        put("2", 4096.into());
    }
    if name == "mg.isa" {
        if on("isa.cluster/split") {
            put("addr_bits", 32.into());
            put("addr_hi_bits", 8.into());
        }
        if on("isa.cluster/cfg") {
            put("anchor", 41.into());
        }
    }
    match name {
        "l1_words" if on("isa.vector/consts") => 256.into(),
        "peer_words" if on("isa.vector/consts") => 128.into(),
        n if n.starts_with("core.") && on("isa.vector/consts") => json!([32, 4]),
        "mg.bank" if on("isa.cluster/plan") => 32.into(),
        "mg.tiles" if on("isa.cluster/plan") => 2048.into(),
        "mg.l2_bank" if on("isa.cluster/bank") => 512.into(),
        "mg.burst" if on("isa.cluster/limits") => 16.into(),
        "mg.dq_room" if on("isa.cluster/limits") => 4096.into(),
        _ => v,
    }
}

fn param(name: &str, k: &Knobs) -> Result<Value, String> {
    let (m, isa) = (&k.machine, &k.isa);
    let c = &isa.cluster.cfg;
    let v = &isa.vector.cfg;
    let mv = &isa.mover;
    Ok(match name {
        "lanes" | "isa.lanes" => v.lanes.into(),
        "chunks" => v.chunks.into(),
        "vlmax" | "isa.vlmax" => v.vlmax.into(),
        "regs" => v.regs.into(),
        "isa.word_bytes" => v.word_bytes.into(),
        "mover.word_bytes" => mv.word_bytes.into(),
        "imem_words" => v.imem_words().into(),
        "run_reach" => (1u32 << v.addr_bits).into(),
        "faults" => crate::unit::vector::core::fault_codes(m).into(),
        "mover.move_words" => mv.move_words.into(),
        "mover.ndim" => mv.levels.into(),
        "mover.max_index" => mv.max_index.into(),
        "mover.addr_bits" => mv.addr_bits.into(),
        "mover.modes" => {
            let modes = [
                MoveMode::Copy,
                MoveMode::Gather,
                MoveMode::Generate,
                MoveMode::Fill,
                MoveMode::Xform,
                MoveMode::Scatter,
            ];
            let mut out = Map::new();
            for mode in modes {
                let key = serde_json::to_value(mode).map_err(|e| e.to_string())?;
                out.insert(
                    key.as_str().unwrap_or_default().to_string(),
                    mv.mode_code(mode).into(),
                );
            }
            Value::Object(out)
        }
        "mg.isa" => json!({
            "payload_bits": c.payload_bits,
            "op_bits": 4,
            "addr_bits": crate::unit::cluster::ADDR_LO_BITS,
            "addr_hi_bits": c.addr_hi_bits,
            "n_bits": 16,
            "grid_bits": 8,
            "pos_bits": c.pos_bits,
            "peer_bits": c.coord_bits() * c.max_peers,
            "anchor": crate::unit::cluster::ANCHOR,
            "kblock": m.mg.kblock,
            "lanes": m.mg.lanes,
            "mem_word_bytes": m.die.word_bytes,
            "max_fill": c.max_fill,
            "max_peers": c.max_peers,
            "kstep": c.kstep,
        }),
        "mg.bank" | "mg.l2_bank" => m.mg.l1_bank_entries.into(),
        "mg.tiles" => m.mg.tiles.into(),
        "mg.burst" => m.mg.wburst.into(),
        // ISA.MG.027: a sweep is held while more than dq_depth less two
        // drain bursts are outstanding.
        "mg.dq_room" => (m.mg.dq_depth - 2 * m.mg.wburst).into(),
        "mg.buffers" => {
            let wb = m.die.word_bytes;
            let entry = m.mg.lanes * m.mg.kblock / wb;
            let subtile = m.mg.lanes * m.mg.lanes * 4 / wb;
            json!({
                "0": m.mg.l1_a_entries * entry,
                "1": m.mg.l1_b_entries * entry,
                "2": m.mg.tiles * subtile,
            })
        }
        "l1_words" => m.vc.l1_words.into(),
        "peer_words" => m.vc.walk_max.into(),
        "depth" => json!({
            "F": m.vc.dma_queue_depth,
            "D": m.vc.dma_queue_depth,
            "U": m.vc.queue_depth,
            "M": m.vc.queue_depth,
            "P": m.vc.queue_depth,
        }),
        "kind" => {
            let (u1, p1) = if m.vc.dualq == 0 {
                ("U", "P")
            } else {
                ("U1", "P1")
            };
            json!({"0": "F", "1": "U", "2": "M", "3": "P", "4": "D", "5": u1, "6": p1})
        }
        "waiter" => json!({"1": "U", "2": "M", "3": "P", "4": "F", "6": "D"}),
        "queue" => json!({
            "VUNPK": "U", "VPACK": "P", "VFILL": "F", "VDRAIN": "D", "VPOST": "D", "VBAR": "U",
        }),
        other if other.starts_with("core.") => {
            let build = &other["core.".len()..];
            if m.vc.build != build {
                return Err(format!("the machine's core is {}, not {build}", m.vc.build));
            }
            json!([m.vc.regs, m.vc.rf_copies])
        }
        "pool" => {
            return Err(
                "parameter pool has no ktpu-isa port: the L2 lowering's landing pool (ktpu-l2)"
                    .into(),
            )
        }
        other => return Err(format!("parameter {other} has no ktpu-isa port")),
    })
}

// ------------------------------------------------------------------ mover
fn move_mode(m: &crate::MoverIsa, code: &Value) -> Result<Option<MoveMode>, String> {
    let code = code.as_u64().ok_or("mode")?;
    Ok(m.mode(code))
}

fn mover_move(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let isa = &need(k)?.isa;
    let (variant, params, flags): (&str, &[&str], &[&str]) =
        if let Some(op) = input.get("op").and_then(Value::as_str) {
            match op {
                "Quantise" => ("Quantise", &["src", "dst", "entries", "b"], &["b"]),
                "Transpose4" => ("Transpose4", &["src", "dst", "groups"], &[]),
                "Copy" => ("Copy", &["src", "dst", "nbytes"], &[]),
                other => return Err(format!("no mover op {other}")),
            }
        } else {
            match input.get("fn").and_then(Value::as_str).ok_or("no fn")? {
                "copy" => ("Copy", &["src", "dst", "nbytes"], &[]),
                "convert" => (
                    "Convert",
                    &[
                        "src",
                        "dst",
                        "entries",
                        "xform_id",
                        "mode",
                        "in_words",
                        "out_words",
                    ],
                    &[],
                ),
                "convert_walk" => ("ConvertWalk", &["src", "dst", "xform_id", "mode"], &[]),
                "fill" => ("Fill", &["dst", "value", "ewidth"], &[]),
                "generate" => ("Generate", &["dst", "seed", "ewidth"], &[]),
                "gather" => (
                    "Gather",
                    &["src_base", "index", "dst", "row", "fp"],
                    &["fp"],
                ),
                "scatter" => (
                    "Scatter",
                    &["src", "dst_base", "index", "row", "fp"],
                    &["fp"],
                ),
                "move" => (
                    "Move",
                    &[
                        "mode", "src", "dst", "ewidth", "flags", "xform", "imm", "seed", "index",
                        "row", "fp",
                    ],
                    &["fp"],
                ),
                "flag_byte" => {
                    let f = bind(input, &["wcoal", "burst"])?;
                    let wcoal = f
                        .get("wcoal")
                        .is_none_or(|v| v.as_bool().unwrap_or(v.as_i64() != Some(0)));
                    let burst = f.get("burst").and_then(Value::as_i64).unwrap_or(0);
                    return Ok(match isa.mover.flag_byte(wcoal, burst) {
                        Ok(v) => json!({ "value": hx(v as u64) }),
                        Err(r) => refused(r),
                    });
                }
                other => return Err(format!("no mover fn {other}")),
            }
        };
    let mut fields = bind(input, params)?;
    bools(&mut fields, flags);
    let op: MoverOp = if variant == "Move" {
        let code = fields.get("mode").cloned().ok_or("move without mode")?;
        let Some(mode) = move_mode(&isa.mover, &code)? else {
            return Ok(json!({ "refused": "PackageError" }));
        };
        fields.insert(
            "mode".into(),
            serde_json::to_value(mode).map_err(|e| e.to_string())?,
        );
        fields.retain(|_, v| !v.is_null());
        MoverOp::Move(
            serde_json::from_value(Value::Object(fields)).map_err(|e| format!("Move: {e}"))?,
        )
    } else {
        typed(variant, fields)?
    };
    Ok(match isa.mover.writes(&op) {
        Ok(ws) => {
            json!({ "writes": ws.iter().map(|(r, v)| json!([hx(*r as u64), hx(*v)])).collect::<Vec<_>>() })
        }
        Err(r) => refused(r),
    })
}

// -------------------------------------------------------------------- SRU
fn sru_word(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let isa = &need(k)?.isa;
    let fmt = match input.get("fmt").and_then(Value::as_str).ok_or("no fmt")? {
        "CFG" => &isa.sru.cfg,
        "LOAD" => &isa.sru.load,
        "RUN" => &isa.sru.run,
        "STORE" => &isa.sru.store,
        "POST" => &isa.sru.post,
        other => return Err(format!("no SRU format {other}")),
    };
    let kw = input
        .get("kw")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut values = Vec::new();
    for (k, v) in &kw {
        let v = v
            .as_i64()
            .or_else(|| v.as_bool().map(i64::from))
            .ok_or_else(|| format!("{k} takes an int"))?;
        values.push((k.as_str(), v));
    }
    Ok(match fmt.encode(&values) {
        Ok(w) => json!({ "word": w.to_hex() }),
        Err(r) => refused(r),
    })
}

fn pool(input: &Value, k: Option<&Knobs>) -> Result<Pool, String> {
    let mut pool = Pool::of(&need(k)?.machine).map_err(|r| r.to_string())?;
    if let Some(f) = input.get("f_lanes").and_then(Value::as_u64) {
        pool.f_lanes = f as u32;
    }
    if let Some(h) = input.get("h_taps").and_then(Value::as_u64) {
        pool.h_taps = h as u32;
    }
    Ok(pool)
}

/// Whether the stack holds an SRU profile of its own (RUL.014): a pool built
/// from the machine with its `sr.profile` removed. The Python names are the
/// answer's keys; both are the one Rust fact.
fn sru_defaults(_: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let bare = need(k)?
        .machine
        .with_fact("sr.profile", Value::Null)
        .map_err(|e| e.to_string())?;
    let own = Pool::of(&bare).is_ok();
    Ok(json!({ "target.SRU": own, "emit.PROFILE": own }))
}

/// The vector core the stack builds for board `board` (RUL.001): its
/// machine file's register count and read copies, as the encoders take them.
fn vector_machine_of(input: &Value, _: Option<&Knobs>) -> Result<Value, String> {
    let board = input["board"].as_str().ok_or("no board")?;
    let m = Machine::named(board).map_err(|e| e.to_string())?;
    let cfg = &Isa::new(&m).map_err(|r| r.to_string())?.vector.cfg;
    Ok(json!({ "regs": cfg.regs, "copies": m.vc.rf_copies }))
}

fn sru_words(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let pool = pool(input, k)?;
    let mut job = Map::new();
    for key in ["g", "passes", "ops", "fin"] {
        if let Some(v) = input.get(key) {
            job.insert(key.into(), v.clone());
        }
    }
    let job: Job = serde_json::from_value(Value::Object(job)).map_err(|e| format!("job: {e}"))?;
    Ok(match config::words(&job, &pool) {
        Ok(w) => Value::Object(
            w.into_iter()
                .map(|(a, v)| (format!("{a:#x}"), hx(v as u64)))
                .collect(),
        ),
        Err(r) => refused(r),
    })
}

fn sru_schedule(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let pool = pool(input, k)?;
    let prog: Vec<PoolOp> = serde_json::from_value(input.get("prog").cloned().ok_or("no prog")?)
        .map_err(|e| format!("prog: {e}"))?;
    Ok(match config::schedule(&prog, &pool) {
        Ok(s) => json!({ "fields": s.fields, "ii": s.ii, "sigma": s.sigma }),
        Err(r) => refused(r),
    })
}

fn table<T: Copy + Into<Value>>(t: &[(&str, T)]) -> Value {
    Value::Object(
        t.iter()
            .map(|(k, v)| (k.to_string(), (*v).into()))
            .collect(),
    )
}

fn sru_map(input: &Value, k: Option<&Knobs>) -> Result<Value, String> {
    let sr = k.and_then(|k| k.machine.sr_facts());
    let fact = |f: fn(&ktpu_machine::SrFacts) -> u32| -> Result<Value, String> {
        sr.as_ref()
            .map(|s| f(s).into())
            .ok_or_else(|| "the machine has no SR units".to_string())
    };
    let map = config::map();
    let mut out = Map::new();
    for key in input
        .get("names")
        .and_then(Value::as_array)
        .ok_or("no names")?
    {
        let key = key.as_str().ok_or("a name")?;
        let (tab, name) = key.split_once('.').unwrap_or((key, ""));
        let slot = |t: &[(&str, config::Slot)]| {
            lookup(t, name)
                .map(|(a, b, c)| json!([a, b, c]))
                .ok_or_else(|| format!("no field {key}"))
        };
        let v = match tab {
            "G" => slot(&map.g)?,
            "P" => slot(&map.p)?,
            "O" => slot(&map.o)?,
            "F" => lookup(&map.f, name)
                .map(|(a, b)| json!([a, b]))
                .ok_or_else(|| format!("no field {key}"))?,
            "PASS_BASE" => map.pass_base.into(),
            "PASS_STRIDE" => map.pass_stride.into(),
            "OP_BASE" => map.op_base.into(),
            "OP_STRIDE" => map.op_stride.into(),
            "FIN_BASE" => map.fin_base.into(),
            "ADDR_BITS" => config::ADDR_BITS.into(),
            "NPASS" => fact(|s| s.npass)?,
            "OP_DEPTH" => fact(|s| s.op_depth)?,
            "FIN_DEPTH" => fact(|s| s.fin_depth)?,
            "LDEP" => fact(|s| s.ldep)?,
            "WLEAD" => fact(|s| s.wlead)?,
            "TAPS" => fact(|s| s.taps)?,
            "ASRC" => table(&map.asrc),
            "BSRC" => table(&map.bsrc),
            "FOP" => table(&map.fop),
            "SRC" => table(&map.src),
            other => return Err(format!("no SRU map entry {other}")),
        };
        out.insert(key.to_string(), v);
    }
    Ok(Value::Object(out))
}
