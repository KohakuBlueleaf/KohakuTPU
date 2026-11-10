//! Retained composition with the same scratch lifetime bound as eager execution.
//!
//! Placement and legalization are per launch. Only *final* arenas are pooled:
//! this includes packing status and F-016 drain storage. A device-side join
//! closes each launch before another can reuse its storage, including on a
//! different node. No host round trip or package concatenation is involved.

use std::collections::{BTreeMap, BTreeSet};

use super::{drains, refusal, CardStack};
use crate::output::{Compiled, Scratch, Verdict, F001, F016};
use crate::Backend;
use ktpu_l1::layout::scratch::arenas_with_bursts;
use ktpu_l1::text::{emit_at, fmt, Module, Stmt, Term};
use ktpu_l2::lower::events::BackendSyncFacts;
use ktpu_l2::{Search, Target, ZeroSign};
use ktpu_machine::{Machine, Refusal, STAGING_RUNTIME_BYTES};

// DRAM is one device window; staging is private to its physical owner.
type Key = (String, Option<i128>);

fn pool_name(key: &Key) -> String {
    format!("__retained_{}_{}", key.0, key.1.map_or_else(|| "shared".into(), |n| n.to_string()))
}

fn key(space: &str, node: Option<i128>, default_node: i128, implicit: bool) -> Key {
    let owner = if implicit { default_node } else { node.unwrap_or(default_node) };
    (space.into(), if space == "stage" { Some(owner) } else { None })
}

/// Replace symbolic addresses as terms, never by substring replacement. The
/// same visitor namespaces generated image names, marks, counters and labels.
fn term(t: &Term, map: &BTreeMap<String, Term>) -> Term {
    let s = |t: &Term| Box::new(term(t, map));
    let v = |ts: &[Term]| ts.iter().map(|t| term(t, map)).collect::<Vec<_>>();
    let name = |n: &String| match map.get(n) {
        Some(Term::Name(to)) => to.clone(),
        _ => n.clone(),
    };
    match t {
        Term::Name(n) => map.get(n).cloned().unwrap_or_else(|| t.clone()),
        Term::Neg(x) => Term::Neg(s(x)),
        Term::Not(x) => Term::Not(s(x)),
        Term::Pos(x) => Term::Pos(s(x)),
        Term::Offset(a, b) => Term::Offset(s(a), s(b)),
        Term::BinOp(op, a, b) => Term::BinOp(*op, s(a), s(b)),
        Term::Placed(a, b) => Term::Placed(s(a), s(b)),
        Term::Compare(op, a, b) => Term::Compare(op.clone(), s(a), s(b)),
        Term::Computed(n, i, axes) => Term::Computed(name(n), s(i), axes.as_ref().map(|xs| v(xs))),
        Term::AddAssign(a, b) => Term::AddAssign(s(a), s(b)),
        Term::Span(a, b) => Term::Span(s(a), s(b)),
        Term::Typed(n, ty) => Term::Typed(name(n), s(ty)),
        Term::Kw(n, x) => Term::Kw(n.clone(), s(x)),
        Term::Call(n, xs) => Term::Call(name(n), v(xs)),
        Term::View(n, xs) => Term::View(name(n), v(xs)),
        Term::Tuple(xs) => Term::Tuple(v(xs)),
        Term::Slice(lo, hi) => Term::Slice(lo.as_ref().map(|x| s(x)), hi.as_ref().map(|x| s(x))),
        // Keyword names are not variable references.
        Term::Assign(a, b) => Term::Assign(a.clone(), s(b)),
        Term::Member(a, b) => Term::Member(s(a), s(b)),
        _ => t.clone(),
    }
}

fn statement(st: &Stmt, map: &BTreeMap<String, Term>) -> Stmt {
    let mut out = st.clone();
    if let Some(Term::Name(op)) = map.get(&st.op) {
        out.op = op.clone();
    }
    out.args = st.args.iter().map(|t| term(t, map)).collect();
    out.target = st.target.as_ref().map(|t| term(t, map));
    out.annot = st.annot.as_ref().map(|t| term(t, map));
    out.body = st.body.iter().map(|s| statement(s, map)).collect();
    out
}

fn locals(stmts: &[Stmt], names: &mut BTreeSet<String>) {
    for st in stmts {
        match &st.target {
            Some(Term::Name(n) | Term::Computed(n, _, _))
                if !n.strip_prefix('r').is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())) =>
            { names.insert(n.clone()); }
            _ => {}
        }
        if st.op == "label" {
            if let Some(Term::Name(n)) = st.args.first() { names.insert(n.clone()); }
        }
        if st.op == "drain_copy_dynamic" {
            if let Some(Term::Name(n)) = st.args.get(4) { names.insert(n.clone()); }
        }
        locals(&st.body, names);
    }
}

