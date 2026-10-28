//! The `l0.program` runner: a JSON L1 step list (`runners/l0.py:l1_program`)
//! built for the vector's machine.

use std::collections::BTreeMap;

use serde_json::{json, Value};

use super::json::{arr, coord, describe, flag, num, pairs, refused, u, u_or, word, words};
use super::Port;
use crate::build::fields::IsaAddresses;
use crate::build::Addresses;
use crate::format::{Alu, Cond};
use crate::mover;
use crate::program::{BuildOpts, Patch, Program};
use crate::word::Word;
use crate::Facts;

fn patches(v: Option<&Value>) -> Result<Vec<Patch>, String> {
    let Some(v) = v else {
        return Ok(Vec::new());
    };
    arr(v)?
        .iter()
        .map(|p| {
            Ok(Patch {
                word: u(&p[0])? as usize,
                segments: arr(&p[1])?
                    .iter()
                    .map(|s| Ok((u(&s[0])? as u32, u(&s[1])? as u32, u(&s[2])? as u32)))
                    .collect::<Result<_, String>>()?,
                reg: u(&p[2])?,
                konst: u(&p[3])?,
            })
        })
        .collect()
}

/// A `Program` from a JSON step list; `send` takes words or a count (words
/// 1..n). A step Python refuses is `Ok(Err(..))`.
pub fn l1_program(f: &Facts, steps: &Value) -> Result<Result<Program, crate::Refusal>, String> {
    let mut p = Program::new();
    for st in arr(steps)? {
        let st = arr(st)?;
        let op = st[0].as_str().ok_or("step op")?;
        let r = &st[1..];
        let at = |i: usize| r.get(i);
        let n = |i: usize, d: u64| u_or(at(i), d);
        let b = |i: usize, d: bool| flag(at(i), d);
        let done: Result<(), crate::Refusal> = match op {
            "send" => {
                let ws = match &r[1] {
                    Value::Array(_) => words(&r[1])?,
                    v => (1..=u(v)?).map(Word::from_u64).collect(),
                };
                p.send(coord(&r[0])?, ws, patches(at(2))?);
                Ok(())
            }
            "mark" => {
                p.mark(coord(&r[0])?);
                Ok(())
            }
            "wait" => {
                let token = match at(1) {
                    None | Some(Value::Null) => None,
                    Some(t) => Some(u(t)? as usize),
                };
                p.wait(coord(&r[0])?, token).map(|_| ())
            }
            "move" => {
                p.move_(pairs(&r[0])?, Vec::new());
                Ok(())
            }
            "post" => {
                p.post(pairs(&r[0])?, Vec::new());
                Ok(())
            }
            "copy" => match mover::copy(f, num(&r[0])?, num(&r[1])?, num(&r[2])?) {
                Ok(w) => {
                    p.move_(w, Vec::new());
                    Ok(())
                }
                Err(e) => Err(e),
            },
            "jump" => {
                let cond = Cond::from_code(n(1, 0)?).ok_or("cond")?;
                p.jump(
                    r[0].as_str().ok_or("label")?,
                    cond,
                    n(2, 0)?,
                    n(3, 0)?,
                    b(4, true),
                );
                Ok(())
            }
            "store" => {
                p.store(pairs(&r[0])?, b(1, false));
                Ok(())
            }
            "ctrl" => {
                p.ctrl(pairs(&r[0])?, b(1, false));
                Ok(())
            }
            "ring" => {
                p.ring(u(&r[0])?, n(1, 0)?, n(2, 1)?, b(3, true));
                Ok(())
            }
            "wait_bell" => {
                p.wait_bell(u(&r[0])?, n(1, 1)?);
                Ok(())
            }
            "wait_slot" => {
                p.wait_slot(u(&r[0])?, u(&r[1])?, b(2, false));
                Ok(())
            }
            "load" => {
                let index = match at(2) {
                    None | Some(Value::Null) => None,
                    Some(v) => Some(u(v)?),
                };
                p.load(u(&r[0])?, u(&r[1])?, index, b(3, false));
                Ok(())
            }
            "alu" => {
                let alu = Alu::from_code(u(&r[1])?).ok_or("alu op")?;
                p.alu(u(&r[0])?, alu, n(2, 0)?, n(3, 0)?, b(4, true));
                Ok(())
            }
            "label" => p.label(r[0].as_str().ok_or("label")?).map(|_| ()),
            "signal" => {
                p.signal(n(0, 0)?, n(1, 0)?);
                Ok(())
            }
            "settle" => {
                p.settle(u(&r[0])?);
                Ok(())
            }
            "send_raw" => {
                p.send_raw(coord(&r[0])?, u(&r[1])?, words(&r[2])?, b(3, false));
                Ok(())
            }
            "push" => {
                p.push(
                    coord(&r[0])?,
                    word(&r[1])?,
                    u(&r[2])?,
                    u(&r[3])?,
                    b(4, false),
                );
                Ok(())
            }
            "recv" => {
                let from = match at(2) {
                    None | Some(Value::Null) => None,
                    Some(c) => Some(coord(c)?),
                };
                p.recv(u(&r[0])?, u(&r[1])?, from);
                Ok(())
            }
            "barrier" => {
                p.barrier();
                Ok(())
            }
            "fetch" => {
                p.fetch(coord(&r[0])?, u(&r[1])?, u(&r[2])?, patches(at(3))?);
                Ok(())
            }
            "wait_moves" => p.wait_moves(u(&r[0])? as usize).map(|_| ()),
            other => return Err(format!("no program step {other}")),
        };
        if let Err(e) = done {
            return Ok(Err(e));
        }
    }
    Ok(Ok(p))
}

