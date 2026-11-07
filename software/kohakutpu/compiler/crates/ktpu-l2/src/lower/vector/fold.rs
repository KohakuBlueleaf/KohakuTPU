//! Word-wide statistic folds with a streamed operand axis (L2.PASS.014).

use ktpu_l1::text::{Env, Module, Stmt, Term};

use super::ctx::Ctx;
use super::gen::{print, typed as ir};
use super::{split, RPar, VErr, VResult};
use crate::ir::Body;
use crate::lower::sync::{mem, Access};
use crate::target::Target;

pub struct Chain {
    pub loads: Vec<usize>,
    code: &'static str,
}

fn statistic(s: &Stmt, dtype: &str, space: &str) -> bool {
    matches!(&s.annot, Some(Term::Placed(t, p))
        if matches!(t.as_ref(), Term::View(d, shape)
            if d == dtype && shape.as_slice() == [Term::int(16)]) && p.as_name() == Some(space))
}

fn unpack(load: &Stmt, unpack: &Stmt) -> bool {
    load.op == "load"
        && load.args.len() == 1
        && statistic(load, "f16", "l1")
        && unpack.op == "unpack"
        && statistic(unpack, "f32", "reg")
        && unpack.args.len() == 1
        && unpack.args[0].as_name() == load.target_name()
}

/// A left fold of complete statistic words, with no intermediate observer.
pub fn chain(stmts: &[Stmt]) -> Option<Chain> {
    if stmts.len() < 7 || stmts.iter().any(|s| !s.body.is_empty()) {
        return None;
    }
    if !unpack(&stmts[0], &stmts[1]) {
        return None;
    }
    let mut loads = vec![0];
    let mut acc = stmts[1].target_name()?;
    let mut at = 2;
    let mut operation = None;
    while at + 3 <= stmts.len() - 2 {
        let (load, value, math) = (&stmts[at], &stmts[at + 1], &stmts[at + 2]);
        if !unpack(load, value)
            || load.target_name() == Some(acc)
            || value.target_name() == Some(acc)
            || !statistic(math, "f32", "reg")
            || math.args.len() != 2
            || math.args[0].as_name() != Some(acc)
            || math.args[1].as_name() != value.target_name()
        {
            return None;
        }
        let code = match math.op.as_str() {
            "add" => "vadd",
            "mul" => "vmul",
            "max" => "vmax",
            "min" => "vmin",
            _ => return None,
        };
        if operation.is_some_and(|op| op != code) {
            return None;
        }
        operation = Some(code);
        acc = math.target_name()?;
        loads.push(at);
        at += 3;
    }
    if at + 2 != stmts.len() {
        return None;
    }
    let (pack, store) = (&stmts[at], &stmts[at + 1]);
    if pack.op != "pack"
        || !statistic(pack, "f16", "l1")
        || pack.args.len() != 1
        || pack.args[0].as_name() != Some(acc)
        || store.op != "store"
        || store.args.len() != 3
        || store.args[2].as_name() != pack.target_name()
    {
        return None;
    }
    Some(Chain {
        loads,
        code: operation?,
    })
}

