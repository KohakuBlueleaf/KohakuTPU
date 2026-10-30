//! A generated kernel: its graph and the specs the generator gives its
//! ports (`Kernel.build` + `Kernel.graph`, DSL.KERN.003, .005-.008).

use crate::trace::{buffer, fail, Buf, Facts, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;

/// A generated kernel.
#[derive(Clone, Debug, PartialEq)]
pub struct Kernel {
    pub graph: g::Graph,
    /// The spec the generator gives each port, in port order.
    pub given: Vec<(String, g::Spec)>,
}

/// A port as the generator declares it: role, element type, layout.
#[derive(Clone, Debug)]
pub struct Port {
    pub name: String,
    pub role: g::Role,
    pub dims: Vec<i64>,
    pub dtype: g::Dtype,
    pub layout: Vec<(String, g::LayoutValue)>,
}

impl Port {
    pub fn input(name: &str, dims: &[i64], dtype: g::Dtype) -> Port {
        Port {
            name: name.to_string(),
            role: g::Role::In,
            dims: dims.to_vec(),
            dtype,
            layout: vec![],
        }
    }

    pub fn output(name: &str, dims: &[i64], dtype: g::Dtype) -> Port {
        Port {
            role: g::Role::Out,
            ..Port::input(name, dims, dtype)
        }
    }

    pub fn with_layout(mut self, layout: Vec<(String, g::LayoutValue)>) -> Port {
        self.layout = layout;
        self
    }
}

/// `gm=..`, `nk=..` style layout pairs.
pub fn layout(pairs: &[(&str, i64)]) -> Vec<(String, g::LayoutValue)> {
    pairs
        .iter()
        .map(|(k, v)| (k.to_string(), g::LayoutValue::Int(*v)))
        .collect()
}

/// The extents `shape` a caller supplies for port `p` of kernel `name`
/// checked against the port's: a DSL.KERN.020 refusal when they differ.
pub fn bind_shape(name: &str, p: &Port, shape: &[i64]) -> R<()> {
    if shape != p.dims {
        return fail(
            "DSL.KERN.020",
            format!(
                "{name}: {} has extents {:?} where the kernel says {:?}",
                p.name, shape, p.dims
            ),
        );
    }
    Ok(())
}

/// The kernel `name` over `ports` (each given the spec in `given`) traced
/// for `machine`: each port's spec is the given shape with the port's
/// element type and layout, the given rank and fixed extents checked.
pub fn build(
    name: &str,
    machine: &Machine,
    ports: Vec<Port>,
    given: Vec<(String, g::Spec)>,
    body: impl FnOnce(&mut Trace, &[Buf]) -> R<()>,
) -> R<Kernel> {
    let mut t = Trace::new(name, Facts::of(machine), &machine.die.target);
    let mut bufs = vec![];
    for p in &ports {
        let Some((_, s)) = given.iter().find(|(n, _)| *n == p.name) else {
            return fail("DSL.KERN.020", format!("{name}: no tensor for {}", p.name));
        };
        bind_shape(name, p, &s.shape)?;
        let spec = buffer::spec(&s.shape, p.dtype, p.layout.clone(), g::BufSpace::Dram)?;
        bufs.push(t.port(&p.name, p.role, spec)?);
    }
    body(&mut t, &bufs)?;
    Ok(Kernel {
        graph: t.graph()?,
        given,
    })
}
