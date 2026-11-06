//! L2 -> L1: an `fn NAME.l2` body to L1 `.ktpu` text (L2.LOW, L2.ORDER).
//!
//! Each `par` of units is one stage: every instance's stream is lowered, cut
//! at its outermost `for` iterations, and sent segment by segment across the
//! instances; consecutive node-scope mover ops are one posted move. Stages are
//! then ordered by what they touch (`sync::schedule`). A `par` over nodes
//! gives each node its own `node N` block.

pub mod cluster;
pub mod events;
pub mod fills;
pub mod links;
pub mod memory;
pub mod mover;
pub mod readmap;
pub mod sync;
pub mod vector;

mod adds;
mod group;
mod nodes;
mod rings;
mod share;

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;
use ktpu_l1::text::{fmt, Stmt, Term, Val};

use crate::ir::read::instances;
use crate::ir::{const_of, value, Body, Instance, Node, Program, Result, StageX};
use crate::mutant::on;
use crate::target::Target;
use adds::{added, added_into, adds};
use cluster::{Line, ShareKey, Stream};
use group::{close, grouped};
use links::{Key, Links};
use nodes::{node_vars, order_nodes, remote_adds, stage_key};
use rings::{counts, open, spend, streams};
use share::shared;
use sync::{Access, Stage, FENCE};
use vector::Search;

const IND: &str = "    ";

/// L1 text for `fn name.l2` of `p`: the module's target, the images, then
/// `fn name.l1` with the L2 parameters and the scratch buffers (L2.LOW.001).
pub fn lower(p: &Program, t: &Target, search: &Search) -> Result<String> {
    lower_with_sync(p, t, search, &events::BackendSyncFacts::common(t.il_slots))
}

