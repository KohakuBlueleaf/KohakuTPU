//! The band lowering of one instance and its image text (L2.VC.001, .005).

use super::*;

pub(in crate::lower::vector) mod typed;

/// The band lowering of one instance: `(image text, parameters, accesses)`.
#[allow(clippy::too_many_arguments)]
#[cfg(feature = "witness")]
pub fn lower_band(
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
) -> VResult<(String, Vec<String>, Vec<Access>)> {
    let (pre, lp) = super::super::split(body, par)?;
    let mut c = Ctx::new(body, t, env, lp.clone())?;
    c.classed = par.st.kw("classed").is_some();
    c.ahead = ahead;
    c.band = band;
    c.remat = remat;
    c.hold = hold;
    c.pin = pin;
    c.plan = plan.clone();
    c.land = land.unwrap_or(t.l1_words);
    c.unit = super::super::unit_of(body, par, env)?;
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
    let mut g = Gen::default();
    let mut all = pre.clone();
    if let Some(lp) = &lp {
        all.extend(ops_of(&lp.body));
    }
    if c.classed {
        g.raw(format!(
            "seti s{} = {} classed=1",
            t.loop_s(),
            c.trips.len()
        ));
    }
    if c.classed && !crate::mutant::on("l2.class/no-idle") {
        g.raw(format!("seti s{} = 1", t.stage_reg()));
        g.raw(format!("skipz s{}", t.loop_s()));
        g.raw("{");
        g.raw(format!("seti s{} = 0", t.stage_reg()));
        g.raw("}");
        g.raw(format!("skipz s{}", t.stage_reg()));
        g.raw("{");
        g.raw("halt");
        g.raw("}");
    }
    c.constants(&mut g, &all);
    if let Some(lp) = &lp {
        if lp.body.iter().any(|o| matches!(o.as_ref(), Node::If(_))) {
            g.raw(format!("getstk s{}", t.stk_s()));
        }
    }
    for n in &pre {
        c.prologue(&mut g, n)?;
    }
    if pre.iter().any(|n| n.op_name() == "unpack") {
        g.raw("vsync fe on=unpack");
    }
    if let Some((_, rname, first, _)) = &c.rel {
        g.raw(format!("uptr {}", c.vals[rname].buf + 8 * first));
        g.raw("uinc 8");
    }
    if let Some(lp) = &lp {
        if c.classed {
            let init_at = g.code.len();
            g.raw(format!("loop s{}", t.loop_s()));
            g.raw("{");
            g.raw("vsync fill on=drain");
            g.raw("vsync fill on=unpack");
            for &d in &c.loads {
                let d = &c.descs[d];
                g.raw(format!("vfill a{} -> l1[{}] rel", d.ad, d.buf[0]));
            }
            c.iteration(&mut g, &lp.body, 0, false)?;
            g.raw("vbar");
            g.raw("}");
            let mut init = Gen::default();
            for (acc, maximum) in c.carries.values() {
                if *maximum {
                    init.raw(format!("seti s{} = -inf", t.stage_reg()));
                    init.emit(format!("vmov {{0}}, s{}", t.stage_reg()), &[acc]);
                } else {
                    init.emit("vmov {0}, k0", &[acc]);
                }
            }
            if !on("l2.class/carry-init") {
                g.code.splice(init_at..init_at, init.code);
            }
        } else {
            for &d in &c.loads {
                let d = &c.descs[d];
                g.raw(format!("vfill a{} -> l1[{}] rel", d.ad, d.buf[0]));
            }
            let (passes, rest) = (c.trips.len() / 2, c.trips.len() % 2);
            if passes > 0 {
                g.raw(format!("seti s{} = {passes}", t.loop_s()));
                g.raw(format!("loop s{}", t.loop_s()));
                g.raw("{");
                c.iteration(&mut g, &lp.body, 0, true)?;
                c.iteration(&mut g, &lp.body, 1, rest != 0 || passes > 1)?;
                g.raw("}");
            }
            if rest != 0 {
                c.iteration(&mut g, &lp.body, 0, false)?;
            }
        }
    }
    g.raw("halt");
    #[cfg(test)]
    if !c.classed {
        typed::tests::compare(
            &c, &g, body, par, env, name, t, band, remat, ahead, plan, land, hold, pin,
        );
    }
    let code = banks::rotate(
        std::mem::take(&mut g.code),
        body.vcopies,
        &super::super::modulo::Core::of(t),
    );
    let lines = super::super::assign(&code, &c.pinned, body.vregs)?;
    Ok(image_text(&c, name, &lines))
}

/// The image text: its header, descriptors, increments and lines.
#[cfg(feature = "witness")]
pub fn image_text(c: &Ctx, name: &str, lines: &[String]) -> (String, Vec<String>, Vec<Access>) {
    let mem: Vec<&Desc> = c.descs.iter().filter(|d| d.to.is_empty()).collect();
    let params: Vec<String> = mem
        .iter()
        .map(|d| format!("{}+{}", d.buffer.as_deref().unwrap(), d.off))
        .collect();
    let mut head = vec![format!(
        "image {name}({})",
        mem.iter()
            .map(|d| format!("p_a{}", d.ad))
            .collect::<Vec<_>>()
            .join(", ")
    )];
    for d in &c.descs {
        let count = if d.count { ", count=1" } else { "" };
        let base = if !d.to.is_empty() {
            format!("peer({}{count})", d.off)
        } else {
            format!("p_a{}", d.ad)
        };
        head.push(format!("    desc a{} = {base} walk=({})", d.ad, d.walk));
    }
    for d in &c.descs {
        if d.inc != 0 || c.classed {
            head.push(format!("    ainc a{} += {}", d.ad, d.inc));
        }
    }
    let body: Vec<String> = indent(lines)
        .into_iter()
        .map(|l| format!("    {l}"))
        .collect();
    let text = head.into_iter().chain(body).collect::<Vec<_>>().join("\n") + "\n";
    (text, params, c.accesses())
}
