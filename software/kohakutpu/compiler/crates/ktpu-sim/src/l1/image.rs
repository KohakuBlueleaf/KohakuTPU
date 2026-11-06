//! An L1 image as instructions (L1.IMG): each op carries the configuration it
//! names, so nothing here tracks what VCFG words the encoder chooses.

use ktpu_l1::text::{fmt, Definition, Stmt, Term};

use super::term::{immediate, int, numbered, pair, Env};
use crate::units::core::inst::*;
use crate::units::core::{Desc, CHUNKS, PEER_COUNT_BIT};
use crate::units::link::{Place, Target};
use crate::R;

/// A descriptor's base: an address, or `peer(OFF, count=1)` (L1.VC.005).
fn base_of(t: &Term, env: &Env) -> R<i64> {
    let Term::Call(n, args) = t else {
        return int(t, env);
    };
    if n != "peer" {
        return int(t, env);
    }
    let mut base = int(args.first().ok_or("peer(OFF)")?, env)?;
    for a in &args[1..] {
        match a {
            Term::Kw(k, v) if k == "count" => base |= int(v, env)? << PEER_COUNT_BIT,
            Term::Kw(k, v) if k == "mesh" => base |= int(v, env)? << 24,
            Term::Kw(k, v) if k == "fin" || k == "ack" => {
                let (x, y) = pair(v, env)?;
                let shift = if k == "fin" { 26 } else { 16 };
                base |= ((y << 4) | x) << shift;
            }
            other => return Err(format!("a peer base's {}", fmt(other))),
        }
    }
    Ok(base)
}

/// One descriptor statement.
pub fn desc(st: &Stmt, env: &Env) -> R<(u32, Desc)> {
    let (name, base) = st
        .kwargs()
        .into_iter()
        .find(|(k, _)| *k != "walk")
        .ok_or_else(|| "desc aN = BASE walk=..".to_string())?;
    let ad = numbered(&Term::name(name), "a")?;
    let mut d = Desc {
        base: base_of(base, env)?,
        ..Desc::default()
    };
    let dims: Vec<&Term> = match st.kw("walk") {
        None => Vec::new(),
        Some(Term::Tuple(items)) if items.iter().all(|i| matches!(i, Term::Tuple(_))) => {
            items.iter().collect()
        }
        Some(w) => vec![w],
    };
    for (i, t) in dims.iter().enumerate() {
        let (s, b) = pair(t, env)?;
        d.dims[i] = (s, b as u32);
    }
    Ok((ad, d))
}

/// An image's leading descriptors and its instructions.
pub type Bound = (Vec<(u32, Desc)>, Vec<Inst>);

/// `(leading descriptors, instructions)` of `image` with its parameters bound.
pub fn read(image: &Definition, args: &[i64]) -> R<Bound> {
    let env: Env = image
        .params
        .iter()
        .cloned()
        .zip(args.iter().copied())
        .collect();
    let mut descs = Vec::new();
    let mut body = image.stmts.as_slice();
    while let Some(st) = body.first().filter(|s| s.op == "desc") {
        descs.push(desc(st, &env)?);
        body = &body[1..];
    }
    let mut out = Vec::new();
    block(body, &env, &mut out)?;
    Ok((descs, out))
}

fn block(stmts: &[Stmt], env: &Env, out: &mut Vec<Inst>) -> R<()> {
    for st in stmts {
        statement(st, env, out).map_err(|e| format!("line {}: {e}", st.line))?;
    }
    Ok(())
}

fn kind(t: &Term) -> R<usize> {
    match t.as_name() {
        Some("fill") => Ok(FILL),
        Some("unpack") => Ok(UNPACK),
        Some("math") => Ok(MATH),
        Some("pack") => Ok(PACK),
        Some("drain") => Ok(DRAIN),
        _ => Err(format!("kind {}", fmt(t))),
    }
}