/// Lower for an explicitly selected backend slot ABI.
pub fn lower_with_sync(
    p: &Program,
    t: &Target,
    search: &Search,
    sync: &events::BackendSyncFacts,
) -> Result<String> {
    let mut body = p.body()?;
    let stages = instances(&body)?;
    let nodes: Vec<i64> = stages
        .iter()
        .map(|s| s.node)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let cores: HashSet<usize> = stages
        .iter()
        .filter_map(|s| match &s.x {
            StageX::Par(x) => Some(
                x.iter()
                    .filter(|i| i.kind == "vc")
                    .map(|i| i.par.uid())
                    .collect::<Vec<_>>(),
            ),
            _ => None,
        })
        .flatten()
        .collect();
    let shared = beside_clusters(&body.stmts, &cores, false) && !on("l2.lower/fill-alone");
    body.fill = t.mem(shared);
    let body = body;
    let multi = !node_vars(&body.stmts).is_empty();
    let land = if drains_to_cores(&body.stmts) {
        Some(t.l1_words - t.pool())
    } else {
        None
    };
    let node_vars = node_vars(&body.stmts);
    let remote = remote_adds(&body, &stages, &node_vars)?;
    let mut images: Vec<String> = Vec::new();
    let mut packing_status = Vec::new();
    let mut built_by: IndexMap<i64, Vec<Stage>> = IndexMap::new();
    let last_st = body.stmts.last().map(|s| s.st().clone());
    for &n in &nodes {
        let mine: Vec<&crate::ir::Stage> = stages.iter().filter(|s| s.node == n).collect();
        let mut links = Links::new(&streams(&body, &mine, t)?, t);
        let mut reading: HashMap<String, (Option<i64>, Option<i64>)> = HashMap::new();
        let mut built: Vec<Stage> = Vec::new();
        let mut pending = mover::quant_status::Pending::default();
        let end_st = last_st.as_ref().unwrap();
        if links.rings.values().any(|r| r.dst.starts_with("vc")) {
            built.push(open(&body.name, n, &links, &mut images));
        }
        for (i, s) in mine.iter().enumerate() {
            match &s.x {
                StageX::Par(x) => {
                    let key = stage_key(&node_vars, x);
                    let rem = remote.get(&(n, key)).cloned().unwrap_or_default();
                    let (lines, units, acc, segs, records) = par_stage(
                        &body,
                        x,
                        t,
                        &mut images,
                        &mut links,
                        land,
                        &mut reading,
                        &rem,
                        search,
                    )?;
                    pending.protect(&body, end_st, &acc)?;
                    for (s, write) in &records {
                        pending.claim(&body, &x[0].par().st, s)?;
                        pending.record(s, std::slice::from_ref(write));
                    }
                    let mut st = Stage::new(lines, units, acc);
                    st.group = s.tag;
                    st.segs = segs;
                    st.phase = s.phase;
                    built.push(st);
                }
                StageX::Op(node, env) => {
                    let op = node.as_op().unwrap();
                    if op.op == "quant_check" {
                        let args = op.st.positional();
                        if args.len() != 1 {
                            return Err(body.reader.fail(
                                &op.st,
                                "L2.MOV.019",
                                "quant_check STATUS",
                            ));
                        }
                        let region = mover::quant_status::region(&body, &op.st, args[0], env, t)?;
                        let mut stage = pending.check(&body, &op.st, &region)?;
                        stage.group = s.tag;
                        stage.phase = s.phase;
                        built.push(stage);
                        continue;
                    }
                    if op.op == "quant" && mover::quant_backend(&body, op, t)? {
                        let checked = op.st.kw("finite").and_then(Term::as_int) != Some(1);
                        let deferred = op
                            .st
                            .kw("status")
                            .map(|term| mover::quant_status::region(&body, &op.st, term, env, t))
                            .transpose()?;
                        let status = match deferred {
                            Some(_) if !checked => {
                                return Err(body.reader.fail(
                                    &op.st,
                                    "L2.MOV.019",
                                    "a deferred quant status checks its quant: no finite=1",
                                ))
                            }
                            Some(region) => {
                                pending.admit(&body, &op.st, env, &region)?;
                                Some(mover::Finite::Deferred(region))
                            }
                            None => checked.then(|| {
                                mover::Finite::Inline(format!("__mx7_status_{}", images.len()))
                            }),
                        };
                        let mut stage =
                            mover::vector_quant(&body, op, env, t, &mut images, status.clone())?;
                        pending.protect(&body, &op.st, &stage.accesses)?;
                        match &status {
                            Some(mover::Finite::Deferred(region)) => {
                                pending.record(region, &stage.accesses)
                            }
                            Some(mover::Finite::Inline(name)) => packing_status
                                .push(format!("{name}: f16[{}] @dram", 64 * t.vc.len())),
                            None => {}
                        }
                        stage.group = s.tag;
                        stage.phase = s.phase;
                        built.push(stage);
                        continue;
                    }
                    if op.op == "quant" && op.st.kw("status").is_some() {
                        return Err(body.reader.fail(
                            &op.st,
                            "L2.MOV.019",
                            "a deferred quant status is a vector_mx7 quant's",
                        ));
                    }
                    if mover::lone_copy(&body, op, env, t)? {
                        let mut stage = mover::vector_copy(&body, op, env, t, &mut images)?;
                        pending.protect(&body, &op.st, &stage.accesses)?;
                        stage.group = s.tag;
                        stage.phase = s.phase;
                        built.push(stage);
                        continue;
                    }
                    if op.op == "tile_transpose" {
                        let mut stage = mover::tile_transpose(&body, op, env, t, &mut images)?;
                        pending.protect(&body, &op.st, &stage.accesses)?;
                        stage.group = s.tag;
                        stage.phase = s.phase;
                        built.push(stage);
                        continue;
                    }
                    if op.op == "gemm_tiles" {
                        let mut stage = cluster::tiles::lower(&body, op, env, t, built.len())?;
                        pending.protect(&body, &op.st, &stage.accesses)?;
                        stage.group = s.tag;
                        stage.phase = s.phase;
                        built.push(stage);
                        continue;
                    }
                    let ops = mover::ops();
                    if !ops.contains(&op.op.as_str()) {
                        return Err(body.reader.fail(
                            &op.st,
                            "L2.MOV.015",
                            format!("no node-level L2 op '{}': {}", op.op, ops.join(", ")),
                        ));
                    }
                    if op.st.kw("node_loop").is_some() {
                        let (lines, acc) = if op.op == "copy" {
                            mover::class_copy_loop(&body, op, env, t, built.len())?
                        } else {
                            mover::class_convert_loop(&body, op, env, t, built.len())?
                        };
                        pending.protect(&body, &op.st, &acc)?;
                        let mut stage = Stage::new(lines, vec![], acc);
                        stage.group = s.tag;
                        stage.phase = s.phase;
                        stage.blocking_mover = true;
                        built.push(stage);
                        continue;
                    }
                    let (got, acc) = mover::lower(&body, op, env, t)?;
                    pending.protect(&body, &op.st, &acc)?;
                    if mine.iter().any(|s| matches!(&s.x, StageX::Op(node, _) if node.as_op().is_some_and(|o| o.st.kw("node_loop").is_some() || (o.op == "quant" && t.machine.conversion.fp16_mx7.vector_looped && mover::quant_backend(&body, o, t).unwrap_or(false))))) {
                        let mut lines = vec!["move".into()];
                        lines.extend(got.iter().map(|l| format!("{IND}{l}")));
                        let mut stage = Stage::new(lines, vec![], acc);
                        stage.group = s.tag;
                        stage.phase = s.phase;
                        stage.blocking_mover = true;
                        built.push(stage);
                        continue;
                    }
                    let prev_op = i > 0 && matches!(mine[i - 1].x, StageX::Op(..));
                    if let Some(prev) = built.last_mut() {
                        if prev.post.is_some() && prev.group == s.tag && prev_op {
                            prev.lines.extend(got.iter().map(|l| format!("{IND}{l}")));
                            prev.accesses.extend(acc);
                            continue;
                        }
                    }
                    let tok = format!("mv{}", built.len());
                    let mut lines = vec![format!("{tok} = move posted")];
                    lines.extend(got.iter().map(|l| format!("{IND}{l}")));
                    let mut st = Stage::new(lines, vec![], acc);
                    st.group = s.tag;
                    st.post = Some(tok);
                    st.phase = s.phase;
                    built.push(st);
                }
            }
        }
        pending.end(&body, end_st)?;
        links.check(&body, last_st.as_ref())?;
        if links.rings.values().any(|r| r.dst.starts_with("vc")) {
            built.push(spend(&links));
        }
        built_by.insert(n, built);
    }
    if nodes.len() > 1 || on("l2.lower/handshake-always") {
        order_nodes(&mut built_by);
    }
    events::lower_events(&mut built_by, sync)?;
    let mut out: Vec<String> = Vec::new();
    for &n in &nodes {
        let built = std::mem::take(built_by.get_mut(&n).unwrap());
        let grouped = grouped(built);
        let closed = if on("l2.lower/no-close") {
            grouped
        } else {
            close(grouped)
        };
        let lines = sync::schedule(&closed);
        if multi {
            out.push(format!("node {n}"));
            out.extend(lines.iter().map(|l| format!("{IND}{l}")));
        } else {
            out.extend(lines);
        }
    }
    let src = p.module.body(&p.name, "l2")?;
    let mut params: Vec<String> = src
        .params
        .iter()
        .map(|(n, ty)| format!("{n}: {}", fmt(ty)))
        .collect();
    for s in &src.stmts {
        if s.op == "buffer" && !on("l2.lower/scratch-dropped") {
            let target = s.target.as_ref().map(fmt).unwrap_or_default();
            let annot = s.annot.as_ref().map(fmt).unwrap_or_else(|| "None".into());
            params.push(format!("{target}: {annot}"));
        }
    }
    params.extend(packing_status);
    let mut text: Vec<String> = Vec::new();
    if p.module.target.is_some() {
        text.push(p.module.target_line());
    }
    text.push(String::new());
    for image in images {
        text.push(fills::throttle(image, t)?);
    }
    text.push(format!("fn {}.l1({})", p.name, params.join(", ")));
    text.extend(out.iter().map(|l| format!("{IND}{l}")));
    Ok(text.join("\n") + "\n")
}