/// A real synchronization edge, not an inferred direct memory dependency.
/// `regions` is empty because this is a whole-launch quiescence fence; its
/// scope includes all prior work, not merely conflicting external byte ranges.
/// Remote entries name the actual sender and receiver of the emitted ring.
fn join_event(index: usize, producer: i128, consumer: i128, slot: Option<i64>, phase: &str) -> serde_json::Value {
    serde_json::json!({
        "kind": "lifetime_join", "after_launch": index, "join_phase": phase,
        "producer": producer, "consumer": consumer, "slot": slot,
        "regions": [], "scope": "all_prior_work",
    })
}

/// Produce one bound-at-preparation L1 module. `launches` are in a valid
/// topological order and their external parameter names are already unique.
/// Actual packages are built and verified at binding, never certified against
/// invented non-aliasing placeholder external addresses.
pub fn compose(
    launches: &[String],
    bindings: &BTreeMap<String, i64>,
    machine: &Machine,
    zero_sign: Option<ZeroSign>,
    default_node: i128,
) -> Result<(Compiled, Vec<serde_json::Value>), Refusal> {
    if launches.is_empty() {
        return Err(Refusal::new("GRAPH.LIFETIME", "a retained graph needs a launch"));
    }
    let backend = CardStack { zero_sign, ..CardStack::default() };
    let target = Target::of(machine);
    let mut sync = BackendSyncFacts::common(target.il_slots);
    let join_slot = sync.available_slots.iter().next_back().copied();
    // Keep the join channel disjoint from all intra-launch events. Reusing it
    // is safe: gather consumes all arrivals before any release is sent, and a
    // participant cannot arrive again before consuming its previous release.
    if launches.len() > 1 {
        if let Some(slot) = join_slot {
            sync.available_slots.remove(&slot);
            sync.reserved_slots.insert(slot);
        }
    }
    let align = u64::from(machine.card.dram.line_bytes)
        .max(256)
        .max(u64::from(machine.mg.wburst) * u64::from(machine.die.word_bytes));
    let up = |n: u64| n.div_ceil(align) * align;
    let mut peak = BTreeMap::<Key, u64>::new();
    let mut params = Vec::new();
    let mut param_names = BTreeSet::new();
    let mut definitions = String::new();
    let mut bodies = Vec::<BTreeMap<i128, Vec<Stmt>>>::new();
    let mut nodes = BTreeSet::new();
    let mut events = Vec::new();
    let mut header = String::new();

    for (index, source) in launches.iter().enumerate() {
        let graph = backend.graph_to_l2(source, machine)?;
        let caller: BTreeSet<_> = backend.caller(&graph, &graph.name)?.into_iter().collect();
        if caller.iter().any(|p| !bindings.contains_key(p)) {
            return Err(Refusal::new("GRAPH.BIND", "a launch has an unbound external port"));
        }
        // Do not rerun placement over a flattened graph: that changes the
        // staging/DRAM choice made and priced for each eager launch.
        let passed = backend.passes(&graph, &graph.name, machine)?;
        let (text, launch_events) = ktpu_l2::lower::lower_bound(
            &passed, &target,
            &Search::default().signed(zero_sign.unwrap_or(ZeroSign::DEFAULT)),
            &sync, bindings, |_, _| Ok(()),
        ).map_err(refusal)?;
        let module = backend.schedule(&backend.parse_l1(&text)?, machine)?;
        let mut plan = ktpu_l1::lower::node::plan_drains(&module, &graph.name, machine, None)
            .map_err(refusal)?;
        drains::copies(&mut plan, machine)?;
        let module = &plan.module;
        header = module.target_line();
        let body = module.body(&graph.name, "l1").map_err(refusal)?;
        let implicit = !body.stmts.iter().any(|s| s.op == "node");
        // Preserve the flat access-edge API. Provenance augments an edge; it
        // does not wrap it or mix a non-event summary into the event stream.
        for mut event in launch_events {
            event["kind"] = "dependency".into();
            event["launch"] = index.into();
            if implicit {
                event["producer"] = serde_json::json!(default_node);
                event["consumer"] = serde_json::json!(default_node);
            }
            events.push(event);
        }
        let arenas = arenas_with_bursts(module, &graph.name, &caller, machine, &plan.burst_ports)
            .map_err(refusal)?;
        let mut map = BTreeMap::new();
        let mut local = BTreeSet::new();
        locals(&body.stmts, &mut local);
        local.extend(module.images.keys().cloned());
        local.extend(module.macros.keys().cloned());
        for name in local {
            map.insert(name.clone(), Term::name(format!("retained{index}_{name}")));
        }
        let mut ends = BTreeMap::<Key, u64>::new();
        for arena in &arenas {
            let k = key(&arena.space, arena.node, default_node, implicit);
            let end = ends.entry(k.clone()).or_default();
            let base = *end;
            for (member, offset) in &arena.members {
                map.insert(member.clone(), Term::Offset(
                    Box::new(Term::name(pool_name(&k))),
                    Box::new(Term::int(i128::from(base + offset))),
                ));
            }
            *end = base.checked_add(up(arena.bytes))
                .ok_or_else(|| Refusal::new("GRAPH.SCRATCH", "scratch footprint overflow"))?;
        }
        for (k, bytes) in ends {
            let entry = peak.entry(k).or_default();
            *entry = (*entry).max(bytes);
        }
        for (name, ty) in &body.params {
            if caller.contains(name) {
                if !param_names.insert(name.clone()) {
                    return Err(Refusal::new("GRAPH.BIND", "launch ports must be namespaced"));
                }
                params.push((name.clone(), ty.clone()));
            }
        }
        for definition in module.images.values().chain(module.macros.values()) {
            let renamed = match map.get(&definition.name) {
                Some(Term::Name(n)) => n,
                _ => unreachable!("definitions were namespaced"),
            };
            definitions.push_str(&format!("{} {}({})\n", definition.kind, renamed, definition.params.join(", ")));
            // Definition parameters shadow the launch's local names.
            let mut local_map = map.clone();
            for p in &definition.params { local_map.remove(p); }
            definitions.push_str(&emit_at(&definition.stmts.iter().map(|s| statement(s, &local_map)).collect::<Vec<_>>(), 1, 48));
        }
        let mut by_node = BTreeMap::new();
        if !implicit {
            for st in &body.stmts {
                let node = st.positional().first().and_then(|t| t.as_int())
                    .ok_or_else(|| Refusal::new("GRAPH.LIFETIME", "expected a physical node block"))?;
                by_node.entry(node).or_insert_with(Vec::new)
                    .extend(st.body.iter().map(|s| statement(s, &map)));
            }
        } else {
            by_node.insert(default_node, body.stmts.iter().map(|s| statement(s, &map)).collect());
        }
        nodes.extend(by_node.keys().copied());
        bodies.push(by_node);
    }

    let mut scratch = Vec::new();
    let stage_budget = machine.die.stage_bytes().unwrap_or(0).saturating_sub(STAGING_RUNTIME_BYTES);
    for (k, bytes) in peak {
        if k.0 == "stage" && bytes > stage_budget {
            return Err(Refusal::new("GRAPH.SCRATCH", format!(
                "sysnode {:?}: peak scratch {bytes} exceeds admitted staging {stage_budget}", k.1,
            )));
        }
        let name = pool_name(&k);
        let space = match k.1 {
            Some(node) => Term::Call(k.0.clone(), vec![Term::Kw("node".into(), Box::new(Term::int(node)))]),
            None => Term::name(&k.0),
        };
        params.push((name.clone(), Term::Placed(
            Box::new(Term::View("u8".into(), vec![Term::int(i128::from(bytes))])), Box::new(space),
        )));
        scratch.push(Scratch { space: k.0, node: k.1.map(|n| n as i64), bytes, members: vec![(name, 0)] });
    }
    let mut text = format!("{header}\n{definitions}\nfn captured_split.l1({})\n",
        params.iter().map(|(n, ty)| format!("{n}: {}", fmt(ty))).collect::<Vec<_>>().join(", "));
    let coordinator = *nodes.first().ok_or_else(|| Refusal::new("GRAPH.LIFETIME", "empty launch"))?;
    for &node in &nodes {
        text.push_str(&format!("    node {node}\n"));
        for (index, launch) in bodies.iter().enumerate() {
            if let Some(stmts) = launch.get(&node) {
                text.push_str(&emit_at(stmts, 2, 48));
                // Preserve the lowerer's terminal close; do not add a second
                // barrier when it already emitted an explicit one.
                if stmts.last().is_none_or(|st| st.op != "barrier") {
                    text.push_str("        barrier\n");
                }
            }
            if index + 1 < bodies.len() {
                if nodes.len() == 1 {
                    events.push(join_event(index, node, node, None, "local"));
                    text.push_str(&format!("        # retained-cut {index}\n"));
                    continue;
                }
                let slot = join_slot.ok_or_else(|| Refusal::new("GRAPH.LIFETIME", "no common slot for a scratch lifetime join"))?;
                if node == coordinator {
                    text.push_str(&format!("        wait slot {slot} n={} consume\n", nodes.len() - 1));
                    for &other in &nodes {
                        if other != coordinator {
                            text.push_str(&format!("        ring mesh={other} slot={slot}\n"));
                            events.push(join_event(index, node, other, Some(slot), "release"));
                        }
                    }
                } else {
                    text.push_str(&format!("        ring mesh={coordinator} slot={slot}\n        wait slot {slot} n=1 consume\n"));
                    events.push(join_event(index, node, coordinator, Some(slot), "gather"));
                }
            }
            // Source provenance only: a capacity cut keeps the actual join
            // above and is independently rebuilt and verified. Comments do
            // not change the fitting program's package bytes.
            text.push_str(&format!("        # retained-cut {index}\n"));
        }
    }
    // Parse now; package construction must wait for the real pooled bindings.
    let _: Module = backend.parse_l1(&text)?;
    Ok((Compiled {
        name: "captured_split".into(), l2: String::new(), l1: text,
        packages: BTreeMap::new(), scratch,
        verdict: Verdict { refused: false, findings: vec![] },
        bound: Some("retained graph requires its pinned physical alias environment".into()),
        f001: F001::default(), f016: F016::default(),
    }, events))
}
