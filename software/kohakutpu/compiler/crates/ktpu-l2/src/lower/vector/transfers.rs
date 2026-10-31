//! A core instance's transfers: each `recv` from a cluster and each `send`
//! into one, plain or streamed (L2.VC.009, .010, .024).

use super::*;

fn unit_key(t: &Term) -> String {
    match t {
        Term::View(n, s) if s.len() == 1 => format!("{n}[{}]", s[0].as_int().unwrap_or(0)),
        other => ktpu_l1::text::fmt(other),
    }
}

/// `{op uid: transfer}` for each `recv` (the oldest transfer into this core
/// from that cluster) and each `send` (its landing, allocated now).
pub fn transfers(
    body: &Body,
    par: &RPar,
    env: &Env,
    links: &mut Links,
    t: &Target,
) -> Result<Plan> {
    let mut out = Plan::new();
    let me = unit_of(body, par, env)?;
    let (pre, lp) = split(body, par)?;
    let trips = match &lp {
        Some(f) => span(body, &f.st, &f.lo, &f.hi, env)?.len() as i64,
        None => 1,
    };
    let looped: Vec<(Arc<Node>, bool)> = pre
        .iter()
        .map(|o| (o.clone(), false))
        .chain(
            lp.iter()
                .flat_map(|f| f.body.iter().map(|o| (o.clone(), true))),
        )
        .collect();
    for (n, inloop) in looped {
        let Node::Op(op) = n.as_ref() else { continue };
        if op.op != "recv" && op.op != "send" {
            continue;
        }
        let pos = op.st.positional();
        if op.op == "recv" && inloop {
            let x = recv_stream(body, op, env, &me, trips, links, t)?;
            out.insert(op.uid, PlanX::S(x));
            continue;
        }
        if op.op == "recv" {
            let ty = match &op.ty {
                Some(ty) if ty.space == "l1" && ty.dtype == "f16" && pos.len() == 1 => ty.clone(),
                _ => {
                    return Err(body.reader.fail(
                        &op.st,
                        "L2.VC.009",
                        "NAME = recv mg[U] : f16[R, C] @l1(gt4)",
                    ))
                }
            };
            let src = value(body, &op.st, pos[0], env)?;
            if !matches!(&src, Term::View(n, s) if n == "mg" && s.len() == 1) {
                return Err(body.reader.fail(
                    &op.st,
                    "L2.VC.009",
                    "a core receives from a cluster: recv mg[U]",
                ));
            }
            let from = unit_key(&src);
            let x = match links.take(body, &op.st, &from, &me, "l1")? {
                Some(x) => x,
                None => {
                    let words = ty.shape[0] * ty.shape[1] / 16;
                    Transfer {
                        src: from.clone(),
                        dst: me.clone(),
                        side: "l1".into(),
                        at: t.l1_words - words,
                        size: words,
                        shape: ty.shape.clone(),
                        flow: false,
                        slots: vec![],
                        per: 0,
                    }
                }
            };
            if x.shape != ty.shape || mode_of(Some(&ty)) != "gt4" {
                return Err(body.reader.fail(
                    &op.st,
                    "L2.VC.009",
                    format!(
                        "the cluster sent f16{} sub-tiles: @l1(gt4)",
                        super::super::cluster::py_list(&x.shape)
                    ),
                ));
            }
            out.insert(op.uid, PlanX::T(x));
            continue;
        }
        let side_t = op.st.kw("side");
        let side = match side_t {
            None => Some("A".to_string()),
            Some(Term::Name(n)) => Some(n.clone()),
            Some(_) => None,
        };
        if pos.len() != 3
            || !matches!(pos[0], Term::Name(_))
            || !matches!(side.as_deref(), Some("A") | Some("B"))
        {
            return Err(body
                .reader
                .fail(&op.st, "L2.VC.024", "send VALUE -> mg[I] side=A|B"));
        }
        let Some(lp) = lp.as_ref().filter(|_| inloop) else {
            return Err(body
                .reader
                .fail(&op.st, "L2.VC.024", "send is in the stream loop"));
        };
        let dst = value(body, &op.st, pos[2], env)?;
        if !matches!(&dst, Term::View(n, s) if n == "mg" && s.len() == 1) {
            return Err(body.reader.fail(
                &op.st,
                "L2.VC.024",
                "a core sends into a cluster: send VALUE -> mg[I] side=A|B",
            ));
        }
        let name = pos[0].as_name().unwrap();
        let packed = ops_of(&lp.body)
            .into_iter()
            .find(|o| o.target() == Some(name));
        let pt = match packed.as_deref() {
            Some(Node::Op(p))
                if p.op == "pack"
                    && p.ty
                        .as_ref()
                        .map(|t| t.dtype == "mx7a" || t.dtype == "mx7b")
                        .unwrap_or(false) =>
            {
                p.ty.clone().unwrap()
            }
            _ => {
                return Err(body.reader.fail(
                    &op.st,
                    "L2.VC.024",
                    "a core sends a packed MXFP7 value into a cluster's bank",
                ))
            }
        };
        let (r, c) = (pt.shape[0], pt.shape[1]);
        let side = side.unwrap();
        let to = unit_key(&dst);
        if links.stream(&me, &to, &side).is_some() {
            let per = (r / 4) * (c / 32);
            if trips % 2 != 0 {
                return Err(body.reader.fail(
                    &op.st,
                    "L2.VC.024",
                    "a streamed send runs an even number of trips",
                ));
            }
            let mut xs = Vec::new();
            for _ in 0..trips {
                xs.push(links.send(body, &op.st, &me, &to, &side, per, &[r, c])?);
            }
            let slots = vec![xs[0].at, if trips > 1 { xs[1].at } else { xs[0].at }];
            if xs.iter().enumerate().any(|(k, x)| x.at != slots[k % 2]) {
                return Err(body.reader.fail(
                    &op.st,
                    "L2.VC.024",
                    format!("{to}'s ring takes two of these at a time"),
                ));
            }
            let distinct = if slots[0] == slots[1] { 1 } else { 2 };
            let mut x = xs[0].clone();
            x.at = slots[0].min(slots[1]);
            x.size = per * distinct;
            x.slots = slots;
            x.per = per;
            out.insert(op.uid, PlanX::T(x));
            continue;
        }
        let shape = [r * trips, c];
        let entries = (r / 4) * (c / 32) * trips;
        let x = links.send(body, &op.st, &me, &to, &side, entries, &shape)?;
        out.insert(op.uid, PlanX::T(x));
    }
    Ok(out)
}

