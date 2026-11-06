//! Mergeable population statistics, transported as line-owned F32 vectors.

mod distributed;
pub mod domains;

pub use distributed::kernel;
pub use domains::GroupNormSpec;

use crate::expr::{self, E};
use crate::kernel::{build, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::spec;
use crate::trace::control::At;
use crate::trace::{fail, Arg, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use std::collections::HashMap;

/// Counts are template metadata; mean and M2 are vector expressions.
#[derive(Clone, Debug)]
pub struct GroupStatState {
    pub n: i64,
    pub mean: E,
    pub m2: E,
}

/// Combine two populations without subtracting squared uncentred moments.
/// Constants are converted to E8M15 by the ordinary vector expression emitter.
pub fn merge_states(a: &GroupStatState, b: &GroupStatState) -> R<GroupStatState> {
    if a.n < 0 || b.n < 0 {
        return fail("API.OP.001", "population counts must be nonnegative");
    }
    let Some(n) = a.n.checked_add(b.n) else {
        return fail("API.OP.001", "population count overflow");
    };
    if n == 0 {
        return Ok(GroupStatState {
            n,
            mean: expr::konst(0.0),
            m2: expr::konst(0.0),
        });
    }
    if a.n == 0 {
        return Ok(b.clone());
    }
    if b.n == 0 {
        return Ok(a.clone());
    }
    let delta = expr::op("sub", vec![(&b.mean).into(), (&a.mean).into()])?;
    let weight = b.n as f64 / n as f64;
    let correction = expr::op("mul", vec![(&delta).into(), weight.into()])?;
    let square = expr::op("mul", vec![(&delta).into(), delta.into()])?;
    let cross = expr::op("mul", vec![square.into(), (a.n as f64 * weight).into()])?;
    let sum = expr::op("add", vec![(&a.m2).into(), (&b.m2).into()])?;
    Ok(GroupStatState {
        n,
        mean: expr::op("add", vec![(&a.mean).into(), correction.into()])?,
        m2: expr::op("add", vec![sum.into(), cross.into()])?,
    })
}

/// Each record field occupies a complete vector of DRAM lines. Lanes carry independent
/// states with the same counts. Empty operands are not read.
pub fn merge_kernel(na: i64, nb: i64, m: &Machine) -> R<Kernel> {
    let state = merge_states(
        &GroupStatState {
            n: na,
            mean: expr::load(0),
            m2: expr::load(1),
        },
        &GroupStatState {
            n: nb,
            mean: expr::load(2),
            m2: expr::load(3),
        },
    )?;
    let shape = [1, m.vc.vlmax as i64];
    let dtype = g::Dtype::F32;
    let mut ports = Vec::new();
    let mut given = Vec::new();
    for field in ["mean_a", "m2_a", "mean_b", "m2_b", "mean", "m2"] {
        let output = field == "mean" || field == "m2";
        ports.push(if output {
            Port::output(field, &shape, dtype)
        } else {
            Port::input(field, &shape, dtype)
        });
        given.push((
            field.into(),
            spec(&shape, dtype, vec![], g::BufSpace::Dram)?,
        ));
    }
    let key = name("groupnorm_merge", vec![Py::Int(na), Py::Int(nb)]);
    build(&key, m, ports, given, |t, bufs| {
        if state.n == 0 {
            t.fill_value(&bufs[4].whole(), 0, None, None)?;
            return t.fill_value(&bufs[5].whole(), 0, None, None);
        }
        t.par(1, g::UnitKind::Vc, At::Var, |t, _| {
            t.range(1, |t, _| {
                let mut values = HashMap::new();
                let used = expr::loads(&state.mean)
                    .union(&expr::loads(&state.m2))
                    .copied()
                    .collect::<std::collections::BTreeSet<_>>();
                for i in used {
                    values.insert(i, Arg::Val(t.load(&bufs[i].whole(), false)?));
                }
                let mean = expr::emit(t, &state.mean, &values)?;
                let m2 = expr::emit(t, &state.m2, &values)?;
                t.store(&bufs[4].whole(), mean)?;
                t.store(&bufs[5].whole(), m2)
            })
        })
    })
}
