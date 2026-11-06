//! Typed candidate construction; legacy generation is an opt-in reference.

use super::*;
use crate::lower::vector::gen::{print, typed as ir};
use ir::Event;
use ktpu_l1::text::Term;
use std::collections::HashMap;
use std::sync::Arc;

#[allow(clippy::too_many_arguments)]
pub(super) fn lower_band(
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
    fits: &Fits,
) -> VResult<Cand> {
    crate::work::LOWERED.fetch_add(1, std::sync::atomic::Ordering::Relaxed);

    crate::lower::vector::ctx::band::typed::lower_band(
        body, par, env, name, t, band, remat, ahead, plan, land, hold, pin, fits,
    )
    .map(|(m, p, a)| (Arc::new(m), p, a))
}

fn rebuilt(event: &Event) -> bool {
    match event {
        Event::Op { .. } => true,
        Event::Fixed(st) => match st.op.as_str() {
            "vbar" | "vdrain" => true,
            "vsync" => matches!((st.args.first(), st.kw("on")),
                (Some(Term::Name(w)), Some(Term::Name(on)))
                if (w == "pack" && on == "drain") || (w == "drain" && on == "pack")),
            _ => false,
        },
        _ => false,
    }
}

fn image(
    c: &Ctx,
    name: &str,
    head: &[Event],
    tail: &[Event],
    pinned: &HashMap<String, i64>,
) -> Module {
    let mut body = vec![];
    for d in &c.descs {
        body.push(
            ir::desc(
                &format!("a{}", d.ad),
                Term::name(format!("p_a{}", d.ad)),
                &d.walk.0,
            )
            .unwrap(),
        );
    }
    for d in &c.descs {
        if d.inc != 0 {
            body.push(ir::ainc(&format!("a{}", d.ad), d.inc));
        }
    }
    let mut events = vec![];
    for e in head {
        match e {
            Event::Op { stmt, .. } => {
                let mut st = stmt.clone();
                MO::emit_typed::resolve(&mut st, &|s| pinned[s]);
                events.push(Event::Fixed(st));
            }
            _ => events.push(e.clone()),
        }
    }
    events.extend_from_slice(tail);
    if c.checked {
        events.extend(
            super::super::checked::fold(c.t)
                .into_iter()
                .map(Event::Fixed),
        );
    }
    events.push(Event::Fixed(ir::instruction("halt", vec![], vec![])));
    body.extend(ir::nest(events).unwrap());
    let names = c
        .descs
        .iter()
        .filter(|d| d.to.is_empty())
        .map(|d| format!("p_a{}", d.ad))
        .collect();
    print::image(name, names, body, c.body.vregs).unwrap()
}

#[allow(clippy::too_many_arguments)]
pub(super) fn modulo(
    body: &Body,
    par: &RPar,
    env: &Env,
    name: &str,
    plan: &Plan,
    land: Option<i64>,
    t: &Target,
) -> VResult<std::result::Result<Cand, String>> {
    modulo_module(body, par, env, name, plan, land, t)
}

