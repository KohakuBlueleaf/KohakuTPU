//! Line-sized replicated word slots for word-separable indexed graphs.
//! Indexed writers own whole lines; each output has one final compaction walk.

use std::collections::BTreeMap;

use ktpu_graph as g;
use ktpu_machine::Machine;

pub const OFF: &str = "stack.layout/no-line-slots";

fn eligible(stmts: &[g::Stmt], vlmax: i64) -> bool {
    stmts.iter().all(|s| match &s.node {
        g::Node::Par { unit, body, .. } => unit.kind == g::UnitKind::Vc && eligible(body, vlmax),
        g::Node::For { body, .. } => eligible(body, vlmax),
        g::Node::Value {
            op, ty, args, kw, ..
        } => {
            matches!(
                op,
                g::ValueOp::Load
                    | g::ValueOp::Unpack
                    | g::ValueOp::Pack
                    | g::ValueOp::Xrot
                    | g::ValueOp::Xlane
                    | g::ValueOp::Sel
            ) && matches!(ty.dtype, g::Dtype::F16 | g::Dtype::F32)
                && ty.shape.len() == 2
                && ty.shape[1] == vlmax
                && ty.mode.is_none()
                && kw.is_empty()
                && args.iter().all(|a| {
                    matches!(
                        a,
                        g::Operand::Value(_) | g::Operand::Float(_) | g::Operand::View(_)
                    )
                })
        }
        g::Node::Store { .. } => true,
        g::Node::Mover { op, kw, flags, .. } => match op {
            g::MoverOp::Gather | g::MoverOp::Scatter => {
                kw.iter()
                    .all(|(k, v)| k == "index" && matches!(v, g::Attr::View(_)))
                    && flags.iter().all(|f| f == "fp")
            }
            g::MoverOp::Fill => kw.iter().all(|(k, _)| k == "value") && flags.is_empty(),
            _ => false,
        },
        _ => false,
    })
}

fn scaled(i: &mut g::Index, factor: i64) {
    match i {
        g::Index::Int(n) => *n *= factor,
        g::Index::Sym(s) => {
            s.konst *= factor;
            for (_, c) in &mut s.terms {
                *c *= factor;
            }
        }
    }
}

fn view(v: &mut g::View, map: &BTreeMap<String, String>, factor: i64) {
    let Some(name) = map.get(&v.buffer) else {
        return;
    };
    v.buffer = name.clone();
    if v.axes.len() == 2 {
        if let g::Axis::Run(at, n) = &mut v.axes[1] {
            scaled(at, factor);
            *n *= factor;
        }
    }
}

fn whole(name: &str) -> g::View {
    g::View {
        buffer: name.into(),
        axes: Vec::new(),
    }
}

fn copy(src: &str, dst: &str, sw: Vec<(i64, i64)>, dw: Vec<(i64, i64)>) -> g::Stmt {
    g::Stmt {
        at: "line-slots".into(),
        node: g::Node::Mover {
            op: g::MoverOp::Copy,
            dst: whole(dst),
            src: Some(whole(src)),
            flags: Vec::new(),
            kw: vec![
                ("walk".into(), g::Attr::Walk(sw)),
                ("dwalk".into(), g::Attr::Walk(dw)),
            ],
        },
    }
}

fn aligned_views(stmts: &[g::Stmt], names: &BTreeMap<String, String>, lanes: i64) -> bool {
    let check = |v: &g::View| {
        !names.contains_key(&v.buffer)
            || v.axes.len() < 2
            || match &v.axes[1] {
                g::Axis::All => true,
                g::Axis::Run(g::Index::Int(at), n) => at % lanes == 0 && n % lanes == 0,
                _ => false,
            }
    };
    stmts.iter().all(|s| match &s.node {
        g::Node::Par { body, .. } | g::Node::For { body, .. } => aligned_views(body, names, lanes),
        g::Node::Store { dst, .. } => names.contains_key(&dst.buffer) && check(dst),
        g::Node::Value { args, .. } => args.iter().all(|a| match a {
            g::Operand::View(v) => names.contains_key(&v.buffer) && check(v),
            _ => true,
        }),
        g::Node::Mover { dst, src, .. } => {
            names.contains_key(&dst.buffer)
                && check(dst)
                && src
                    .as_ref()
                    .is_none_or(|s| names.contains_key(&s.buffer) && check(s))
        }
        _ => false,
    })
}

