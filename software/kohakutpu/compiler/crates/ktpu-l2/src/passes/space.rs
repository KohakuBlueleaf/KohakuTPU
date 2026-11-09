//! `space` (L2.PASS.004): an `@any` scratch tile the clusters of one `par`
//! drain and only the cores of the next read back, a band a trip, becomes a
//! transfer straight into those cores' L1 (`drain -> vc[..]`, `recv`) when
//! every core reads exactly one cluster's tile and it fits the landing pool;
//! else, like `@mem`, staging when it fits the staging room, else DRAM.

use std::collections::{BTreeMap, BTreeSet};

use ktpu_l1::text::{Stmt, Term};

use super::{body_of, head, subst, unit_of, views, walk, write};
use crate::ir::read::{bind, instance_scopes, instances};
use crate::ir::{span, value, Body, Buffer, Instance, Node, Program, Result, StageX};
use crate::lower::memory as M;
use crate::target::{Target, POOL};

fn space_of(s: &Stmt) -> Option<String> {
    if s.op != "buffer" {
        return None;
    }
    match &s.annot {
        Some(Term::Placed(_, sp)) => match sp.as_ref() {
            Term::Call(n, _) | Term::Name(n) => Some(n.clone()),
            _ => None,
        },
        _ => None,
    }
}

/// `s`'s annotation with its space named `to`.
fn respaced(s: &Stmt, to: &str) -> Stmt {
    let mut out = s.clone();
    if let Some(Term::Placed(x, sp)) = &s.annot {
        let new = match sp.as_ref() {
            Term::Call(_, args) => Term::Call(to.into(), args.clone()),
            _ => Term::name(to),
        };
        out.annot = Some(Term::Placed(x.clone(), Box::new(new)));
    }
    out
}

/// Physical users of implicit scratch, after node scopes are instantiated.
fn scratch_nodes(body: &Body) -> Result<BTreeMap<String, BTreeSet<i64>>> {
    fn names(t: &Term, buffers: &BTreeSet<String>, out: &mut BTreeSet<String>) {
        match t {
            Term::Name(n) => {
                out.insert(n.clone());
            }
            Term::View(n, axes) => {
                out.insert(n.clone());
                for t in axes {
                    names(t, buffers, out);
                }
            }
            Term::Neg(x) | Term::Not(x) | Term::Pos(x) | Term::Typed(_, x) | Term::Kw(_, x) => {
                names(x, buffers, out);
            }
            Term::Offset(a, b)
            | Term::BinOp(_, a, b)
            | Term::Placed(a, b)
            | Term::Compare(_, a, b)
            | Term::AddAssign(a, b)
            | Term::Span(a, b)
            | Term::Assign(a, b)
            | Term::Member(a, b) => {
                names(a, buffers, out);
                names(b, buffers, out);
            }
            Term::Computed(n, index, axes) => {
                out.extend(
                    buffers
                        .iter()
                        .filter(|name| name.starts_with(n.as_str()))
                        .cloned(),
                );
                names(index, buffers, out);
                for t in axes.iter().flatten() {
                    names(t, buffers, out);
                }
            }
            Term::Call(_, args) | Term::Tuple(args) => {
                for t in args {
                    names(t, buffers, out);
                }
            }
            Term::Slice(lo, hi) => {
                for t in lo.iter().chain(hi) {
                    names(t, buffers, out);
                }
            }
            _ => {}
        }
    }
    fn scan(node: &Node, buffers: &BTreeSet<String>, out: &mut BTreeSet<String>) {
        for arg in &node.st().args {
            names(arg, buffers, out);
        }
        for child in node.body() {
            scan(child, buffers, out);
        }
    }
    let buffers = body.scratch.keys().cloned().collect();
    let mut out: BTreeMap<String, BTreeSet<i64>> = BTreeMap::new();
    for stage in instance_scopes(body)? {
        let mut used = BTreeSet::new();
        match &stage.x {
            StageX::Op(node, _) => scan(node, &buffers, &mut used),
            StageX::Par(parts) => {
                for part in parts {
                    scan(&part.par, &buffers, &mut used);
                }
            }
        }
        for name in used {
            if body.scratch.contains_key(&name) {
                out.entry(name).or_default().insert(stage.node);
            }
        }
    }
    Ok(out)
}

