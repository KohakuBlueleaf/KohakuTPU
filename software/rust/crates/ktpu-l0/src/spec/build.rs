//! The `l0.pkg.build` runner: `PackageBuilder` calls by their Python
//! signatures (positional arguments, then keywords, then defaults).

use serde_json::{json, Map, Value};

use super::json::{arr, coord, describe, flag, hx, lines, pairs, refused, u, u_or, word, words};
use super::Port;
use crate::build::{AddrField, Addresses, Increment};
use crate::error::Refusal;
use crate::format::{Alu, Cond, Kind, Op, Step, ANY_UNIT};
use crate::word::Word;
use crate::PackageBuilder;

/// A parameter and its default (JSON text), none when required.
type Param = (&'static str, Option<&'static str>);

/// `PackageBuilder` method -> its parameters.
const SIGNATURES: &[(&str, &[Param])] = &[
    (
        "unit",
        &[
            ("coord", None),
            ("type_name", Some("null")),
            ("mesh", Some("null")),
        ],
    ),
    (
        "buffer",
        &[
            ("name", None),
            ("size", None),
            ("default", Some("0")),
            ("kind", Some("3")),
        ],
    ),
    ("spans_from", &[("live", None)]),
    (
        "payload",
        &[
            ("word", None),
            ("addresses", Some("null")),
            ("unit_type", Some("\"\"")),
        ],
    ),
    (
        "dispatch",
        &[("unit", None), ("words", None), ("addresses", Some("null"))],
    ),
    (
        "repeat",
        &[
            ("unit", None),
            ("template", None),
            ("repeats", None),
            ("increments", None),
            ("addresses", Some("null")),
        ],
    ),
    (
        "dispatch_periodic",
        &[
            ("unit", None),
            ("words", None),
            ("fields", None),
            ("addresses", Some("null")),
        ],
    ),
    ("await_", &[("unit", None), ("count", None)]),
    ("barrier", &[]),
    ("end", &[]),
    (
        "step",
        &[
            ("op", None),
            ("unit", Some("0")),
            ("count", Some("0")),
            ("arg", Some("0")),
            ("flags", Some("0")),
        ],
    ),
    ("signal", &[("value32", Some("0")), ("value64", Some("0"))]),
    ("settle", &[("cycles", None)]),
    (
        "ring",
        &[
            ("mesh", None),
            ("slot", Some("0")),
            ("amount", Some("1")),
            ("fence", Some("true")),
        ],
    ),
    ("wait_bell", &[("mesh", None), ("count", Some("1"))]),
    (
        "wait_slot",
        &[("slot", None), ("count", None), ("consume", Some("false"))],
    ),
    (
        "store",
        &[
            ("pairs", None),
            ("from_regs", Some("false")),
            ("relocate", Some("false")),
        ],
    ),
    ("ctrl", &[("pairs", None), ("from_regs", Some("false"))]),
    (
        "load",
        &[
            ("reg", None),
            ("address", None),
            ("index", Some("null")),
            ("ctrl", Some("false")),
            ("relocate", Some("false")),
        ],
    ),
    (
        "alu",
        &[
            ("rd", None),
            ("op", None),
            ("ra", Some("0")),
            ("b", Some("0")),
            ("imm", Some("true")),
        ],
    ),
    (
        "jump",
        &[
            ("target", None),
            ("cond", Some("0")),
            ("ra", Some("0")),
            ("b", Some("0")),
            ("imm", Some("true")),
        ],
    ),
    ("repoint", &[("start", None), ("index", None)]),
    ("retarget", &[("step", None), ("target", None)]),
    (
        "patch",
        &[
            ("reg", None),
            ("index", None),
            ("bit", None),
            ("width", None),
            ("shift", Some("0")),
            ("steps", Some("false")),
            ("add", Some("false")),
        ],
    ),
    (
        "send",
        &[
            ("unit", None),
            ("flit_type", None),
            ("words", None),
            ("ack", Some("false")),
        ],
    ),
    (
        "push",
        &[
            ("unit", None),
            ("descriptor", None),
            ("address", None),
            ("words", None),
            ("ack", Some("false")),
            ("relocate", Some("false")),
        ],
    ),
    (
        "recv",
        &[
            ("address", None),
            ("count", None),
            ("unit", Some("65535")),
            ("relocate", Some("false")),
        ],
    ),
    (
        "mover",
        &[
            ("writes", None),
            ("addresses", Some("null")),
            ("posted", Some("false")),
        ],
    ),
    (
        "fetch_from",
        &[("unit", None), ("addr", None), ("count", None)],
    ),
    ("mwait", &[("moves", None)]),
    ("reserve_acks", &[("count", None)]),
];