/// Whether any cluster drains a tile into a core's L1.
fn drains_to_cores(stmts: &[Arc<Node>]) -> bool {
    for s in stmts {
        match s.as_ref() {
            Node::Op(o) if o.op == "drain" => {
                let pos = o.st.positional();
                if pos.len() == 3 && matches!(pos[2], Term::View(n, _) if n == "vc") {
                    return true;
                }
            }
            Node::Op(_) => {}
            other => {
                if drains_to_cores(other.body()) {
                    return true;
                }
            }
        }
    }
    false
}

/// Whether a vector stage of `cores` can run while clusters stream
/// (L2.SEARCH.003): in an outermost loop (`in_loop`) with clusters or drains.
fn beside_clusters(stmts: &[Arc<Node>], cores: &HashSet<usize>, in_loop: bool) -> bool {
    for s in stmts {
        if in_loop && cores.contains(&s.uid()) {
            return true;
        }
        match s.as_ref() {
            Node::For(f) if !in_loop => {
                if (has_cluster_par(&f.body) || drains_to_cores(&f.body))
                    && beside_clusters(&f.body, cores, true)
                {
                    return true;
                }
            }
            Node::Op(_) => {}
            other => {
                if !other.body().is_empty() && beside_clusters(other.body(), cores, in_loop) {
                    return true;
                }
            }
        }
    }
    false
}