fn waiter(t: &Term) -> R<Waiter> {
    match t.as_name() {
        Some("fe") => Ok(Waiter::Fe),
        Some("unpack") => Ok(Waiter::Unpack),
        Some("math") => Ok(Waiter::Math),
        Some("pack") => Ok(Waiter::Pack),
        Some("fill") => Ok(Waiter::Fill),
        Some("drain") => Ok(Waiter::Drain),
        _ => Err(format!("waiter {}", fmt(t))),
    }
}

/// A coordinate pair or named unit, as a burst names its receiver (L1.IMG.042).
pub fn target(t: &Term, env: &Env) -> R<Target> {
    if matches!(t, Term::Tuple(_)) {
        let (x, y) = pair(t, env)?;
        return Ok(Target {
            mesh: None,
            place: Place::At(x as u32, y as u32),
        });
    }
    let text = fmt(t);
    let (mesh, unit) = match text.strip_prefix("mesh").and_then(|r| r.split_once('.')) {
        Some((m, u)) => (
            Some(m.parse::<usize>().map_err(|_| format!("mesh in {text}"))?),
            u,
        ),
        None => (None, text.as_str()),
    };
    let index = |prefix: &str| {
        unit.strip_prefix(prefix)
            .and_then(|r| r.strip_suffix(']'))
            .and_then(|n| n.parse::<usize>().ok())
    };
    let place = match (index("vc["), index("mg[")) {
        (Some(i), _) => Place::Vc(i),
        (_, Some(i)) => Place::Mg(i),
        _ => return Err(format!("a burst's receiver {text}")),
    };
    Ok(Target { mesh, place })
}

fn opt(st: &Stmt, key: &str, env: &Env, default: i64) -> R<i64> {
    st.kw(key).map_or(Ok(default), |t| int(t, env))
}

fn statement(st: &Stmt, env: &Env, out: &mut Vec<Inst>) -> R<()> {
    let pos = st.positional();
    let inst = match st.op.as_str() {
        "loop" | "skipz" => {
            let sreg = numbered(pos[0], "s")?;
            let at = out.len();
            out.push(Inst::Halt);
            block(&st.body, env, out)?;
            let n = (out.len() - at - 1) as u32;
            out[at] = if st.op == "loop" {
                Inst::Loop { sreg, body: n }
            } else {
                Inst::Skipz { sreg, n }
            };
            return Ok(());
        }
        "vunpk" | "vpack" => walk(st, &pos, env)?,
        "vfill" | "vdrain" => {
            let ad = numbered(pos[0], "a")?;
            let off = l1_word(pos[2], env)?;
            let rel = pos[3..].iter().any(|p| p.as_name() == Some("rel"));
            let peer = match st.kw("to") {
                Some(t) => Some(Peer {
                    to: target(t, env)?,
                    buf: opt(st, "buf", env, 0)? as u32,
                }),
                None => None,
            };
            if st.op == "vfill" {
                Inst::Fill { ad, off, rel }
            } else {
                Inst::Drain { ad, off, rel, peer }
            }
        }
        "vwait" => {
            if pos[1].as_name() != Some("rx") {
                return Err("vwait on credits in this simulator".into());
            }
            Inst::Landed {
                waiter: waiter(pos[0])?,
                n: opt(st, "n", env, 0)? as u32,
            }
        }
        "vpost" => Inst::Post {
            to: target(pos[0], env)?,
            n: opt(st, "n", env, 0)? as usize,
        },
        "vsync" => {
            let waiter = waiter(pos[0])?;
            let on = match st.kw("mark") {
                Some(m) => On::Mark(numbered(m, "m")?),
                None => On::Kind {
                    kind: kind(st.kw("on").ok_or("vsync on=")?)?,
                    slack: opt(st, "slack", env, 0)?,
                },
            };
            Inst::Sync { waiter, on }
        }
        "vmark" => Inst::Mark {
            mark: numbered(pos[0], "m")?,
            kind: kind(st.kw("on").ok_or("vmark on=")?)?,
            slack: opt(st, "slack", env, 0)?,
        },
        "vbar" => Inst::Bar,
        "ainc" => {
            let [Term::AddAssign(a, b)] = st.args.as_slice() else {
                return Err("ainc aN += BYTES".into());
            };
            Inst::Ainc {
                ad: numbered(a, "a")?,
                inc: int(b, env)?,
            }
        }
        "uptr" | "uinc" | "pptr" | "pinc" => Inst::Ptr {
            reg: match st.op.as_str() {
                "uptr" => Ptr::Uptr,
                "uinc" => Ptr::Uinc,
                "pptr" => Ptr::Pptr,
                _ => Ptr::Pinc,
            },
            value: int(pos[0], env)? as u32,
        },
        "seti" => {
            let Some(Term::Assign(a, v)) = st.args.first() else {
                return Err("seti sN = V".into());
            };
            let to_k = a.as_name() == Some("k3");
            Inst::Seti {
                sreg: if to_k { 0 } else { numbered(a, "s")? },
                to_k,
                value: immediate(v, env)?,
            }
        }
        "setvl" => Inst::Setvl {
            sreg: numbered(pos[0], "s")?,
        },
        "getstk" => Inst::Getstk {
            sreg: numbered(pos[0], "s")?,
        },
        "halt" => Inst::Halt,
        op => math(op, st, &pos, env)?,
    };
    out.push(inst);
    Ok(())
}