/// A single row tile's fold. Operand views share one affine descriptor; the
/// accumulator stays in E8M15 until the final pack, in written operand order.
pub fn lower(
    body: &Body,
    par: &RPar,
    env: &Env,
    name: &str,
    t: &Target,
) -> VResult<Option<(Module, Vec<String>, Vec<Access>)>> {
    if par.st.kw("classed").is_some() {
        return Ok(None);
    }
    let (pre, lp) = split(body, par)?;
    let Some(lp) = lp.filter(|_| pre.is_empty()) else {
        return Ok(None);
    };
    let stmts: Vec<Stmt> = lp.body.iter().map(|n| n.st().clone()).collect();
    let Some(chain) = chain(&stmts).filter(|c| c.loads.len() + 1 > t.descriptors) else {
        return Ok(None);
    };
    let mut c = Ctx::new(body, t, env, Some(lp.clone()))?;
    if c.trips.len() != 1 {
        return Ok(None);
    }
    c.unit = super::unit_of(body, par, env)?;
    c.prepare(&lp.body[chain.loads[0]], true)?;
    let first = c.descs[0].clone();
    let mut stride = None;
    let mut lo = first.off;
    let mut hi = first.off + first.extent();
    let mut probe = Ctx::new(body, t, env, Some(lp.clone()))?;
    for (i, &at) in chain.loads.iter().enumerate().skip(1) {
        probe.descs.clear();
        probe.loads.clear();
        probe.of.clear();
        probe.top = 0;
        probe.prepare(&lp.body[at], true)?;
        let d = &probe.descs[0];
        let step = *stride.get_or_insert(d.off - first.off);
        if d.buffer != first.buffer
            || d.walk != first.walk
            || d.words != 1
            || d.mapped.is_some()
            || i128::from(d.off) != i128::from(first.off) + i as i128 * i128::from(step)
        {
            return Ok(None);
        }
        lo = lo.min(d.off);
        hi = hi.max(d.off + d.extent());
    }
    if first.words != 1 || first.mapped.is_some() {
        return Ok(None);
    }
    let Some(stride) = stride else {
        return Ok(None);
    };
    let Some(store) = lp.body.last() else {
        return Ok(None);
    };
    c.prepare(store, true)?;
    let output = &c.descs[1];
    let (Some(input_name), Some(output_name)) = (&first.buffer, &output.buffer) else {
        return Ok(None);
    };
    let vl = || vec![ir::kw("vl", Term::int(16))];
    let memory = |kind, reg: &str, word, mode, rel, options| {
        ir::memory(kind, Term::name(reg), word, mode, rel, options).map_err(VErr::Value)
    };
    let load = |reg: &str| -> VResult<Vec<Stmt>> {
        Ok(vec![
            ir::sync("fill", "unpack", None),
            memory(ir::Memory::Fill, "a0", 0, None, true, vec![])?,
            ir::instruction("vbar", vec![], vec![]),
            memory(
                ir::Memory::Unpack,
                reg,
                0,
                Some("f16"),
                false,
                vec![ir::kw("n", Term::int(1))],
            )?,
            ir::math(
                "vmul",
                Term::name(reg),
                vec![Term::name("k1"), Term::name(reg)],
                vl(),
            ),
        ])
    };
    let mut code = vec![
        ir::desc("a0", Term::name("p_a0"), &first.walk.0).map_err(VErr::Value)?,
        ir::desc("a1", Term::name("p_a1"), &output.walk.0).map_err(VErr::Value)?,
        ir::ainc("a0", stride),
    ];
    code.extend(load("%1")?);
    let counter = format!("s{}", t.loop_s());
    code.push(ir::seti(
        &counter,
        Term::int((chain.loads.len() - 1) as i128),
    ));
    let mut iteration = load("%2")?;
    iteration.push(ir::math(
        chain.code,
        Term::name("%1"),
        vec![Term::name("%1"), Term::name("%2")],
        vl(),
    ));
    code.push(ir::block("loop", &counter, iteration).map_err(VErr::Value)?);
    code.extend([
        ir::math("vmov", Term::name("%1"), vec![Term::name("%1")], vl()),
        memory(
            ir::Memory::Pack,
            "%1",
            1,
            Some("f16"),
            false,
            vec![ir::kw("n", Term::int(1))],
        )?,
        ir::sync("drain", "pack", None),
        memory(ir::Memory::Drain, "a1", 1, None, false, vec![])?,
        ir::instruction("halt", vec![], vec![]),
    ]);
    let mut events = Vec::new();
    symbolic(code, &mut events);
    let events = par
        .zero_sign
        .events(events, super::sign::Lanes::of(t))
        .map_err(|message| VErr::Fail(body.reader.fail(&par.st, "L2.VC.026", message)))?;
    let pinned = std::collections::HashSet::from(["%1".to_string()]);
    let (assigned, _) = super::assign_typed::assign(&events, &pinned, body.vregs);
    let code = ir::nest(assigned?).map_err(VErr::Value)?;
    let module = print::image(name, vec!["p_a0".into(), "p_a1".into()], code, body.vregs)
        .map_err(VErr::Value)?;
    let params = vec![
        format!("{input_name}+{}", first.off),
        format!("{output_name}+{}", output.off),
    ];
    let accesses = vec![
        mem(&c.unit, false, input_name, lo, hi).described(0),
        mem(
            &c.unit,
            true,
            output_name,
            output.off,
            output.off + output.extent(),
        )
        .described(1),
    ];
    Ok(Some((module, params, accesses)))
}

fn symbolic(code: Vec<Stmt>, events: &mut Vec<ir::Event>) {
    for mut stmt in code {
        let body = std::mem::take(&mut stmt.body);
        let syms: Vec<_> = stmt
            .args
            .iter()
            .filter_map(|a| match a.as_name() {
                Some("%1") => Some(ir::Symbol(1)),
                Some("%2") => Some(ir::Symbol(2)),
                _ => None,
            })
            .collect();
        if syms.is_empty() {
            events.push(ir::Event::Fixed(stmt));
        } else {
            events.push(ir::Event::Op { stmt, syms });
        }
        if !body.is_empty() {
            events.push(ir::Event::Open);
            symbolic(body, events);
            events.push(ir::Event::Close);
        }
    }
}