fn has_cluster_par(stmts: &[Arc<Node>]) -> bool {
    stmts.iter().any(|s| match s.as_ref() {
        Node::Par(p) if p.kind == "mg" => true,
        Node::Op(_) => false,
        other => !other.body().is_empty() && has_cluster_par(other.body()),
    })
}

/// An image already kept whose body is `text`'s, else `text` kept.
fn shared_image(images: &mut Vec<String>, text: &str, name: &str) -> String {
    let (head, rest) = text.split_once('\n').unwrap_or((text, ""));
    let args = &head[head.find('(').unwrap()..];
    let kept_ones: &[String] = if on("l2.lower/no-shared-image") {
        &[]
    } else {
        images
    };
    for kept in kept_ones.iter() {
        let (h, r) = kept.split_once('\n').unwrap_or((kept, ""));
        if r == rest && h.ends_with(args) {
            return h["image ".len()..h.find('(').unwrap()].to_string();
        }
    }
    images.push(text.to_string());
    name.to_string()
}

/// The finite records a stage drains, each with its write.
type Records = Vec<(mover::quant_status::Status, Access)>;

type ParOut = (
    Vec<String>,
    Vec<String>,
    Vec<Access>,
    Vec<Vec<String>>,
    Records,
);

/// `(lines, units, accesses, segments, records)` of one `par` stage.
#[allow(clippy::too_many_arguments)]
fn par_stage(
    body: &Body,
    insts: &[Instance],
    t: &Target,
    images: &mut Vec<String>,
    links: &mut Links,
    land: Option<i64>,
    reading: &mut HashMap<String, (Option<i64>, Option<i64>)>,
    remote: &[(i64, i64)],
    search: &Search,
) -> Result<ParOut> {
    let kinds: BTreeSet<&str> = insts.iter().map(|i| i.kind.as_str()).collect();
    let pst = &insts[0].par().st;
    if kinds.len() != 1 {
        return Err(body
            .reader
            .fail(pst, "L2.LOW.012", "one par runs one unit kind"));
    }
    let kind = *kinds.iter().next().unwrap();
    for i in insts {
        if i.index.is_none() {
            return Err(body.reader.fail(
                &i.par().st,
                "L2.LOW.012",
                format!("{kind}[any] is the place pass's to resolve first"),
            ));
        }
    }
    if kind == "vc" {
        let (mut lines, mut units, mut acc) = (Vec::new(), Vec::new(), Vec::new());
        // Each core's open finite record: cleared before its first checked
        // instance, drained after its last (L2.VC.028).
        let mut open: IndexMap<String, mover::quant_status::Status> = IndexMap::new();
        let mut records = Vec::new();
        let mut drain = |unit: &str,
                         s: mover::quant_status::Status,
                         images: &mut Vec<String>,
                         lines: &mut Vec<String>,
                         acc: &mut Vec<Access>| {
            let name = format!("{}.mx7_status_{}", body.name, images.len());
            let name = shared_image(images, &mover::quant_status::drain_image(&name), &name);
            lines.extend([
                format!("send {unit}"),
                format!("{IND}run {name}({})", s.address()),
            ]);
            let (lo, hi) = mover::quant_status::DRAIN_L1;
            acc.push(Access::new(unit, true, (unit, "l1"), lo, hi));
            let write = Access::new(unit, true, ("mem", &s.name), s.offset, s.offset + s.bytes);
            acc.push(write.clone());
            records.push((s, write));
        };
        for i in insts {
            let classed = i.par().st.kw("classed").is_some();
            if !classed && vector::idle(body, i.par(), &i.env)? {
                continue;
            }
            let idx = i.index.unwrap();
            let unit = format!("vc[{idx}]");
            let record = vector::checked::record(body, i.par(), &i.env, t)?;
            let kept = matches!((open.get(&unit), &record),
                (Some(o), Some(r)) if o.name == r.name && o.offset == r.offset);
            if !kept {
                if let Some(s) = open.shift_remove(&unit) {
                    drain(&unit, s, images, &mut lines, &mut acc);
                }
                if let Some(r) = &record {
                    let name = format!("{}.mx7_clear_{}", body.name, images.len());
                    let name =
                        shared_image(images, &mover::quant_status::clear_image(&name), &name);
                    lines.extend([format!("send {unit}"), format!("{IND}run {name}()")]);
                    open.insert(unit.clone(), r.clone());
                }
            }
            let img = format!("{}_n{}_vc{idx}_{}", body.name, i.node, images.len());
            let (parts, params, got) =
                vector::lower(body, i.par(), &i.env, &img, links, land, t, search)?;
            acc.extend(got);
            units.push(format!("vc[{idx}]"));
            let repeat = if let Some(count) = i.par().st.kw("repeats") {
                let count = const_of(body, &i.par().st, count, &i.env)?;
                let advances = i.par().st.kw("advances").ok_or_else(|| {
                    body.reader.fail(
                        &i.par().st,
                        "L2.VC.018",
                        "repeated par requires descriptor advances",
                    )
                })?;
                // The par's i-th advance steps descriptor a_i: keyed `(i, step)`.
                let Term::Tuple(steps) = value(body, &i.par().st, advances, &i.env)? else {
                    return Err(body.reader.fail(
                        &i.par().st,
                        "L2.VC.018",
                        "descriptor advances are a tuple of steps",
                    ));
                };
                let keyed = steps
                    .into_iter()
                    .enumerate()
                    .map(|(ad, step)| Term::Tuple(vec![Term::int(ad as i128), step]))
                    .collect();
                format!(" repeats={count} advances={}", fmt(&Term::Tuple(keyed)))
            } else {
                String::new()
            };
            lines.push(format!("send vc[{idx}]{repeat}"));
            for (img, text) in parts {
                let img = if classed && !on("l2.class/share-images") {
                    images.push(text);
                    img
                } else {
                    shared_image(images, &text, &img)
                };
                lines.push(format!("{IND}run {img}({})", params.join(", ")));
            }
        }
        for (unit, s) in std::mem::take(&mut open) {
            drain(&unit, s, images, &mut lines, &mut acc);
        }
        let segs = vec![lines.clone()];
        return Ok((lines, units, acc, segs, records));
    }
    if kind != "mg" {
        return Err(body.reader.fail(
            pst,
            "L2.LOW.012",
            format!("L2 -> L1 has no {kind} lowering yet"),
        ));
    }
    let insts = if on("l2.lower/no-merge") {
        insts.to_vec()
    } else {
        merge(insts)
    };
    let into = if on("l2.lower/added-into-ignored") {
        BTreeSet::new()
    } else {
        added_into(body, &insts, remote)?
    };
    let mut streams: Vec<(i64, Stream)> = Vec::new();
    for i in &insts {
        let idx = i.index.unwrap();
        let unit = format!("mg[{idx}]");
        let s = cluster::lower(
            body,
            i,
            t,
            !into.contains(&idx),
            links,
            reading.get(&unit).copied(),
        )?;
        reading.insert(unit, s.reading);
        streams.push((idx, s));
    }
    let units: Vec<String> = streams.iter().map(|(_, s)| s.unit.clone()).collect();
    let acc: Vec<Access> = streams
        .iter()
        .flat_map(|(_, s)| s.accesses.iter().cloned())
        .collect();
    let sends_far = streams
        .iter()
        .any(|(_, s)| s.lines.iter().any(|l| l.far.is_some()));
    if !into.is_empty() || sends_far {
        let lines = added(body, &streams, &into, pst, remote, insts[0].node, t)?;
        return Ok((lines.clone(), units, acc, vec![lines], Vec::new()));
    }
    if streams
        .iter()
        .any(|(_, s)| s.lines.iter().any(|l| l.share.is_some()))
    {
        let lines = shared(body, &insts, streams, t)?;
        return Ok((lines.clone(), units, acc, vec![lines], Vec::new()));
    }
    let segs = segments(
        &streams
            .iter()
            .map(|(i, s)| (*i, s.lines.clone()))
            .collect::<Vec<_>>(),
    );
    let lines = segs.iter().flatten().cloned().collect();
    Ok((lines, units, acc, segs, Vec::new()))
}