pub fn space(p: &Program, t: &Target) -> Result<Program> {
    if crate::mutant::on("l2.passes/space-off") {
        return Ok(p.clone());
    }
    let (params, mut stmts) = body_of(p)?;
    let anys: Vec<String> = stmts
        .iter()
        .filter(|s| space_of(s).as_deref() == Some("any"))
        .filter_map(|s| s.target_name().map(str::to_string))
        .collect();
    for h in anys {
        if let Some(got) = direct(p, &params, &stmts, &h, t)? {
            stmts = got
                .into_iter()
                .filter(|s| !(s.target_name() == Some(h.as_str()) && s.op == "buffer"))
                .collect();
        }
    }
    let owners = scratch_nodes(&p.body()?)?;
    let r = p.module.reader();
    // Size follows the packing layout independently of the memory placement.
    let nbytes = |s: &Stmt| -> Result<i64> {
        let at = if crate::mutant::on("l2.passes/space-layout-dropped") {
            let Some(Term::Placed(x, _)) = &s.annot else {
                unreachable!()
            };
            Term::Placed(x.clone(), Box::new(Term::name("dram")))
        } else {
            s.annot.clone().expect("a scratch buffer is placed")
        };
        let b = Buffer::of(&at).map_err(|e| r.fail(s, "L2.PASS.004", e))?;
        Ok(b.nbytes(t))
    };
    // The one physical node that touches a scratch buffer, when there is one.
    let owner = |s: &Stmt| {
        s.target_name()
            .and_then(|name| owners.get(name))
            .filter(|nodes| nodes.len() == 1)
            .and_then(|nodes| nodes.first())
            .copied()
    };
    // Each physical node owns one staging window. A staging buffer that names
    // no node lives in the window of its one physical user (node 0 otherwise).
    let mut used = BTreeMap::<i64, i64>::new();
    let mut unowned = BTreeSet::new();
    for s in stmts
        .iter()
        .filter(|s| space_of(s).as_deref() == Some("stage"))
    {
        let buffer = Buffer::of(s.annot.as_ref().expect("placed scratch"))
            .map_err(|e| r.fail(s, "L2.PASS.004", e))?;
        let node = match buffer.layout.get("node") {
            Some(value) => value.int(),
            None => {
                if let Some(name) = s.target_name() {
                    unowned.insert(name.to_string());
                }
                owner(s).unwrap_or(0)
            }
        };
        *used.entry(node).or_default() += nbytes(s)?;
    }
    let mut out = Vec::new();
    for s in stmts {
        let sp = space_of(&s);
        if sp.as_deref() == Some("stage")
            && s.target_name().is_some_and(|name| unowned.contains(name))
        {
            let node = owner(&s).unwrap_or(0);
            let mut placed = s.clone();
            if let Some(Term::Placed(_, space)) = &mut placed.annot {
                let at = Term::Assign(
                    Box::new(Term::name("node")),
                    Box::new(Term::int(node as i128)),
                );
                match space.as_mut() {
                    Term::Call(_, args) => args.push(at),
                    space => *space = Term::Call("stage".into(), vec![at]),
                }
            }
            out.push(placed);
        } else if sp.as_deref() == Some("mem") || sp.as_deref() == Some("any") {
            let size = nbytes(&s)?;
            let node = owner(&s).unwrap_or(0);
            let room = used.entry(node).or_default();
            let fits = *room + size <= t.stage_room();
            *room += if fits { size } else { 0 };
            let mut placed = respaced(&s, if fits { "stage" } else { "dram" });
            if fits {
                if let Some(Term::Placed(_, space)) = &mut placed.annot {
                    let owner = Term::Assign(
                        Box::new(Term::name("node")),
                        Box::new(Term::int(node as i128)),
                    );
                    match space.as_mut() {
                        Term::Call(_, args) => args.push(owner),
                        space => *space = Term::Call("stage".into(), vec![owner]),
                    }
                }
            }
            out.push(placed);
        } else {
            out.push(s);
        }
    }
    write(p, &params, &out)
}

