//! Elide a complete contiguous input-prefix copy into one quantization stage.
//!
//! A partial/padded prefix retains its mover schedule. A complete copy can
//! instead expose the same bounded rows at their global source origin without
//! allocating or initializing another activation buffer.

use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::Value;

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::trace::R;

fn writes(node: &g::Node, buffer: &str) -> bool {
    match node {
        g::Node::Store { dst, .. } | g::Node::Mover { dst, .. } => dst.buffer == buffer,
        g::Node::Drain {
            dst: g::Operand::View(dst),
            ..
        } => dst.buffer == buffer,
        _ => false,
    }
}

fn safe(body: &[g::Stmt], edge: &str, source: &str) -> bool {
    body.iter().all(|stmt| {
        if writes(&stmt.node, edge) || writes(&stmt.node, source) {
            return false;
        }
        if let Some(body) = stmt.node.body() {
            return safe(body, edge, source);
        }
        let mut copied = stmt.clone();
        let mut uses = 0;
        super::rows::views(std::slice::from_mut(&mut copied), &mut |view| {
            uses += usize::from(view.buffer == edge);
        });
        if uses == 0 {
            return true;
        }
        if uses != 1 {
            return false;
        }
        match &stmt.node {
            g::Node::Mover {
                op: g::MoverOp::Quant,
                src: Some(src),
                kw,
                ..
            } => src.buffer == edge && !kw.iter().any(|(key, _)| key == "read_map"),
            _ => false,
        }
    })
}

fn shifted(index: &mut g::Index, rows: i64) -> bool {
    let constant = match index {
        g::Index::Int(n) => n,
        g::Index::Sym(sym) => &mut sym.konst,
    };
    if let Some(value) = constant.checked_add(rows) {
        *constant = value;
        true
    } else {
        false
    }
}

pub(crate) fn apply(kernel: &mut Kernel, arguments: &Value, m: &Machine) -> R<()> {
    if arguments.get("keep").and_then(Value::as_bool) != Some(false) {
        return Ok(());
    }
    let Some(prefix) = super::super::convert::prefix::recorded(&arguments["first"], m)? else {
        return Ok(());
    };
    if prefix.initialize
        || prefix.classed
        || prefix.rows != prefix.copied_rows
        || prefix.dst_first != 0
        || prefix.rows != prefix.dst_rows
    {
        return Ok(());
    }
    let Some(edge) = arguments.get("to").and_then(Value::as_str) else {
        return Ok(());
    };
    let Some(source) = kernel.graph.consts["chain"]["first_ports"]["src"].as_str() else {
        return Ok(());
    };
    let source = source.to_string();
    let Some(first) = kernel.graph.body.first() else {
        return Ok(());
    };
    let g::Node::Mover {
        op: g::MoverOp::Copy,
        dst,
        src: Some(src),
        ..
    } = &first.node
    else {
        return Ok(());
    };
    if src.buffer != source || dst.buffer != edge || !safe(&kernel.graph.body[1..], edge, &source) {
        return Ok(());
    }
    let matching_source = kernel.graph.ports.iter().any(|port| {
        port.name == source
            && port.role == g::Role::In
            && port.spec.dtype == g::Dtype::F16
            && port.spec.shape == [prefix.src_rows, prefix.src_pitch]
    });
    let matching_edge = kernel.graph.scratch.iter().any(|scratch| {
        scratch.name == edge
            && scratch.spec.dtype == g::Dtype::F16
            && scratch.spec.shape == [prefix.dst_rows, prefix.dst_pitch]
    });
    if !matching_source || !matching_edge {
        return Ok(());
    }
    // One top-level quant consumes each input prefix. Do not replace a staged
    // value that an arbitrary consumer reloads repeatedly from faster memory.
    let direct_reads = kernel.graph.body[1..]
        .iter()
        .filter(|stmt| {
            matches!(&stmt.node, g::Node::Mover {
                op: g::MoverOp::Quant, src: Some(view), ..
            } if view.buffer == edge)
        })
        .count();
    let mut body = kernel.graph.body[1..].to_vec();
    let mut references = 0;
    super::rows::views(&mut body, &mut |view| {
        references += usize::from(view.buffer == edge);
    });
    if direct_reads != 1 || references != 1 {
        return Ok(());
    }
    let mut valid = true;
    super::rows::views(&mut body, &mut |view| {
        if view.buffer != edge {
            return;
        }
        if view.axes.len() > 2 || view.axes.iter().any(|axis| matches!(axis, g::Axis::New)) {
            valid = false;
            return;
        }
        view.axes.resize(2, g::Axis::All);
        for (axis, extent, copied) in [
            (&view.axes[0], prefix.dst_rows, prefix.rows),
            (&view.axes[1], prefix.dst_pitch, prefix.width),
        ] {
            let bounds = match axis {
                g::Axis::All => Some((0, extent)),
                g::Axis::Run(index, count) if *count > 0 => {
                    super::index::interval(index, &Default::default())
                        .and_then(|(lo, hi)| Some((lo, hi.checked_add(*count)?)))
                }
                g::Axis::Idx(index) => super::index::interval(index, &Default::default())
                    .and_then(|(lo, hi)| Some((lo, hi.checked_add(1)?))),
                _ => None,
            };
            if !bounds.is_some_and(|(lo, hi)| lo >= 0 && hi <= copied) {
                valid = false;
                return;
            }
        }
        if view.axes[1] == g::Axis::All {
            view.axes[1] = g::Axis::Run(g::Index::Int(0), prefix.width);
        }
        match &mut view.axes[0] {
            g::Axis::All => {
                view.axes[0] = g::Axis::Run(g::Index::Int(prefix.src_first), prefix.rows);
            }
            g::Axis::Run(index, _) | g::Axis::Idx(index) => {
                valid &= shifted(index, prefix.src_first);
            }
            g::Axis::New => valid = false,
        }
        view.buffer = source.clone();
    });
    if valid {
        kernel.graph.body = body;
        kernel.graph.scratch.retain(|scratch| scratch.name != edge);
        kernel.graph.name = name("input_prefix_view", vec![Py::str(&kernel.graph.name)]);
    }
    Ok(())
}
