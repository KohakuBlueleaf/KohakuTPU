//! Parallel pre-bank bands: flat events first, then headers and nested statements.

use super::*;
use crate::lower::vector::ctx::emit::typed::Emitter;
use crate::lower::vector::gen::{print, typed as ir};
use ktpu_l1::text::Module;

#[cfg(all(test, feature = "witness"))]
pub(super) mod tests;

#[allow(clippy::too_many_arguments)]
pub(in crate::lower::vector) fn lower_band(
    body: &Body,
    par: &RPar,
    env: &Env,
    name: &str,
    t: &Target,
    band: usize,
    remat: bool,
    ahead: bool,
    plan: &Plan,
    land: Option<i64>,
    hold: bool,
    pin: bool,
) -> VResult<(Module, Vec<String>, Vec<Access>)> {
    let (c, g) = flat_band(
        body, par, env, t, band, remat, ahead, plan, land, hold, pin, false,
    )?;
    let events = banks::typed::rotate(
        crate::lower::vector::gen::predicate::fuse(g.code),
        body.vcopies,
        &crate::lower::vector::modulo::Core::of(t),
    );
    let (assigned, _) = crate::lower::vector::assign_typed::assign(&events, &c.pinned, body.vregs);
    let result = image(&c, name, &assigned?).expect("generated band image");
    let fits = ktpu_l1::sched::schedule_module(&result.0, &t.machine)
        .and_then(|m| crate::lower::vector::encode::words_of(&m, name, result.1.len(), &t.machine))
        .is_ok_and(|n| n as i64 <= crate::target::IMEM_ROOM || on("l2.vector/imem-unbounded"));
    let (_, lp) = crate::lower::vector::split(body, par)?;
    // Peer streams retain their producer/consumer parity protocol.
    let peer = lp.as_ref().is_some_and(|lp| {
        ops_of(&lp.body)
            .iter()
            .any(|o| matches!(o.op_name(), "recv" | "send"))
    });
    if fits || peer || crate::mutant::on("l2.vector/no-serial-loop") {
        return Ok(result);
    }
    // One synchronized trip avoids duplicating the body for ping-pong buffers.
    let (c, g) = flat_band(
        body, par, env, t, band, remat, ahead, plan, land, hold, pin, true,
    )?;
    let events = banks::typed::rotate(
        crate::lower::vector::gen::predicate::fuse(g.code),
        body.vcopies,
        &crate::lower::vector::modulo::Core::of(t),
    );
    let (assigned, _) = crate::lower::vector::assign_typed::assign(&events, &c.pinned, body.vregs);
    Ok(image(&c, name, &assigned?).expect("generated serial band image"))
}