/// The statements with scratch `h` turned into cluster -> core transfers, or
/// None where the shapes do not allow it.
fn direct(
    p: &Program,
    params: &[(String, Term)],
    stmts: &[Stmt],
    h: &str,
    t: &Target,
) -> Result<Option<Vec<Stmt>>> {
    let as_dram: Vec<Stmt> = stmts
        .iter()
        .map(|s| {
            if s.op == "buffer" && s.target_name() == Some(h) {
                respaced(s, "dram")
            } else {
                s.clone()
            }
        })
        .collect();
    let Some((p1, p2, drain, load)) = drain_load_pair(&as_dram, h) else {
        return Ok(None);
    };
    let Ok(prog) = write(p, params, &as_dram) else {
        return Ok(None);
    };
    let Ok(body) = prog.body() else {
        return Ok(None);
    };
    let Some(tile) = match_tile(&body, h, t) else {
        return Ok(None);
    };
    let var1 = head(&p1).unwrap().0;
    let var2 = head(&p2).unwrap().0;
    let unit_ix = |s: &Stmt| match s.positional()[2] {
        Term::View(_, sl) => sl[0].clone(),
        _ => unreachable!(),
    };
    let (unit1, unit2) = (unit_ix(&p1), unit_ix(&p2));
    let lo1 = match drain.positional()[2] {
        Term::View(_, sl) => match sl.first() {
            Some(Term::Slice(Some(lo), _)) => (**lo).clone(),
            _ => Term::int(0),
        },
        _ => Term::int(0),
    };
    let name_l1 = format!("{h}_l1");
    let out = walk(stmts, &mut |s: Stmt, _scopes: &[Stmt]| {
        if s.op == "drain" && s.positional().get(2).and_then(|t| views(t)) == Some(h) {
            let to = Term::View(
                "vc".into(),
                vec![subst(&unit2, &[(var2.clone(), Term::name(var1.clone()))])],
            );
            let mut out = s.clone();
            let mut args: Vec<Term> = s.args.iter().take(2).cloned().collect();
            args.push(to);
            args.extend(s.args.iter().skip(3).cloned());
            out.args = args;
            return Ok(vec![out]);
        }
        if s.op != "par" || unit_of(&s).as_deref() != Some("vc") {
            return Ok(vec![s]);
        }
        let Some(li) = s.body.iter().position(|b| b.op == "for") else {
            return Ok(vec![s]);
        };
        let lp = &s.body[li];
        let Some(ldi) = lp
            .body
            .iter()
            .position(|b| b.op == "load" && b.line == load.line)
        else {
            return Ok(vec![s]);
        };
        let ld = &lp.body[ldi];
        let (rlo, rhi) = match ld.positional()[0] {
            Term::View(_, sl) => match &sl[0] {
                Term::Slice(lo, hi) => (lo.clone(), hi.clone()),
                _ => return Ok(vec![s]),
            },
            _ => return Ok(vec![s]),
        };
        let rel = Term::BinOp(
            '-',
            Box::new(rlo.map(|x| *x).unwrap_or(Term::name("None"))),
            Box::new(subst(&lo1, &[(var1.clone(), Term::name(var2.clone()))])),
        );
        let band = Term::View(
            name_l1.clone(),
            vec![
                Term::Slice(Some(Box::new(rel)), rhi),
                Term::Slice(None, None),
            ],
        );
        let ld_target = ld.target_name().map(str::to_string);
        let mut body = Vec::new();
        for (k, b) in lp.body.iter().enumerate() {
            if k == ldi {
                continue;
            }
            let mut b = b.clone();
            let pos = b.positional();
            if b.op == "unpack"
                && pos.len() == 1
                && pos[0].as_name().map(str::to_string) == ld_target
            {
                b.args = vec![band.clone()];
            }
            body.push(b);
        }
        let mut recv = Stmt::new(
            "recv",
            vec![Term::View(
                "mg".into(),
                vec![subst(&unit1, &[(var1.clone(), Term::name(var2.clone()))])],
            )],
        );
        recv.target = Some(Term::name(name_l1.clone()));
        recv.annot = Some(Term::Placed(
            Box::new(Term::View(
                "f16".into(),
                vec![Term::int(tile.0 as i128), Term::int(tile.1 as i128)],
            )),
            Box::new(Term::Call("l1".into(), vec![Term::name("gt4")])),
        ));
        recv.line = ld.line;
        let mut out = s.clone();
        let mut newloop = lp.clone();
        newloop.body = body;
        let mut rest: Vec<Stmt> = vec![recv];
        for (k, b) in s.body.iter().enumerate() {
            rest.push(if k == li { newloop.clone() } else { b.clone() });
        }
        out.body = rest;
        Ok(vec![out])
    })?;
    Ok(Some(out))
}