#[allow(clippy::too_many_arguments)]
fn modulo_module(
    body: &Body,
    par: &RPar,
    env: &Env,
    name: &str,
    plan: &Plan,
    land: Option<i64>,
    t: &Target,
) -> VResult<std::result::Result<Cand, String>> {
    let no = |w: &str| Ok(Err(w.to_string()));
    let (pre, lp) = split(body, par)?;
    let Some(lp) = lp else {
        return no("no stream loop");
    };
    let ops = ops_of(&lp.body);
    if lp.body.iter().any(|o| matches!(o.as_ref(), Node::If(_))) {
        return no("a sticky block");
    }
    if ops
        .iter()
        .chain(&pre)
        .any(|o| o.op_name() == "recv" || o.op_name() == "send")
    {
        return no("a cluster stream");
    }
    let mut c = Ctx::new(body, t, env, Some(lp.clone()))?;
    c.ahead = true;
    c.band = 1 << 10;
    c.remat = false;
    c.plan = plan.clone();
    c.land = land.unwrap_or(t.l1_words);
    c.unit = crate::lower::vector::unit_of(body, par, env)?;
    c.pre = pre.clone();
    c.guards = par.guards.clone();
    c.zero_sign = par.zero_sign;
    c.checked = super::super::checked::holds(&pre) || super::super::checked::holds(&ops);
    let regs = super::super::checked::registers(body.vregs, c.checked);
    for n in &lp.body {
        if matches!(n.as_ref(), Node::For(_)) {
            return no("a nested loop");
        }
        c.prepare(n, true)?;
    }
    if c.descs.iter().any(|d| d.mapped.is_some()) {
        return no("logical loads use descriptor-step band scheduling");
    }
    if c.rel.is_some() || c.descs.iter().any(|d| d.kind == "send") {
        return no("a banded unpack or a send");
    }
    if c.descs.iter().any(|d| d.shared.is_some()) {
        return no("a store packed over a load (no L1 room for its own ring)");
    }
    if c.descs.iter().any(|d| d.twin.is_some()) {
        return no("a value stored through several views (one ring a view)");
    }
    let packed: Vec<_> = ops
        .iter()
        .filter(|o| o.op_name() == "pack")
        .filter_map(|o| o.target().map(str::to_string))
        .collect();
    if ops.iter().any(|o| {
        let Node::Op(x) = o.as_ref() else {
            return false;
        };
        x.op == "store"
            && !matches!(x.st.positional().get(2), Some(Term::Name(n)) if packed.contains(n))
    }) {
        return no("a store of loaded bytes");
    }
    if ops.iter().any(|o| {
        let Node::Op(x) = o.as_ref() else {
            return false;
        };
        x.op == "unpack"
            && matches!(x.st.positional().first(), Some(Term::Name(n)) if packed.contains(n))
    }) {
        // The schedule orders L1 only through its rings, not pack to unpack.
        return no("a packed value read back in the trip");
    }
    c.down = c.land;
    for n in &pre {
        c.prepare(n, false)?;
    }
    let (events, head_len, pinned_syms) = match c.clone().modulo_typed() {
        Err(VErr::Pressure) => return no("one trip in one band exceeds the registers"),
        other => other?,
    };
    let core = MO::Core::of(t);
    let placed = banks::typed::rotate(events, body.vcopies, &core);
    let (head, trip) = placed.split_at(head_len);
    for e in trip {
        if !rebuilt(e) {
            let Event::Fixed(st) = e else {
                unreachable!("generated modulo block edge")
            };
            let text = print::emit(&mut [st.clone()]).unwrap();
            return Ok(Err(format!(
                "a raw line the emission does not rebuild: {}",
                text.trim_end_matches('\n')
            )));
        }
    }
    let mut pinned_v: Vec<_> = pinned_syms.iter().collect();
    pinned_v.sort_by_key(|s| s[1..].parse::<i64>().unwrap_or(0));
    let pinned: HashMap<_, _> = pinned_v
        .iter()
        .enumerate()
        .map(|(r, s)| ((*s).clone(), r as i64))
        .collect();
    let mut mops = vec![];
    let mut stmts = vec![];
    for e in trip {
        if let Event::Op { stmt, syms } = e {
            mops.push(MO::derive(stmt, syms, body.vcopies, &core));
            stmts.push(stmt.clone());
        }
    }
    if mops.is_empty() {
        return no("no vector ops in the trip");
    }
    let streamed: Vec<_> = (0..c.descs.len())
        .filter(|&i| c.loads.contains(&i) || c.descs[i].kind == "store")
        .collect();
    let per_iter: i64 = streamed.iter().map(|&i| c.descs[i].words).sum();
    let k_max = 8.min(2 + (c.land - c.top) / per_iter.max(1));
    let lower = ['M', 'U', 'P']
        .iter()
        .map(|e| {
            mops.iter()
                .filter(|o| o.eng == *e)
                .map(|o| o.occ)
                .sum::<i64>()
        })
        .max()
        .unwrap();
    let ii_max = 4 * lower + 400;
    let lead = MO::lead(c.loads.iter().map(|&i| c.descs[i].words).sum(), body.fill);
    let params: Vec<_> = c
        .descs
        .iter()
        .filter(|d| d.to.is_empty())
        .map(|d| format!("{}+{}", d.buffer.as_deref().unwrap(), d.off))
        .collect();
    let trips = c.trips.len() as i64;
    let mut ii_min = 1;
    let mut tried = vec![];
    loop {
        let Some(p) = MO::plan(
            &mops,
            regs as i64,
            &pinned,
            k_max,
            ii_max,
            lead,
            ii_min,
            &core,
        ) else {
            let list = if tried.is_empty() {
                "none planned".to_string()
            } else {
                tried.join(", ")
            };
            return Ok(Err(format!("no II up to {ii_max} colours in {regs} registers (K <= {k_max}) with an image in IMEM: {list}")));
        };
        let mut top = c.top;
        let mut rings = vec![];
        for &i in &streamed {
            let d = &c.descs[i];
            let own: Vec<_> = d.buf.iter().take(p.k as usize).copied().collect();
            let extra: Vec<_> = (0..p.k - own.len() as i64)
                .map(|j| top + j * d.words)
                .collect();
            top += d.words * extra.len() as i64;
            rings.push(MO::Ring {
                ad: d.ad,
                kind: if d.kind == "load" { "load" } else { "store" },
                base: d.buf[0],
                words: d.words,
                slots: own.into_iter().chain(extra).collect(),
            });
        }
        let mut best: Option<(Arc<Module>, i64)> = None;
        let mut overflow = false;
        for tail in MO::TAILS {
            let emitted = MO::emit_typed::emit(&p, &stmts, &rings, trips, &pinned, tail)
                .map_err(|e| VErr::Fail(body.reader.fail(&par.st, "L2.VC.014", e)))?;
            let (events, cycles) = match emitted {
                Some((events, cycles)) if top <= c.land => (events, cycles),
                other => {
                    let what = if other.map(|x| !x.0.is_empty()).unwrap_or(false) {
                        "L1"
                    } else {
                        "no loop body"
                    };
                    tried.push(format!("II {} K {} {tail}: {what}", p.ii, p.k));
                    continue;
                }
            };
            let module = Arc::new(image(&c, name, head, &events, &pinned));
            let thrown = match on("l2.vector/modulo-unthrottled") {
                true => std::borrow::Cow::Borrowed(&*module),
                false => throttled(&module, t)?,
            };
            let encoded = encode::words_of(&thrown, name, params.len(), &t.machine);
            match encoded {
                Err(e) => {
                    tried.push(format!("II {} K {} {tail}: {}", p.ii, p.k, first_line(&e)));
                    overflow = true;
                }
                Ok(words) => {
                    tried.push(format!(
                        "II {} K {} S {} {tail}: {words} words",
                        p.ii, p.k, p.stages
                    ));
                    if words as i64 <= IMEM_ROOM
                        && best.as_ref().map(|b| cycles < b.1).unwrap_or(true)
                    {
                        best = Some((module, cycles));
                    }
                }
            }
        }
        if let Some((module, _)) = best {
            c.top = top;
            return Ok(Ok((module, params, c.accesses())));
        }
        if overflow && p.k <= 2 {
            return Ok(Err(format!(
                "two iterations overflow the loop body: {}",
                tried.join(", ")
            )));
        }
        ii_min = p.ii + 1.max(p.ii / 16);
    }
}
