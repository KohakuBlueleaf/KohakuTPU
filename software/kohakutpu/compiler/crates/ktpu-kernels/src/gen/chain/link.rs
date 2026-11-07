//! A checked logical prefix view of a wider typed-chain capacity allocation.
//!
//! Only abstract FP16 loads and quantization reads can change their backing
//! pitch here. Explicit byte walks, read maps, reshapes and writes are rejected
//! rather than silently reinterpreted with a different stride.

use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::Value;

use crate::kernel::Kernel;
use crate::pyrepr::{name, Py};
use crate::split::Region;
use crate::trace::{fail, R};

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum View {
    Exact,
    Prefix,
}

impl View {
    pub(crate) fn read(arguments: &Value) -> R<Self> {
        match arguments.get("link_view") {
            None => Ok(Self::Exact),
            Some(Value::String(value)) if value == "prefix" => Ok(Self::Prefix),
            _ => fail(
                "API.OP.001",
                "chain link_view must be prefix when specified",
            ),
        }
    }
}

pub(crate) struct Binding {
    input: String,
    logical: g::Spec,
    capacity: g::Spec,
}

fn safe(body: &[g::Stmt], input: &str) -> bool {
    body.iter().all(|stmt| {
        if let Some(body) = stmt.node.body() {
            return safe(body, input);
        }
        let mut probe = stmt.clone();
        let mut uses = 0;
        super::rows::views(std::slice::from_mut(&mut probe), &mut |view| {
            uses += usize::from(view.buffer == input);
        });
        if uses == 0 {
            return true;
        }
        if uses != 1 {
            return false;
        }
        match &stmt.node {
            g::Node::Value {
                op: g::ValueOp::Load,
                kw,
                ..
            } => kw.is_empty(),
            g::Node::Mover {
                op: g::MoverOp::Quant,
                src: Some(src),
                dst,
                kw,
                ..
            } => {
                src.buffer == input
                    && dst.buffer != input
                    && !kw
                        .iter()
                        .any(|(key, value)| key == "read_map" || matches!(value, g::Attr::Walk(_)))
            }
            _ => false,
        }
    })
}

impl Binding {
    pub(crate) fn of(
        first: &Kernel,
        second: &Kernel,
        from: &str,
        to: &str,
        view: View,
        machine: &Machine,
    ) -> R<Option<Self>> {
        if view == View::Exact {
            return Ok(None);
        }
        let input = super::port(second, from)?;
        let output = super::port(first, to)?;
        let s = &input.spec;
        let d = &output.spec;
        if input.role != g::Role::In
            || output.role != g::Role::Out
            || s.dtype != g::Dtype::F16
            || d.dtype != g::Dtype::F16
            || !s.layout.is_empty()
            || !d.layout.is_empty()
            || s.shape.len() != 2
            || d.shape.len() != 2
            || s.shape.iter().zip(&d.shape).any(|(s, d)| *s <= 0 || s > d)
        {
            return fail(
                "API.OP.001",
                "prefix link needs a bounded two-dimensional FP16 input",
            );
        }
        let bytes = |spec: &g::Spec| {
            spec.shape[0]
                .checked_mul(spec.shape[1])
                .and_then(|n| n.checked_mul(2))
        };
        if bytes(s).is_none() || bytes(d).is_none() {
            return fail("API.OP.001", "prefix link byte extent exceeds i64");
        }
        if s.shape == d.shape {
            return Ok(None);
        }
        if !safe(&second.graph.body, from) {
            return fail(
                "API.OP.001",
                "prefix link requires abstract loads or quant reads, not byte walks",
            );
        }
        // Validate all explicit and symbolic views against their original
        // logical specification before a wider allocation could hide a bad
        // access. Reuse the normal graph/L2 validation layer, not a second
        // hand-written loop-bound checker.
        ktpu_graph::program(&second.graph, machine)?;
        let mut capacity = s.clone();
        capacity.shape = d.shape.clone();
        Ok(Some(Self {
            input: from.into(),
            logical: s.clone(),
            capacity,
        }))
    }

    pub(crate) fn apply(&self, mut kernel: Kernel) -> R<Kernel> {
        let mut bounded = true;
        super::rows::views(&mut kernel.graph.body, &mut |view| {
            if view.buffer != self.input {
                return;
            }
            if view.axes.len() > 2 || view.axes.iter().any(|axis| matches!(axis, g::Axis::New)) {
                bounded = false;
                return;
            }
            view.axes.resize(2, g::Axis::All);
            for (axis, length) in view.axes.iter_mut().zip(&self.logical.shape) {
                if *axis == g::Axis::All {
                    *axis = g::Axis::Run(g::Index::Int(0), *length);
                }
            }
        });
        if !bounded {
            return fail(
                "API.OP.001",
                "prefix link cannot reinterpret reshaped input views",
            );
        }
        for port in &mut kernel.graph.ports {
            if port.name == self.input {
                port.spec = self.capacity.clone();
            }
        }
        for (input, spec) in &mut kernel.given {
            if *input == self.input {
                *spec = self.capacity.clone();
            }
        }
        kernel.graph.name = name(
            "prefix_link",
            vec![
                Py::str(&kernel.graph.name),
                Py::str(&self.input),
                Py::ints(&self.capacity.shape),
            ],
        );
        Ok(kernel)
    }

    /// Preserve exact logical read regions while changing only their physical
    /// row stride. Splitting a crossing interval avoids claiming the row gaps.
    pub(crate) fn regions(&self, regions: &mut Vec<Region>) -> R<()> {
        let old_pitch = (self.logical.shape[1] * 2) as u64;
        let new_pitch = (self.capacity.shape[1] * 2) as u64;
        let old_bytes = self.logical.shape[0] as u64 * old_pitch;
        let mut result = Vec::new();
        for region in regions.iter() {
            if region.allocation != self.input {
                result.push(region.clone());
                continue;
            }
            if region.bytes.start > region.bytes.end || region.bytes.end > old_bytes {
                return fail(
                    "DSL.KERN.020",
                    "prefix link read region exceeds its logical input",
                );
            }
            let mut at = region.bytes.start;
            while at < region.bytes.end {
                let row = at / old_pitch;
                let end = ((row + 1) * old_pitch).min(region.bytes.end);
                let start = row * new_pitch + at % old_pitch;
                result.push(Region {
                    allocation: self.input.clone(),
                    bytes: start..start + (end - at),
                });
                at = end;
            }
        }
        *regions = result;
        Ok(())
    }
}
