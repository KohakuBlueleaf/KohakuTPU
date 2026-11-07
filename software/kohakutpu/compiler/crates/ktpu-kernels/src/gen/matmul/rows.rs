//! Complete contraction row domains inside a node-local typed composition.
//!
//! The shared B packing keeps its global N tile. Only A is repacked locally,
//! and the local product and quant rings retain their ordinary emitter.

use super::*;

/// Emit a row owner's product against the invocation's global ports. Row cuts
/// are whole epilogue quantums; output and extra operands keep global origins.
#[allow(clippy::too_many_arguments)]
pub(crate) fn kernel(
    mm: &Mm,
    e: &E,
    extras: &[Operand],
    out: &Operand,
    knobs: &Knobs,
    first: i64,
    count: i64,
    mach: &Machine,
) -> R<Kernel> {
    let signature = mm_kernel(mm, e, extras, out, knobs, mach)?;
    let hw = Hw::of(mach);
    let quantum = if v::holds_stat(extras, out.kind) {
        v::split::common_quantum(mm.quantum, hw.stat_rows())?
    } else {
        mm.quantum
    };
    let tm = (1..=mm.tm / quantum)
        .rev()
        .map(|n| n * quantum)
        .find(|n| count % n == 0)
        .ok_or_else(|| {
            ktpu_machine::Refusal::new("API.OP.006", "row owner needs a whole product tile")
        })?;
    let local = Mm {
        bt: 1,
        m: count,
        mp: count,
        tm,
        shared: true,
        quantum,
        ..*mm
    };
    let ports = signature
        .graph
        .ports
        .iter()
        .map(|p| Port {
            name: p.name.clone(),
            role: p.role,
            dims: p.spec.shape.clone(),
            dtype: p.spec.dtype,
            layout: p.spec.layout.clone(),
        })
        .collect();
    let extras: Vec<_> = extras.iter().map(|o| v::split::shifted(o, first)).collect();
    let out = v::split::shifted(out, first);
    let key = name(
        "product_rows",
        vec![
            Py::str(&signature.graph.name),
            Py::Int(first),
            Py::Int(count),
        ],
    );
    build(&key, mach, ports, signature.given, |t, b| {
        emit_local(
            t,
            b,
            &local,
            &computed(e, knobs),
            &extras,
            &out,
            knobs,
            mach,
            &BTreeMap::new(),
            &[],
            split::Origin {
                f16: first,
                last_rows: first + count == mm.m,
                ..split::Origin::default()
            },
            None,
        )
    })
}