/// Every argument of ``name(*args, **kw)`` by name, defaults applied.
pub fn named(
    name: &str,
    args: &[Value],
    kw: &Map<String, Value>,
) -> Result<Map<String, Value>, String> {
    let (_, params) = SIGNATURES
        .iter()
        .find(|(n, _)| *n == name)
        .ok_or_else(|| format!("no builder call {name}"))?;
    if args.len() > params.len() {
        return Err(format!(
            "{name}: {} arguments for {}",
            args.len(),
            params.len()
        ));
    }
    let mut out = Map::new();
    for (i, (p, default)) in params.iter().enumerate() {
        let v = match (args.get(i), kw.get(*p), default) {
            (Some(a), _, _) => a.clone(),
            (None, Some(k), _) => k.clone(),
            (None, None, Some(d)) => serde_json::from_str(d).map_err(|e| e.to_string())?,
            (None, None, None) => return Err(format!("{name}: no {p}")),
        };
        out.insert(p.to_string(), v);
    }
    Ok(out)
}

/// The runner's address function: each field a list of `[bit, width, shift]`
/// segments, its value the segments' bits put back at their shifts.
pub struct FieldList(pub Vec<Vec<(u32, u32, u32)>>);

impl FieldList {
    /// From a field list, or `{"fields": list}`.
    pub fn of(v: &Value) -> Result<FieldList, String> {
        let list = v.get("fields").unwrap_or(v);
        let fields = arr(list)?
            .iter()
            .map(|f| {
                arr(f)?
                    .iter()
                    .map(|s| Ok((u(&s[0])? as u32, u(&s[1])? as u32, u(&s[2])? as u32)))
                    .collect()
            })
            .collect::<Result<_, String>>()?;
        Ok(FieldList(fields))
    }
}

impl Addresses for FieldList {
    fn fields(&self, word: &Word, _: &str) -> Vec<AddrField> {
        self.0
            .iter()
            .map(|segs| AddrField {
                segments: segs.clone(),
                value: segs
                    .iter()
                    .fold(0u64, |v, &(b, w, s)| v | word.get(b, w).wrapping_shl(s)),
            })
            .collect()
    }
}