/// Each stream's lines sent segment by segment, the units in turn.
fn interleave(streams: &[(i64, Vec<Line>)]) -> Vec<String> {
    segments(streams).into_iter().flatten().collect()
}

fn segments(streams: &[(i64, Vec<Line>)]) -> Vec<Vec<String>> {
    let count = streams
        .iter()
        .flat_map(|(_, ls)| ls.iter().map(|l| l.seg as i64))
        .max()
        .unwrap_or(-1)
        + 1;
    let mut out = Vec::new();
    for seg in 0..count {
        let mut lines = Vec::new();
        for (idx, ls) in streams {
            let words: Vec<String> = ls
                .iter()
                .filter(|l| l.seg as i64 == seg)
                .map(Line::render)
                .collect();
            if !words.is_empty() {
                lines.push(format!("send mg[{idx}]"));
                lines.extend(words.into_iter().map(|w| format!("{IND}{w}")));
            }
        }
        out.push(lines);
    }
    out
}

/// Instances placed on one unit as one stream, in instance order.
fn merge(insts: &[Instance]) -> Vec<Instance> {
    let mut by: IndexMap<i64, Instance> = IndexMap::new();
    for i in insts {
        let idx = i.index.unwrap();
        let m = by.entry(idx).or_insert_with(|| Instance {
            kind: i.kind.clone(),
            index: i.index,
            node: i.node,
            env: i.env.clone(),
            par: i.par.clone(),
            items: Vec::new(),
            cuts: Vec::new(),
        });
        let base = m.items.len();
        let cuts: Vec<usize> = if i.cuts.is_empty() {
            vec![0]
        } else {
            i.cuts.clone()
        };
        m.cuts.extend(cuts.iter().map(|c| base + c));
        m.items.extend(i.items.iter().cloned());
    }
    by.into_values().collect()
}
