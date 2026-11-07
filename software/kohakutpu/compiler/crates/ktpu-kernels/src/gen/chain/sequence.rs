//! Ordered typed stages with one public signature and hygienic private scopes.
//!
//! This is the composition boundary for row chunks: public row views retain
//! their global coordinates while each member owns its private stage buffers.
//! The shared lifetime allocator may then reuse disjoint private allocations.

use std::collections::{BTreeMap, HashSet};

use ktpu_machine::Machine;
use serde_json::{json, Value};

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::{fail, R};

/// Concatenate stages without equating their private names. Identical public
/// signatures are deliberate: ordered overlapping writes are legal and are not
/// mistaken for independent row owners by this constructor.
pub fn kernel(members: Vec<Kernel>) -> R<Kernel> {
    let Some(first) = members.first() else {
        return fail("API.OP.001", "kernel.sequence needs at least one member");
    };
    for member in &members {
        if member.graph.ports != first.graph.ports || member.given != first.given {
            return fail(
                "API.OP.001",
                "sequence members must have identical public signatures",
            );
        }
    }
    let constants: Vec<_> = members.iter().map(|k| k.graph.consts.clone()).collect();
    let signature = first.clone();
    let mut result = scoped(&signature, members)?;
    result.graph.consts["sequence"] = json!(constants);
    Ok(result)
}

/// Shared hygiene for sequential members and independently node-scoped shares.
/// The caller supplies the already validated global signature.
pub(crate) fn scoped(signature: &Kernel, members: Vec<Kernel>) -> R<Kernel> {
    let mut result = signature.clone();
    result.graph.body.clear();
    result.graph.scratch.clear();
    let mut taken: HashSet<_> = signature
        .graph
        .ports
        .iter()
        .map(|p| p.name.clone())
        .collect();
    // Reserve all original names before choosing any prefix. In particular,
    // a later member's public or private name cannot capture an earlier one.
    for member in &members {
        taken.extend(member.graph.scratch.iter().map(|s| s.name.clone()));
        let value = serde_json::to_value(&member.graph.body)
            .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
        super::names_in(&value, &super::NAMED, &mut taken);
    }
    let mut names = Vec::new();
    let mut scopes = Vec::new();
    for mut member in members {
        let mut value = serde_json::to_value(&member.graph.body)
            .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
        let mut private: HashSet<_> = member
            .graph
            .scratch
            .iter()
            .map(|s| s.name.clone())
            .collect();
        super::bound_names(&member.graph.body, &mut private);
        let prefix = (0..)
            .map(|i| format!("sequence{i}_"))
            .find(|prefix| {
                private
                    .iter()
                    .all(|n| !taken.contains(&format!("{prefix}{n}")))
            })
            .expect("a fresh private sequence scope");
        scopes.push(
            private
                .iter()
                .map(|n| (n.clone(), format!("{prefix}{n}")))
                .collect::<BTreeMap<_, _>>(),
        );
        super::renamed(&mut value, &private, &|n| format!("{prefix}{n}"));
        taken.extend(private.iter().map(|n| format!("{prefix}{n}")));
        member.graph.body = serde_json::from_value(value)
            .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))?;
        for scratch in &mut member.graph.scratch {
            scratch.name = format!("{prefix}{}", scratch.name);
        }
        names.push(Py::str(&member.graph.name));
        result.graph.scratch.extend(member.graph.scratch);
        result.graph.body.extend(member.graph.body);
    }
    result.graph.name = name(
        "sequence",
        vec![Py::str(&signature.graph.name), Py::Tuple(names)],
    );
    result.graph.consts["scopes"] = json!(scopes);
    Ok(result)
}

/// Pending straight-line quant operations acquire their sequence-wide status
/// lifetime after composition. Explicit statuses and nested schedules retain
/// the quant emitter's existing lifetime policy.
pub(crate) fn finish(kernel: Kernel, arguments: &Value, machine: &Machine) -> R<Kernel> {
    finalized(kernel, arguments, machine, true).map(|(kernel, _)| kernel)
}

/// The actual collision-selected scope, shared with split-region metadata.
pub(crate) fn scope(kernel: &Kernel, index: usize) -> R<BTreeMap<String, String>> {
    serde_json::from_value(kernel.graph.consts["scopes"][index].clone())
        .map_err(|e| ktpu_machine::Refusal::new("DSL.KERN.020", format!("composed scope: {e}")))
}