fn rewrite(stmts: &mut [g::Stmt], map: &BTreeMap<String, String>, factor: i64) {
    for s in stmts {
        match &mut s.node {
            g::Node::Par { body, .. } | g::Node::For { body, .. } => rewrite(body, map, factor),
            g::Node::Value { op, ty, args, .. } => {
                if *op == g::ValueOp::Load {
                    ty.shape[0] *= factor;
                } else {
                    ty.shape[1] *= factor;
                }
                for a in args {
                    if let g::Operand::View(v) = a {
                        view(v, map, factor);
                    }
                }
            }
            g::Node::Store { dst, .. } => view(dst, map, factor),
            g::Node::Mover { dst, src, .. } => {
                view(dst, map, factor);
                if let Some(src) = src {
                    view(src, map, factor);
                }
            }
            _ => unreachable!(),
        }
    }
}

/// Preserve each word's lane operations in identical line-sized copies.
/// Unsupported reductions, tiled views and sub-word slices retain the normal path.
pub fn isolate(mut graph: g::Graph, m: &Machine) -> g::Graph {
    let word = i64::from(m.die.word_bytes);
    let line = i64::from(m.card.dram.line_bytes);
    if crate::mutant::active() == Some(OFF)
        || word == 0
        || line <= word
        || line % word != 0
        || !graph.body.iter().any(|s| {
            if let g::Node::Mover {
                op: g::MoverOp::Scatter,
                dst,
                ..
            } = &s.node
            {
                graph.ports.iter().any(|p| {
                    p.name == dst.buffer && p.spec.shape.len() == 2 && p.spec.shape[1] * 2 < line
                })
            } else {
                false
            }
        })
        || !eligible(&graph.body, i64::from(m.vc.vlmax))
    {
        return graph;
    }
    let good = |s: &g::Spec| {
        s.dtype != g::Dtype::F16
            || (s.shape.len() == 2 && s.shape[1] % (word / 2) == 0 && s.layout.is_empty())
    };
    if !graph.ports.iter().all(|p| good(&p.spec)) || !graph.scratch.iter().all(|p| good(&p.spec)) {
        return graph;
    }
    let factor = line / word;
    let mut map: BTreeMap<String, String> = graph
        .scratch
        .iter()
        .filter(|p| p.spec.dtype == g::Dtype::F16)
        .map(|p| (p.name.clone(), p.name.clone()))
        .collect();
    for p in &graph.ports {
        if p.spec.dtype == g::Dtype::F16 {
            map.insert(p.name.clone(), format!("line_slot_{}", p.name));
        }
    }
    if map.values().any(|n| {
        n.starts_with("line_slot_")
            && (graph.scratch.iter().any(|s| &s.name == n)
                || graph.ports.iter().any(|p| &p.name == n))
    }) || !aligned_views(&graph.body, &map, word / 2)
    {
        return graph;
    }
    for s in &mut graph.scratch {
        if s.spec.dtype == g::Dtype::F16 {
            s.spec.shape[1] *= factor;
        }
    }
    let mut prefix = Vec::new();
    let mut suffix = Vec::new();
    for p in &graph.ports {
        if p.spec.dtype != g::Dtype::F16 {
            continue;
        }
        let name = &map[&p.name];
        let words = p.spec.shape.iter().product::<i64>() * 2 / word;
        let mut spec = p.spec.clone();
        spec.shape[1] *= factor;
        graph.scratch.push(g::Scratch {
            name: name.clone(),
            spec,
        });
        prefix.push(copy(
            &p.name,
            name,
            vec![(words, word), (factor, 0)],
            vec![(words, line), (factor, word)],
        ));
        if p.role == g::Role::Out {
            suffix.push(copy(
                name,
                &p.name,
                vec![(words, line)],
                vec![(words, word)],
            ));
        }
    }
    rewrite(&mut graph.body, &map, factor);
    prefix.append(&mut graph.body);
    prefix.append(&mut suffix);
    graph.body = prefix;
    graph
}