/// One builder call with every argument named: its return (an index for
/// `unit`, `buffer`, `payload`, `jump`), or the builder's refusal; `Err` for
/// arguments that do not parse.
pub fn call(
    b: &mut PackageBuilder,
    name: &str,
    a: &Map<String, Value>,
) -> Result<Result<Option<u64>, Refusal>, String> {
    let fl = match a.get("addresses") {
        Some(v) if !v.is_null() => Some(FieldList::of(v)?),
        _ => None,
    };
    let addrs = fl.as_ref().map(|x| x as &dyn Addresses);
    let g = |k: &str| a.get(k).ok_or_else(|| format!("{name}: no {k}"));
    let n = |k: &str| u(g(k)?);
    let ui = |k: &str| n(k).map(|v| v as usize);
    let bo = |k: &str| flag(a.get(k), false);
    let some = |v: usize| Ok(Some(v as u64));
    Ok(match name {
        "step" => {
            let op = Op::from_code(n("op")?).ok_or("unknown step opcode")?;
            let flags = u8::try_from(n("flags")?).map_err(|_| "step flags exceed 8 bits")?;
            b.steps
                .push(Step::new(op, n("unit")?, n("count")?, n("arg")?, flags));
            Ok(None)
        }
        "end" => {
            b.steps.push(Step::new(Op::End, 0, 0, 0, 0));
            Ok(None)
        }
        "unit" => {
            let mesh = match a.get("mesh") {
                None | Some(Value::Null) => None,
                Some(v) => Some(u(v)?),
            };
            let t = a.get("type_name").and_then(Value::as_str);
            b.unit(coord(g("coord")?)?, t, mesh).and_then(some)
        }
        "buffer" => {
            let kind = Kind::from_code(n("kind")?).ok_or("kind")?;
            let name = g("name")?.as_str().ok_or("buffer name")?;
            some(b.buffer(name, n("size")?, n("default")?, kind))
        }
        "spans_from" => {
            let live = arr(g("live")?)?
                .iter()
                .map(|p| Ok((u(&p[0])?, u(&p[1])?)))
                .collect::<Result<_, String>>()?;
            b.spans_from(&live);
            Ok(None)
        }
        "payload" => {
            let t = g("unit_type")?.as_str().unwrap_or_default();
            some(b.payload(word(g("word")?)?, addrs, t))
        }
        "dispatch" => b
            .dispatch(ui("unit")?, &words(g("words")?)?, addrs)
            .map(|_| None),
        "repeat" => {
            let incs: Vec<Increment> = arr(g("increments")?)?
                .iter()
                .map(|i| {
                    Ok((
                        u(&i[0])? as usize,
                        u(&i[1])? as u32,
                        u(&i[2])? as u32,
                        u(&i[3])?,
                    ))
                })
                .collect::<Result<_, String>>()?;
            b.repeat(
                ui("unit")?,
                &words(g("template")?)?,
                n("repeats")?,
                &incs,
                addrs,
            )
            .map(|_| None)
        }
        "dispatch_periodic" => {
            let fields: Vec<(u32, u32)> = arr(g("fields")?)?
                .iter()
                .map(|x| Ok((u(&x[0])? as u32, u(&x[1])? as u32)))
                .collect::<Result<_, String>>()?;
            b.dispatch_periodic(ui("unit")?, &words(g("words")?)?, &fields, addrs)
                .map(|_| None)
        }
        "await_" => {
            b.await_(ui("unit")?, n("count")?);
            Ok(None)
        }
        "barrier" => {
            b.barrier();
            Ok(None)
        }
        "signal" => {
            b.signal(n("value32")?, n("value64")?);
            Ok(None)
        }
        "settle" => {
            b.settle(n("cycles")?);
            Ok(None)
        }
        "ring" => b
            .ring(n("mesh")?, n("slot")?, n("amount")?, bo("fence"))
            .map(|_| None),
        "wait_bell" => {
            b.wait_bell(n("mesh")?, n("count")?);
            Ok(None)
        }
        "wait_slot" => b
            .wait_slot(n("slot")?, n("count")?, bo("consume"))
            .map(|_| None),
        "store" => {
            b.store(&pairs(g("pairs")?)?, bo("from_regs"), bo("relocate"));
            Ok(None)
        }
        "ctrl" => {
            b.ctrl(&pairs(g("pairs")?)?, bo("from_regs"));
            Ok(None)
        }
        "load" => {
            let index = match a.get("index") {
                None | Some(Value::Null) => None,
                Some(v) => Some(u(v)?),
            };
            b.load(n("reg")?, n("address")?, index, bo("ctrl"), bo("relocate"));
            Ok(None)
        }
        "alu" => {
            let op = Alu::from_code(n("op")?).ok_or("alu op")?;
            b.alu(n("rd")?, op, n("ra")?, n("b")?, bo("imm"))
                .map(|_| None)
        }
        "jump" => {
            let c = Cond::from_code(n("cond")?).ok_or("cond")?;
            some(b.jump(n("target")?, c, n("ra")?, n("b")?, bo("imm")))
        }
        "repoint" => {
            b.repoint(ui("start")?, n("index")?);
            Ok(None)
        }
        "retarget" => {
            b.retarget(ui("step")?, n("target")?);
            Ok(None)
        }
        "patch" => b
            .patch(
                n("reg")?,
                n("index")?,
                n("bit")?,
                n("width")?,
                n("shift")?,
                bo("steps"),
                bo("add"),
            )
            .map(|_| None),
        "send" => {
            b.send(
                ui("unit")?,
                n("flit_type")?,
                &words(g("words")?)?,
                bo("ack"),
            );
            Ok(None)
        }
        "push" => {
            b.push(
                ui("unit")?,
                word(g("descriptor")?)?,
                n("address")?,
                n("words")?,
                bo("ack"),
                bo("relocate"),
            );
            Ok(None)
        }
        "recv" => {
            let unit = u_or(a.get("unit"), ANY_UNIT)?;
            b.recv(n("address")?, n("count")?, unit, bo("relocate"));
            Ok(None)
        }
        "mover" => {
            b.mover(&pairs(g("writes")?)?, addrs, bo("posted"));
            Ok(None)
        }
        "fetch_from" => b
            .fetch_from(ui("unit")?, n("addr")?, n("count")?)
            .map(|_| None),
        "mwait" => {
            b.mwait(n("moves")?);
            Ok(None)
        }
        "reserve_acks" => {
            b.reserve_acks(n("count")?);
            Ok(None)
        }
        other => return Err(format!("no builder call {other}")),
    })
}