/// A sequence's optional quant policy is selected on the whole descriptor.
/// Reject child policies rather than silently retain an earlier final check.
pub(crate) fn deferred(descriptor: &Value) -> R<()> {
    match descriptor {
        Value::Object(object) => {
            if object.contains_key("gen") {
                if let Some(arguments) = object.get("args") {
                    if super::quant::Backend::read(arguments)? != super::quant::Backend::Generator {
                        return fail(
                            "API.OP.001",
                            "select quant_backend on the sequence, not its members",
                        );
                    }
                }
            }
            for child in object.values() {
                deferred(child)?;
            }
        }
        Value::Array(array) => {
            for child in array {
                deferred(child)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// Rebuild private allocation manifests after sequence-wide status scheduling
/// or pooling. A private buffer used on several nodes is listed for each user;
/// unlike output ownership, this is an access manifest, not a uniqueness claim.
pub(crate) fn scratch_regions(
    kernel: &Kernel,
    nodes: &crate::split::NodeSet,
    shards: &mut [crate::split::ShardTiles],
    machine: &Machine,
) -> R<()> {
    use ktpu_graph as g;
    fn visit(body: &[g::Stmt], physical: i64, used: &mut BTreeMap<i64, HashSet<String>>) -> R<()> {
        for stmt in body {
            if let g::Node::Par {
                var,
                lo,
                hi,
                unit,
                body,
                ..
            } = &stmt.node
            {
                if unit.kind == g::UnitKind::Node {
                    let id = match (lo, hi, &unit.index) {
                        (g::Index::Int(lo), g::Index::Int(hi), Some(g::Index::Int(id)))
                            if hi.checked_sub(*lo) == Some(1) =>
                        {
                            *id
                        }
                        (g::Index::Int(lo), g::Index::Int(hi), Some(g::Index::Sym(index)))
                            if hi.checked_sub(*lo) == Some(1)
                                && index.konst == 0
                                && index.terms == [(g::Atom::Var(var.clone()), 1)] =>
                        {
                            *lo
                        }
                        _ => return fail("DSL.KERN.020", "sequence node needs one physical owner"),
                    };
                    visit(body, id, used)?;
                    continue;
                }
            }
            if let Some(body) = stmt.node.body() {
                visit(body, physical, used)?;
            } else {
                let mut copy = stmt.clone();
                super::rows::views(std::slice::from_mut(&mut copy), &mut |view| {
                    used.entry(physical)
                        .or_default()
                        .insert(view.buffer.clone());
                });
            }
        }
        Ok(())
    }
    let mut used = BTreeMap::new();
    visit(&kernel.graph.body, 0, &mut used)?;
    for shard in shards {
        shard.scratch_regions.clear();
        let Some(names) = used.get(&nodes.ids()[shard.logical_node]) else {
            continue;
        };
        for scratch in &kernel.graph.scratch {
            if names.contains(&scratch.name) {
                let elements = scratch
                    .spec
                    .shape
                    .iter()
                    .try_fold(1i64, |n, d| n.checked_mul(*d))
                    .ok_or_else(|| {
                        ktpu_machine::Refusal::new("API.OP.001", "sequence scratch extent")
                    })?;
                let bytes = match scratch.spec.dtype {
                    g::Dtype::Mx7a | g::Dtype::Mx7b => machine.mx7_bytes(elements),
                    dtype => elements
                        .checked_mul(crate::gen::Hw::item(dtype))
                        .ok_or_else(|| {
                            ktpu_machine::Refusal::new("API.OP.001", "sequence scratch bytes")
                        })?,
                };
                shard.scratch_regions.push(crate::split::Region {
                    allocation: scratch.name.clone(),
                    bytes: 0..bytes as u64,
                });
            }
        }
    }
    Ok(())
}

/// Distributed member manifests are already physical. Reuse equal capacities
/// and return the selected aliases so every private region follows its buffer.
pub(crate) fn finish_mapped(
    kernel: Kernel,
    arguments: &Value,
    machine: &Machine,
) -> R<(Kernel, BTreeMap<String, String>)> {
    finalized(kernel, arguments, machine, false)
}

/// Retain the existing decorated-and-pooled schedule as the baseline. The
/// optional alternative pools pending quants first, orders their windows, then
/// assigns independent status records to each resulting quant. Select only on
/// complete decorated prices, returning the aliases of the selected storage.
fn finalized(
    kernel: Kernel,
    arguments: &Value,
    machine: &Machine,
    resize: bool,
) -> R<(Kernel, BTreeMap<String, String>)> {
    let backend = super::quant::Backend::read(arguments)?;
    let enabled = match arguments.get("pipeline_lookahead") {
        None | Some(Value::Bool(false)) => false,
        Some(Value::Bool(true)) => true,
        _ => return fail("API.OP.001", "pipeline_lookahead must be boolean"),
    };
    select(kernel, backend, enabled, resize, machine)
}

/// Storage and ordering are independent candidates until complete L1 pricing.
pub(super) fn select(
    kernel: Kernel,
    backend: super::quant::Backend,
    enabled: bool,
    resize: bool,
    machine: &Machine,
) -> R<(Kernel, BTreeMap<String, String>)> {
    if !enabled {
        return super::reuse::mapped(
            super::quant::kernel(kernel, backend, machine)?,
            machine,
            resize,
        );
    }
    let mut price = |graph: &ktpu_graph::Graph| ktpu_graph::cycles(graph, machine);
    let baseline = super::quant::kernel(kernel.clone(), backend, machine)?;
    let (decorated_pool, decorated_aliases) = super::reuse::candidate(&baseline, resize)?;
    let (pooled, aliases) = super::reuse::candidate(&kernel, resize)?;
    let mut candidates = Vec::new();
    if !decorated_aliases.is_empty() {
        candidates.push((decorated_pool, decorated_aliases));
    }
    candidates.push((
        super::lookahead::candidate_priced(&kernel, backend, machine, &mut price)?,
        BTreeMap::new(),
    ));
    if !aliases.is_empty() {
        candidates.push((
            super::quant::kernel(pooled.clone(), backend, machine)?,
            aliases.clone(),
        ));
        candidates.push((
            super::lookahead::candidate_priced(&pooled, backend, machine, &mut price)?,
            aliases,
        ));
    }
    let mut best = None;
    let mut error = None;
    for (candidate, mapped) in std::iter::once((baseline, BTreeMap::new())).chain(candidates) {
        match price(&candidate.graph) {
            Ok(cycles) => {
                if best
                    .as_ref()
                    .is_none_or(|(previous, _, _)| cycles <= *previous)
                {
                    best = Some((cycles, candidate, mapped));
                }
            }
            Err(reason) if error.is_none() => error = Some(reason),
            Err(_) => {}
        }
    }
    match best {
        Some((_, kernel, aliases)) => Ok((kernel, aliases)),
        None => Err(error.expect("every complete candidate was refused")),
    }
}