/// `(cluster par, core par, the drain, the load)` when `h` is drained by one
/// statement of one cluster `par`, loaded by one statement of the stream loop
/// of the vector `par` right after it, and touched by nothing else.
fn drain_load_pair(stmts: &[Stmt], h: &str) -> Option<(Stmt, Stmt, Stmt, Stmt)> {
    struct Found<'a> {
        drain: Vec<(&'a Stmt, Option<&'a Stmt>)>,
        load: Vec<(&'a Stmt, Vec<&'a Stmt>)>,
        other: usize,
        next: Vec<(*const Stmt, Option<*const Stmt>)>,
    }
    fn mentions(s: &Stmt, h: &str) -> bool {
        fn go(t: &Term, h: &str) -> bool {
            match t {
                Term::View(n, sl) => n == h || sl.iter().any(|x| go(x, h)),
                Term::Name(n) => n == h,
                Term::Int(..)
                | Term::Float(_)
                | Term::Dims(_)
                | Term::Str(_)
                | Term::NewAxis
                | Term::Arrow(_) => false,
                Term::Neg(x) | Term::Not(x) | Term::Pos(x) | Term::Typed(_, x) | Term::Kw(_, x) => {
                    go(x, h)
                }
                Term::Offset(a, b)
                | Term::BinOp(_, a, b)
                | Term::Placed(a, b)
                | Term::Compare(_, a, b)
                | Term::AddAssign(a, b)
                | Term::Span(a, b)
                | Term::Assign(a, b)
                | Term::Member(a, b) => go(a, h) || go(b, h),
                Term::Computed(_, i, sl) => go(i, h) || sl.iter().flatten().any(|x| go(x, h)),
                Term::Call(_, a) | Term::Tuple(a) => a.iter().any(|x| go(x, h)),
                Term::Slice(lo, hi) => lo.iter().any(|x| go(x, h)) || hi.iter().any(|x| go(x, h)),
            }
        }
        s.args.iter().any(|a| go(a, h))
    }
    fn scan<'a>(sts: &'a [Stmt], scopes: &mut Vec<&'a Stmt>, h: &str, f: &mut Found<'a>) {
        for (i, s) in sts.iter().enumerate() {
            if s.op == "par" && unit_of(s).as_deref() == Some("mg") {
                let nxt = sts.get(i + 1).map(|n| n as *const Stmt);
                f.next.push((s as *const Stmt, nxt));
            }
            let reads = mentions(s, h);
            if s.op == "drain" && reads {
                f.drain.push((s, scopes.last().copied()));
            } else if s.op == "load" && reads {
                f.load.push((s, scopes.clone()));
            } else if reads {
                f.other += 1;
            }
            scopes.push(s);
            scan(&s.body, scopes, h, f);
            scopes.pop();
        }
    }
    let mut found = Found {
        drain: vec![],
        load: vec![],
        other: 0,
        next: vec![],
    };
    scan(stmts, &mut Vec::new(), h, &mut found);
    if found.other > 0 || found.drain.len() != 1 || found.load.len() != 1 {
        return None;
    }
    let (drain, p1) = found.drain[0];
    let (load, scopes) = &found.load[0];
    let p1 = p1?;
    if p1.op != "par" || unit_of(p1).as_deref() != Some("mg") || scopes.len() < 2 {
        return None;
    }
    let (lp, p2) = (scopes[scopes.len() - 1], scopes[scopes.len() - 2]);
    if lp.op != "for" || p2.op != "par" || unit_of(p2).as_deref() != Some("vc") {
        return None;
    }
    let nxt = found
        .next
        .iter()
        .rev()
        .find(|(k, _)| std::ptr::eq(*k, p1))
        .and_then(|(_, n)| *n);
    if nxt != Some(p2 as *const Stmt) {
        return None;
    }
    Some((p1.clone(), p2.clone(), drain.clone(), (*load).clone()))
}