#[allow(clippy::too_many_arguments)]
fn flat_band<'a>(
    body: &'a Body,
    par: &RPar,
    env: &Env,
    t: &'a Target,
    band: usize,
    remat: bool,
    ahead: bool,
    plan: &Plan,
    land: Option<i64>,
    hold: bool,
    pin: bool,
    serial: bool,
) -> VResult<(Ctx<'a>, Emitter)> {
    let (pre, lp) = crate::lower::vector::split(body, par)?;
    let mut c = Ctx::new(body, t, env, lp.clone())?;
    c.classed = par.st.kw("classed").is_some();
    c.ahead = ahead;
    c.band = band;
    c.remat = remat;
    c.hold = hold;
    c.pin = pin;
    c.plan = plan.clone();
    c.land = land.unwrap_or(t.l1_words);
    c.unit = crate::lower::vector::unit_of(body, par, env)?;
    c.pre = pre.clone();
    if let Some(lp) = &lp {
        for n in &lp.body {
            if let Node::For(f) = n.as_ref() {
                return Err(c.fail(
                    &f.st,
                    "L2.VC.018",
                    "a loop inside the stream loop: write `for ... unroll`",
                ));
            }
            c.prepare(n, true)?;
        }
    }
    c.down = c.land;
    for n in &pre {
        c.prepare(n, false)?;
    }
    let mut g = Emitter::default();
    let mut all = pre.clone();
    if let Some(lp) = &lp {
        all.extend(ops_of(&lp.body));
    }
    if c.classed {
        let mut count = ir::seti(
            &format!("s{}", t.loop_s()),
            Term::int(c.trips.len() as i128),
        );
        count.args.push(ir::kw("classed", Term::int(1)));
        g.fixed(count);
        if !on("l2.class/no-idle") {
            let stage = format!("s{}", t.stage_reg());
            g.fixed(ir::seti(&stage, Term::int(1)));
            g.fixed(ir::instruction(
                "skipz",
                vec![Term::name(format!("s{}", t.loop_s()))],
                vec![],
            ));
            g.code.push(ir::Event::Open);
            g.fixed(ir::seti(&stage, Term::int(0)));
            g.code.push(ir::Event::Close);
            g.fixed(ir::instruction("skipz", vec![Term::name(&stage)], vec![]));
            g.code.push(ir::Event::Open);
            g.fixed(ir::instruction("halt", vec![], vec![]));
            g.code.push(ir::Event::Close);
        }
    }
    c.constants_typed(&mut g, &all);
    if let Some(lp) = &lp {
        if lp.body.iter().any(|o| matches!(o.as_ref(), Node::If(_))) {
            g.fixed(ir::instruction(
                "getstk",
                vec![Term::name(format!("s{}", t.stk_s()))],
                vec![],
            ));
        }
    }
    for n in &pre {
        c.prologue_typed(&mut g, n)?;
    }
    if pre.iter().any(|n| n.op_name() == "unpack") {
        g.fixed(ir::sync("fe", "unpack", None));
    }
    if let Some((_, rname, first, _)) = &c.rel {
        g.fixed(ir::instruction(
            "uptr",
            vec![Term::dec((c.vals[rname].buf + 8 * first).into())],
            vec![],
        ));
        g.fixed(ir::instruction("uinc", vec![Term::int(8)], vec![]));
    }
    if let Some(lp) = &lp {
        if c.classed {
            let init_at = g.code.len();
            g.fixed(ir::instruction(
                "loop",
                vec![Term::name(format!("s{}", t.loop_s()))],
                vec![],
            ));
            g.code.push(ir::Event::Open);
            g.fixed(ir::sync("fill", "drain", None));
            g.fixed(ir::sync("fill", "unpack", None));
            for &d in &c.loads {
                let d = &c.descs[d];
                g.fixed(
                    ir::memory(
                        ir::Memory::Fill,
                        Term::name(format!("a{}", d.ad)),
                        d.buf[0],
                        None,
                        true,
                        vec![],
                    )
                    .unwrap(),
                );
            }
            c.iteration_typed(&mut g, &lp.body, 0, false)?;
            g.fixed(ir::instruction("vbar", vec![], vec![]));
            g.code.push(ir::Event::Close);
            let mut init = Emitter::default();
            for (acc, maximum) in c.carries.values() {
                let acc = crate::lower::vector::ctx::emit::typed::symbol(acc);
                let src = if *maximum {
                    let stage = format!("s{}", t.stage_reg());
                    init.fixed(ir::seti(
                        &stage,
                        Term::Neg(Box::new(Term::Float(f64::INFINITY))),
                    ));
                    Term::name(stage)
                } else {
                    Term::name("k0")
                };
                init.op(
                    ir::math("vmov", acc.operand(ir::Select::Whole), vec![src], vec![]),
                    vec![acc],
                );
            }
            if !on("l2.class/carry-init") {
                g.code.splice(init_at..init_at, init.code);
            }
        } else if serial {
            let reg = format!("s{}", t.loop_s());
            g.fixed(ir::seti(&reg, Term::int(c.trips.len() as i128)));
            g.fixed(ir::instruction("loop", vec![Term::name(&reg)], vec![]));
            g.code.push(ir::Event::Open);
            g.fixed(ir::instruction("vbar", vec![], vec![]));
            g.fixed(ir::sync("fill", "unpack", None));
            g.fixed(ir::sync("fill", "drain", None));
            g.fixed(ir::sync("pack", "drain", None));
            for &d in &c.loads {
                let d = &c.descs[d];
                g.fixed(
                    ir::memory(
                        ir::Memory::Fill,
                        Term::name(format!("a{}", d.ad)),
                        d.buf[0],
                        None,
                        true,
                        vec![],
                    )
                    .unwrap(),
                );
            }
            g.fixed(ir::sync("unpack", "fill", None));
            c.iteration_typed(&mut g, &lp.body, 0, false)?;
            g.fixed(ir::instruction("vbar", vec![], vec![]));
            g.code.push(ir::Event::Close);
        } else {
            for &d in &c.loads {
                let d = &c.descs[d];
                g.fixed(
                    ir::memory(
                        ir::Memory::Fill,
                        Term::name(format!("a{}", d.ad)),
                        d.buf[0],
                        None,
                        true,
                        vec![],
                    )
                    .unwrap(),
                );
            }
            let (passes, rest) = (c.trips.len() / 2, c.trips.len() % 2);
            if passes > 0 {
                let reg = format!("s{}", t.loop_s());
                g.fixed(ir::seti(&reg, Term::int(passes as i128)));
                g.fixed(ir::instruction("loop", vec![Term::name(&reg)], vec![]));
                g.code.push(ir::Event::Open);
                c.iteration_typed(&mut g, &lp.body, 0, true)?;
                c.iteration_typed(&mut g, &lp.body, 1, rest != 0 || passes > 1)?;
                g.code.push(ir::Event::Close);
            }
            if rest != 0 {
                c.iteration_typed(&mut g, &lp.body, 0, false)?;
            }
        }
    }
    g.fixed(ir::instruction("halt", vec![], vec![]));
    Ok((c, g))
}

// Resolved events are supplied by the caller; allocation and banks are separate passes.
pub(in crate::lower::vector) fn image(
    c: &Ctx,
    name: &str,
    events: &[ir::Event],
) -> ir::Result<(Module, Vec<String>, Vec<Access>)> {
    let mem: Vec<&Desc> = c.descs.iter().filter(|d| d.to.is_empty()).collect();
    let params = mem
        .iter()
        .map(|d| format!("{}+{}", d.buffer.as_deref().unwrap(), d.off))
        .collect();
    let names = mem.iter().map(|d| format!("p_a{}", d.ad)).collect();
    let mut stmts = Vec::new();
    for d in &c.descs {
        let base = if d.to.is_empty() {
            Term::name(format!("p_a{}", d.ad))
        } else {
            ir::peer(d.off, d.count)
        };
        stmts.push(ir::desc(&format!("a{}", d.ad), base, &d.walk.0)?);
    }
    for d in &c.descs {
        if d.inc != 0 || c.classed {
            stmts.push(ir::ainc(&format!("a{}", d.ad), d.inc));
        }
    }
    stmts.extend(ir::nest(events)?);
    Ok((
        print::image(name, names, stmts, c.body.vregs)?,
        params,
        c.accesses(),
    ))
}