fn l1_word(t: &Term, env: &Env) -> R<u32> {
    match t {
        Term::View(n, s) if n == "l1" && s.len() == 1 => Ok(int(&s[0], env)? as u32),
        _ => Err(format!("wanted l1[OFFSET], got {}", fmt(t))),
    }
}

/// `vN` or `vN[c:]` / `vN[c]`: the register and its (base, stride).
fn chunk(t: &Term, env: &Env) -> R<(u32, Option<(u32, u32)>)> {
    match t {
        Term::View(n, s) if s.len() == 1 => {
            let reg = numbered(&Term::name(n.clone()), n.get(..1).unwrap_or("v"))?;
            let nib = match &s[0] {
                Term::Slice(Some(lo), None) => (int(lo, env)? as u32, 1),
                Term::Int(v, _) => (*v as u32, 0),
                other => return Err(format!("chunk select {}", fmt(other))),
            };
            Ok((reg, Some(nib)))
        }
        _ => {
            let name = t.as_name().ok_or(format!("register {}", fmt(t)))?;
            Ok((numbered(t, name.get(..1).unwrap_or("v"))?, None))
        }
    }
}

fn walk(st: &Stmt, pos: &[&Term], env: &Env) -> R<Inst> {
    let pack = st.op == "vpack";
    let (reg, nib) = chunk(pos[0], env)?;
    let off = l1_word(pos[2], env)?;
    let mode = pos[3].as_name().ok_or("a walk mode")?;
    let rel = pos[4..].iter().any(|p| p.as_name() == Some("rel"));
    let cb = nib.map_or(0, |n| n.0);
    let mode_ = match mode {
        "f16" => Mode::F16,
        "gt4" => Mode::Gt4,
        "f32" => Mode::F32,
        "mx7a" => Mode::Mx7(false),
        "mx7b" => Mode::Mx7(true),
        _ => return Err(format!("mode {mode}")),
    };
    Ok(Inst::Walk {
        pack,
        reg,
        off,
        n: opt(st, "n", env, CHUNKS as i64 - i64::from(cb))? as u32,
        mode: mode_,
        cb,
        rel,
        stride: Some(match st.kw("walk") {
            Some(t) => pair(t, env)?,
            None => (1, 4),
        }),
    })
}

/// A source, its chunk select, and the crossbar on it.
struct Source {
    src: Src,
    nib: Option<(u32, u32)>,
    xbar: Option<(u32, u32, u32)>,
}