/// The tile's `(rows, cols)` when, in every stage pair, the cluster instance
/// with each `par` value drains one tile of `h` and the core instance with
/// that value loads exactly its row bands in order; the tile fits the pool.
fn match_tile(body: &Body, h: &str, t: &Target) -> Option<(i64, i64)> {
    let stages: Vec<Vec<Instance>> = instances(body)
        .ok()?
        .into_iter()
        .filter_map(|s| match s.x {
            StageX::Par(x) => Some(x),
            _ => None,
        })
        .collect();
    let mut tile: Option<(i64, i64)> = None;
    let mut pairs = 0;
    for w in stages.windows(2) {
        let drains = drained(body, &w[0], h)?;
        if drains.is_empty() {
            continue;
        }
        let loads = loaded(body, &w[1])?;
        let dk: BTreeSet<&i64> = drains.keys().collect();
        let lk: BTreeSet<&i64> = loads.keys().collect();
        if dk != lk {
            return None;
        }
        for (v, (region, shape)) in &drains {
            if loads[v] != *region {
                return None;
            }
            if tile.is_some() && tile != Some(*shape) {
                return None;
            }
            tile = Some(*shape);
        }
        pairs += 1;
    }
    let tile = tile?;
    let pool = if crate::mutant::on("l2.lower/pool-128") {
        128
    } else {
        POOL
    };
    if pairs == 0 || tile.0 * tile.1 / t.f16_word() > pool {
        return None;
    }
    Some(tile)
}

type Region = (i64, i64, i64, i64);

/// A drained tile: its region and `(rows, cols)`.
type Drain = (Region, (i64, i64));

/// `{par value: drain}` of the cluster instances draining into `h`; None
/// where a drained view does not evaluate (an empty map: none drain, or not
/// clusters).
fn drained(body: &Body, insts: &[Instance], h: &str) -> Option<BTreeMap<i64, Drain>> {
    let mut out = BTreeMap::new();
    for i in insts {
        if i.kind != "mg" {
            return Some(BTreeMap::new());
        }
        for (n, env) in &i.items {
            let Node::Op(op) = n.as_ref() else { continue };
            let pos = op.st.positional();
            if op.op == "drain" && pos.get(2).and_then(|t| views(t)) == Some(h) {
                let v = value(body, &op.st, pos[2], env).ok()?;
                let Some(ax) = axes2(body, &v) else {
                    return Some(BTreeMap::new());
                };
                let (r0, r, c0, c) = (ax[0].lo, ax[0].size, ax[1].lo, ax[1].size);
                let pv = crate::ir::read::env_int(env, &i.par().var).unwrap_or(0);
                out.insert(pv, ((r0, r, c0, c), (r, c)));
            }
        }
    }
    Some(out)
}

/// `{par value: region}` the core instances load over their stream loops,
/// each trip's band right after the last; None if some load is not such a band.
fn loaded(body: &Body, insts: &[Instance]) -> Option<BTreeMap<i64, Region>> {
    let mut out = BTreeMap::new();
    for i in insts {
        if i.kind != "vc" {
            return None;
        }
        let lp = i.par().body.iter().find_map(|s| match s.as_ref() {
            Node::For(f) => Some(f.clone()),
            _ => None,
        })?;
        let ld = lp.body.iter().find_map(|s| match s.as_ref() {
            Node::Op(o) if o.op == "load" => Some(o.clone()),
            _ => None,
        })?;
        let mut bands = Vec::new();
        for tt in span(body, &lp.st, &lp.lo, &lp.hi, &i.env).ok()? {
            let v = value(
                body,
                &ld.st,
                ld.st.positional()[0],
                &bind(&i.env, &lp.var, tt),
            )
            .ok()?;
            bands.push(axes2(body, &v)?);
        }
        if bands.is_empty() {
            return None;
        }
        let (r0, r, c0, c) = (
            bands[0][0].lo,
            bands[0][0].size,
            bands[0][1].lo,
            bands[0][1].size,
        );
        for (k, b) in bands.iter().enumerate() {
            if (b[0].lo, b[0].size, b[1].lo, b[1].size) != (r0 + k as i64 * r, r, c0, c) {
                return None;
            }
        }
        let pv = crate::ir::read::env_int(&i.env, &i.par().var).unwrap_or(0);
        out.insert(pv, (r0, r * bands.len() as i64, c0, c));
    }
    Some(out)
}

fn axes2(body: &Body, v: &Term) -> Option<Vec<M::Ax>> {
    let Term::View(n, _) = v else { return None };
    let buf = body.buffers.get(n)?;
    if buf.shape.len() != 2 {
        return None;
    }
    M::axes_of(v, &buf.shape).ok()
}