/// A builder from the runner's `builder` spec: `types` as `[x, y, name]`,
/// required `credit`, `mesh`, `signature`, `fetch`.
pub fn builder(spec: &Value, f: &crate::Facts) -> Result<PackageBuilder, String> {
    let types = spec
        .get("types")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .iter()
        .map(|t| {
            Ok((
                (u(&t[0])? as u32, u(&t[1])? as u32),
                t[2].as_str().ok_or("type")?.to_string(),
            ))
        })
        .collect::<Result<_, String>>()?;
    let credit = if crate::mutant::on("l0.build/default_credit") {
        u_or(spec.get("credit"), 32)?
    } else {
        u(&spec["credit"])?
    };
    let mut b = PackageBuilder::new(f, types, credit);
    b.mesh = u_or(spec.get("mesh"), 0)?;
    if let Some(s) = spec.get("signature") {
        b.signature = u(s)?;
    }
    b.fetch = match spec.get("fetch") {
        None | Some(Value::Null) => None,
        Some(c) => Some(coord(c)?),
    };
    Ok(b)
}

/// `PackageBuilder` calls; the package, the calls' returns, bindings.
pub fn run(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let f = super::facts(m)?;
    let spec = input.get("builder").unwrap_or(&Value::Null);
    if spec.get("credit").is_none() && !crate::mutant::on("l0.build/default_credit") {
        return Ok(json!({"refused": "TypeError"}));
    }
    let mut b = builder(spec, &f)?;
    let mut results = Vec::new();
    for c in arr(input.get("calls").unwrap_or(&json!([])))? {
        let c = arr(c)?;
        let name = c[0].as_str().ok_or("call name")?;
        let mut args: Vec<Value> = c[1..].to_vec();
        let kw = match args.last() {
            Some(Value::Object(o)) => {
                let o = o.clone();
                args.pop();
                o
            }
            _ => Map::new(),
        };
        match call(&mut b, name, &named(name, &args, &kw)?)? {
            Ok(r) => results.push(r.map_or(Value::Null, |v| json!(v))),
            Err(e) => return Ok(refused(&e)),
        }
    }
    let opts = input.get("build").cloned().unwrap_or(json!({}));
    let pkg = b.build(
        flag(opts.get("defaults"), true),
        flag(opts.get("checksum"), false),
    );
    let raw = match pkg.to_bytes(&f) {
        Ok(r) => r,
        Err(e) => return Ok(refused(&e)),
    };
    let mut out = json!({
        "results": results,
        "package": describe(&pkg),
        "bindings": b.bindings().iter().map(|&v| hx(v as i128)).collect::<Vec<_>>(),
    });
    if flag(input.get("lines"), false) {
        out["lines"] = json!(lines(&raw));
    }
    Ok(super::keys(out, input))
}