/// `Program.build` of a JSON L1 step list for the vector's machine. With
/// ``through: packages`` the runner schedules through `verify.schedule`,
/// which is not this crate's.
pub fn run(input: &Value, m: Option<&ktpu_machine::Machine>) -> Port {
    let m = m.ok_or("l0.program needs a machine")?;
    if input.get("through").is_some() {
        return Err("`through: packages` runs verify.schedule: see through_packages".into());
    }
    let f = Facts::of(m);
    let prog = match l1_program(&f, &input["steps"])? {
        Ok(p) => p,
        Err(_) => return Ok(json!({"refused": "ValueError"})),
    };
    let spans = spans_of(input)?;
    let isa = if spans.is_empty() {
        None
    } else {
        Some(IsaAddresses::new(m).map_err(|e| e.to_string())?)
    };
    let opts = BuildOpts {
        fetch: if flag(input.get("fetch"), false) {
            f.fetch_port
        } else {
            None
        },
        resident: None,
        addresses: isa.as_ref().map(|i| i as &dyn Addresses),
        spans: (!spans.is_empty()).then_some(&spans),
    };
    let out = match prog.build(&f, opts) {
        Ok(b) => describe(&b.build(true, false)),
        Err(e) => return Ok(refused(&e)),
    };
    Ok(super::keys(out, input))
}

/// The input's `spans` as `{base: size}`.
pub fn spans_of(input: &Value) -> Result<BTreeMap<u64, u64>, String> {
    match input.get("spans") {
        Some(s) => arr(s)?.iter().map(|p| Ok((u(&p[0])?, u(&p[1])?))).collect(),
        None => Ok(BTreeMap::new()),
    }
}

/// What `kohakutpu.compiler.build.packages` returns for a one-node program:
/// the package `schedule` gives (the dispatch scheduler, `verify.schedule`,
/// with the program's spans and the isa address fields) bound at its
/// bindings and lowered for the engine.
pub type Schedule<'a> = &'a dyn Fn(
    &Program,
    &Facts,
    Option<crate::facts::Coord>,
    &BTreeMap<u64, u64>,
) -> Result<(crate::Package, Vec<u64>), String>;

/// The `l0.program` runner's `through: packages` form, with the dispatch
/// scheduler given: what the node receives (bindings, relocations, buffers,
/// step ops) and whether it is the same package the program builds with
/// every address absolute.
pub fn through_packages(
    input: &Value,
    m: Option<&ktpu_machine::Machine>,
    schedule: Schedule<'_>,
) -> Port {
    let m = m.ok_or("l0.program needs a machine")?;
    let f = Facts::of(m);
    let prog = match l1_program(&f, &input["steps"])? {
        Ok(p) => p,
        Err(_) => return Ok(json!({"refused": "ValueError"})),
    };
    let fetch = if flag(input.get("fetch"), false) {
        f.fetch_port
    } else {
        None
    };
    let spans = spans_of(input)?;
    let (pkg, bindings) = schedule(&prog, &f, fetch, &spans)?;
    let sent = match pkg
        .bind(&bindings)
        .and_then(|b| crate::engine::lower(&b, &f))
    {
        Ok(p) => p,
        Err(e) => return Ok(refused(&e)),
    };
    let plain = prog
        .build(
            &f,
            BuildOpts {
                fetch,
                ..Default::default()
            },
        )
        .and_then(|b| crate::engine::lower(&b.build(true, false), &f));
    let want = match plain {
        Ok(p) => p,
        Err(e) => return Ok(refused(&e)),
    };
    Ok(json!({
        "bindings": [],
        "relocs": sent.relocs.len(),
        "buffers": sent.buffers.len(),
        "ops": sent.steps.iter().map(|s| s.op.name()).collect::<Vec<_>>(),
        "same_as_absolute": sent.steps == want.steps && sent.payloads == want.payloads,
    }))
}