fn source(t: &Term, env: &Env) -> R<Source> {
    if let Term::Call(name, args) = t {
        let xm = match name.as_str() {
            "xor" => XM_XOR,
            "rot" => XM_ROT,
            "bcast" => XM_BCAST,
            "merge" => XM_MERGE,
            _ => return Err(format!("no crossbar {name}")),
        };
        let inner = source(args.first().ok_or("a crossbar's register")?, env)?;
        let kw = |k: &str| {
            args[1..].iter().find_map(|a| match a {
                Term::Kw(n, v) if n == k => Some(v.as_ref()),
                _ => None,
            })
        };
        let (xk, sh) = if xm == XM_BCAST {
            (
                kw("lane").map_or(Ok(0), |t| int(t, env))?,
                kw("sh").map_or(Ok(0), |t| int(t, env))?,
            )
        } else {
            (int(&args[1], env)?, 0)
        };
        return Ok(Source {
            xbar: Some((xm, xk as u32, sh as u32)),
            ..inner
        });
    }
    let (reg, nib) = chunk(t, env)?;
    let head = match t {
        Term::View(n, _) => n.chars().next(),
        _ => t.as_name().and_then(|n| n.chars().next()),
    };
    let src = match head {
        Some('v') => Src::V(reg),
        Some('s') => Src::S(reg),
        Some('k') => Src::K(reg),
        _ => return Err(format!("source {}", fmt(t))),
    };
    Ok(Source {
        src,
        nib,
        xbar: None,
    })
}

fn math(op: &str, st: &Stmt, pos: &[&Term], env: &Env) -> R<Inst> {
    let code = MathOp::parse(op).ok_or(format!("no instruction {op}"))?;
    let (reads_b, reads_c) = match code {
        MathOp::Vadd | MathOp::Vsub => (false, true),
        MathOp::Vmul
        | MathOp::Vmax
        | MathOp::Vmin
        | MathOp::Vexp2d
        | MathOp::Vcmplt
        | MathOp::Vcmpgt
        | MathOp::Vcmpeq => (true, false),
        MathOp::Vsel | MathOp::Vfma | MathOp::Vfnma => (true, true),
        _ => (false, false),
    };
    let compare = matches!(code, MathOp::Vcmplt | MathOp::Vcmpgt | MathOp::Vcmpeq);
    let (vd, dnib) = chunk(pos[0], env)?;
    let mut rest = pos[1..].iter();
    let a = source(rest.next().ok_or("operand a")?, env)?;
    let b = reads_b
        .then(|| source(rest.next().expect("operand b"), env))
        .transpose()?;
    let c = reads_c
        .then(|| source(rest.next().expect("operand c"), env))
        .transpose()?;
    let mut cfg = Cfg {
        vl: match st.kw("vl") {
            Some(t) if t.as_name() == Some("keep") => return Err("vl=keep".into()),
            Some(t) => int(t, env)? as u32,
            None => 128,
        },
        ..Cfg::RESET
    };
    cfg.chk[0] = a.nib.unwrap_or((0, 1));
    cfg.chk[3] = dnib.unwrap_or((0, 1));
    let mut bsrc = Src::V(0);
    if let Some(b) = &b {
        bsrc = b.src;
        cfg.chk[1] = b.nib.unwrap_or((0, 1));
        if let Some((xm, xk, sh)) = b.xbar {
            (cfg.xm, cfg.xk, cfg.sh) = (xm, xk, sh);
        }
    }
    let mut csrc = Src::V(0);
    if let Some(c) = &c {
        csrc = c.src;
        cfg.chk[2] = c.nib.unwrap_or((0, 1));
        if let Some((xm, xk, sh)) = c.xbar {
            (cfg.xm, cfg.xk, cfg.sh) = (xm, xk, sh);
            cfg.xc = true;
            bsrc = c.src;
            cfg.chk[1] = c.nib.unwrap_or((0, 1));
        }
    }
    if let Some(p) = st.kw("if") {
        (cfg.pm, cfg.pr) = (1, numbered(p, "p")?);
    }
    if let Some(p) = st.kw("unless") {
        (cfg.pm, cfg.pr) = (2, numbered(p, "p")?);
    }
    let mut vd = vd;
    if compare {
        (cfg.pr, vd) = (vd, 0);
    }
    Ok(Inst::Math {
        op: code,
        vd,
        a: a.src,
        b: bsrc,
        c: csrc,
        om: true,
        cfg: Some(cfg),
    })
}
