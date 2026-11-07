//! Rebase a private row-local edge into its owner's compact allocation.

use ktpu_graph as g;

use crate::kernel::Kernel;
use crate::trace::R;

fn attr(value: &mut g::Attr, visit: &mut impl FnMut(&mut g::View)) {
    match value {
        g::Attr::View(v) => visit(v),
        g::Attr::Tuple(values) => {
            for value in values {
                attr(value, visit);
            }
        }
        _ => {}
    }
}

pub(super) fn views(body: &mut [g::Stmt], visit: &mut impl FnMut(&mut g::View)) {
    for stmt in body {
        match &mut stmt.node {
            g::Node::Par { body, .. }
            | g::Node::For { body, .. }
            | g::Node::When { body, .. }
            | g::Node::Sticky { body } => views(body, visit),
            g::Node::Value { args, kw, .. } => {
                for value in args {
                    if let g::Operand::View(v) = value {
                        visit(v);
                    }
                }
                for (_, value) in kw {
                    attr(value, visit);
                }
            }
            g::Node::Store { dst, .. } => visit(dst),
            g::Node::Drain { dst, .. } => {
                if let g::Operand::View(v) = dst {
                    visit(v);
                }
            }
            g::Node::Mover { dst, src, kw, .. } => {
                visit(dst);
                if let Some(src) = src {
                    visit(src);
                }
                for (_, value) in kw {
                    attr(value, visit);
                }
            }
            g::Node::Send { .. } => {}
        }
    }
}

/// All uses must name row slices. Whole-buffer accesses retain their original
/// allocation because their extent is part of the operation's meaning.
/// The row-locality proof belongs to the descriptor planner (L2.PASS.004).
pub(super) fn compact(kernel: &mut Kernel, link: &str, first: i64, count: i64) -> R<()> {
    let Some(scratch) = kernel.graph.scratch.iter().find(|s| s.name == link) else {
        return Ok(());
    };
    if !scratch.spec.layout.is_empty()
        || scratch.spec.shape.is_empty()
        || scratch.spec.shape[0] < first + count
    {
        return Ok(());
    }
    let mut sliced = true;
    views(&mut kernel.graph.body, &mut |view| {
        if view.buffer == link && !matches!(view.axes.first(), Some(g::Axis::Run(..))) {
            sliced = false;
        }
    });
    if !sliced {
        return Ok(());
    }
    let mut overflow = false;
    views(&mut kernel.graph.body, &mut |view| {
        if view.buffer != link {
            return;
        }
        if let Some(g::Axis::Run(index, _)) = view.axes.first_mut() {
            let constant = match index {
                g::Index::Int(n) => n,
                g::Index::Sym(s) => &mut s.konst,
            };
            match constant.checked_sub(first) {
                Some(value) => *constant = value,
                None => overflow = true,
            }
        }
    });
    if overflow {
        return crate::trace::fail("API.OP.001", "private row origin exceeds i64");
    }
    for scratch in &mut kernel.graph.scratch {
        if scratch.name == link {
            scratch.spec.shape[0] = count;
        }
    }
    Ok(())
}