/// `x = recv mg[U] : f16[R, C] @l1(gt4)` in the stream loop: each trip takes
/// the next tile cluster U streams into this core's ring.
fn recv_stream(
    body: &Body,
    op: &Op,
    env: &Env,
    me: &str,
    trips: i64,
    links: &mut Links,
    t: &Target,
) -> Result<StreamIn> {
    let pos = op.st.positional();
    let ty = match &op.ty {
        Some(ty) if ty.space == "l1" && ty.dtype == "f16" && pos.len() == 1 => ty.clone(),
        _ => {
            return Err(body.reader.fail(
                &op.st,
                "L2.VC.010",
                "NAME = recv mg[U] : f16[R, C] @l1(gt4)",
            ))
        }
    };
    if mode_of(Some(&ty)) != "gt4" {
        return Err(body
            .reader
            .fail(&op.st, "L2.VC.010", "a cluster streams sub-tiles: @l1(gt4)"));
    }
    let src = value(body, &op.st, pos[0], env)?;
    if !matches!(&src, Term::View(n, s) if n == "mg" && s.len() == 1) {
        return Err(body.reader.fail(
            &op.st,
            "L2.VC.010",
            "a core receives from a cluster: recv mg[U]",
        ));
    }
    let unit = unit_key(&src);
    let words = ty.shape[0] * ty.shape[1] / 16;
    if links.alone {
        let lo = t.l1_words - 2 * words;
        return Ok(StreamIn {
            src: unit,
            dst: me.to_string(),
            slots: vec![lo, lo + words],
            size: words,
            shape: ty.shape.clone(),
        });
    }
    if links.stream(&unit, me, "l1").is_none() {
        return Err(body.reader.fail(
            &op.st,
            "L2.VC.010",
            format!("{unit} streams nothing into {me}"),
        ));
    }
    if trips % 2 != 0 {
        return Err(body.reader.fail(
            &op.st,
            "L2.VC.010",
            "a streamed recv runs an even number of trips",
        ));
    }
    let mut xs = Vec::new();
    for _ in 0..trips {
        xs.push(links.take_stream(body, &op.st, &unit, me, "l1", words)?);
    }
    let slots = vec![xs[0].at, if trips > 1 { xs[1].at } else { xs[0].at }];
    if xs.iter().enumerate().any(|(k, x)| x.at != slots[k % 2]) {
        return Err(body.reader.fail(
            &op.st,
            "L2.VC.010",
            format!("{unit}'s ring into {me} holds two of these tiles"),
        ));
    }
    Ok(StreamIn {
        src: unit,
        dst: me.to_string(),
        slots,
        size: words,
        shape: ty.shape.clone(),
    })
}
