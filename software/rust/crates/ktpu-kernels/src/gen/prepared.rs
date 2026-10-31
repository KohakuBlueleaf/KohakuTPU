//! A rows kernel whose laid-out operands it makes itself: one launch, where
//! the operand's relayout alone took one (or two, with a repitch).

use super::relayout::{self, scratch_rows, shape_repr};
use super::vector::{self as v, Bufs, Kind, Operand};
use super::{cdiv, Hw};
use crate::expr::{self, E};
use crate::kernel::{build, Kernel, Port};
use crate::mutants;
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, spec, Buf};
use crate::trace::{fail, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use std::collections::{BTreeMap, HashMap};

/// The reverse mutant of the prepared default: every pending operand made
/// by its own relayouts first.
pub const SEPARATE_MUTANT: &str = "prepared/separate";

/// The planner knobs of pending operands (Python's `runtime/forms.py`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Knobs {
    /// A laid-out operand is made inside the kernel that reads it
    /// (`forms.PREPARED`).
    pub prepared: bool,
    /// A packing's operand is deferred to the packing's kernel when a
    /// measured recipe wins (`forms.PREPARED_QUANT`).
    pub prepared_quant: bool,
}

/// The knobs' defaults: the one source the planner and Python
/// (`_rs.PREPARED_DEFAULTS`) read; separate relayouts under
/// [`SEPARATE_MUTANT`].
pub fn defaults() -> Knobs {
    Knobs {
        prepared: !mutants::on(SEPARATE_MUTANT),
        prepared_quant: true,
    }
}

/// Whether the final plan addresses its destination in words, not row
/// indices: every part writing it a fill, word walk, runs or pull.
pub fn direct_destination(plans: &[relayout::Plan]) -> bool {
    use relayout::Part;
    plans[plans.len() - 1].parts.iter().all(|p| match p {
        Part::Fill(_) | Part::Words(_) | Part::Runs(_) | Part::Pull(_) => true,
        Part::Splat(s) => s.dst != "dst",
        Part::Exchange(x) => x.dst != "dst",
        Part::Shuffle(s) => s.dst != "dst",
    })
}

/// Ports and the spec each is given.
pub type Ports = (Vec<Port>, Vec<(String, g::Spec)>);

/// An input a kernel makes from its plans' steps: the port it stands for,
/// the steps' port prefix, the plans.
pub struct MadeInput<'a> {
    pub port: String,
    pub prefix: String,
    pub plans: &'a [relayout::Plan],
}

/// The inputs `made` (operand index -> plans) of a kernel whose operand i
/// is port `in{i}` (`p{i}_` its steps').
pub fn made_inputs(made: &BTreeMap<usize, Vec<relayout::Plan>>) -> Vec<MadeInput<'_>> {
    made.iter()
        .map(|(i, plans)| MadeInput {
            port: format!("in{i}"),
            prefix: format!("p{i}_"),
            plans,
        })
        .collect()
}

/// `prepared.input_ports`: `plans`' source `{prefix}x` and their tables.
pub fn input_ports(plans: &[relayout::Plan], prefix: &str, m: &Machine) -> R<Ports> {
    let x = [plans[0].src_words, relayout::Hw::of(m).lanes];
    let (mut ports, mut given) = (
        vec![Port::input(&format!("{prefix}x"), &x, g::Dtype::F16)],
        vec![(format!("{prefix}x"), f16(&x)?)],
    );
    let (tp, tg) = relayout::step_ports(plans, prefix)?;
    ports.extend(tp);
    given.extend(tg);
    Ok((ports, given))
}

/// `(ports, given)` with each made input's port taken out and its steps'
/// ports appended after the rest, and the shape of each port taken out.
pub fn made_ports(
    (mut ports, mut given): Ports,
    made: &[MadeInput],
    m: &Machine,
) -> R<(Ports, Vec<Vec<i64>>)> {
    let mut shapes = Vec::new();
    for mi in made {
        let at = ports
            .iter()
            .position(|p| p.name == mi.port)
            .expect("a made input's port");
        shapes.push(ports.remove(at).dims);
        given.retain(|(n, _)| *n != mi.port);
    }
    for mi in made {
        let (tp, tg) = input_ports(mi.plans, &mi.prefix, m)?;
        ports.extend(tp);
        given.extend(tg);
    }
    Ok(((ports, given), shapes))
}

/// `prepared.input_buffer`: `plans`' steps into a scratch in the consumer's
/// own row shape (`shape`'s width, at least its rows), no copy after.
pub fn input_buffer(
    t: &mut Trace,
    plans: &[relayout::Plan],
    bufs: &HashMap<String, Buf>,
    prefix: &str,
    shape: &[i64],
    m: &Machine,
) -> R<Buf> {
    let rl = relayout::Hw::of(m);
    let width = shape[shape.len() - 1];
    let lead = if shape.len() == 2 { shape[0] } else { 1 };
    let rows = lead.max(cdiv(plans[plans.len() - 1].dst_words * rl.lanes, width));
    let rows = scratch_rows(rows, width, &rl);
    let dims = if shape.len() == 2 {
        vec![rows, width]
    } else {
        vec![rows * width]
    };
    let dst = t.buffer(&dims, g::Dtype::F16, vec![])?;
    relayout::emit_steps(
        t,
        plans,
        &bufs[&format!("{prefix}x")],
        bufs,
        prefix,
        m,
        Some(&dst),
    )
}

/// The buffers of a kernel built from [`made_ports`], in its ports' order
/// before (`names`), each made input built first ([`input_buffer`]).
pub fn made_bufs(
    t: &mut Trace,
    names: &[String],
    built: &[String],
    b: &[Buf],
    made: &[MadeInput],
    shapes: &[Vec<i64>],
    m: &Machine,
) -> R<Vec<Buf>> {
    let mut p: HashMap<String, Buf> = built.iter().cloned().zip(b.iter().cloned()).collect();
    for (mi, shape) in made.iter().zip(shapes) {
        let x = input_buffer(t, mi.plans, &p, &mi.prefix, shape, m)?;
        p.insert(mi.port.clone(), x);
    }
    Ok(names.iter().map(|n| p[n].clone()).collect())
}

/// The kernel-name part of `made` (`prepkey`): `((i, (plan shapes..)),..)`.
pub fn made_key(made: &BTreeMap<usize, Vec<relayout::Plan>>) -> Py {
    Py::Tuple(
        made.iter()
            .map(|(i, ps)| {
                Py::Tuple(vec![
                    Py::Int(*i as i64),
                    Py::Tuple(ps.iter().map(shape_repr).collect()),
                ])
            })
            .collect(),
    )
}

/// [`v::rows_kernel`] of `e` with operand i of `made` made by its plans
/// (the first from port `x{i}`, tables `p{i}_index{j}` / `p{i}_mask{j}`)
/// into a scratch shaped as its port, operand i of `alias` reading operand
/// `alias[i]`'s.
#[allow(clippy::too_many_arguments)]
pub fn rows_kernel(
    e: &E,
    operands: &[Operand],
    r: i64,
    w: i64,
    out: &Operand,
    made: &BTreeMap<usize, Vec<relayout::Plan>>,
    alias: &BTreeMap<usize, usize>,
    m: &Machine,
) -> R<Kernel> {
    let (hw, rl) = (Hw::of(m), relayout::Hw::of(m));
    let quantum = if v::holds_stat(operands, out.kind) {
        hw.stat_rows()
    } else {
        hw.band
    };
    let pl = v::plan(operands, out.kind, r, w, None, None, quantum, &hw)?;
    if pl.nblk > 1 && expr::reduces(e).len() > 1 {
        return fail(
            "API.ROW.009",
            "several reductions over a row wider than one block",
        );
    }
    let parts = v::stages(&pl, r, quantum);
    let (mut ports, mut given) = (vec![], vec![]);
    let mut shapes: BTreeMap<usize, Vec<i64>> = BTreeMap::new();
    for (i, o) in operands.iter().enumerate() {
        let shape = v::port_shape(o, r, w, &hw)?;
        if let Some(plans) = made.get(&i) {
            let x = [plans[0].src_words, rl.lanes];
            ports.push(Port::input(&format!("x{i}"), &x, g::Dtype::F16));
            given.push((format!("x{i}"), f16(&x)?));
            let (tp, tg) = relayout::step_ports(plans, &format!("p{i}_"))?;
            ports.extend(tp);
            given.extend(tg);
        }
        if made.contains_key(&i) || alias.contains_key(&i) {
            let j = alias.get(&i).copied().unwrap_or(i);
            let held = shapes.entry(j).or_insert_with(|| shape.clone());
            *held = held.clone().max(shape);
        } else {
            ports.push(Port::input(&format!("in{i}"), &shape, g::Dtype::F16));
            given.push((
                format!("in{i}"),
                spec(&shape, o.dtype, vec![], g::BufSpace::Dram)?,
            ));
        }
    }
    let out_shape = if out.kind == Kind::Row {
        let rows = out.rows.max(v::stat_words(r, &hw) * hw.stat_rows());
        if pl.nblk > 1 {
            vec![pl.nblk, rows]
        } else {
            vec![rows]
        }
    } else {
        v::port_shape(out, r, w, &hw)?
    };
    ports.push(Port::output("out", &out_shape, g::Dtype::F16));
    given.push(("out".to_string(), f16(&out_shape)?));
    let made_shapes = made_key(made);
    let key = name(
        "rows",
        vec![
            Py::Str(expr::py(e).repr()),
            Py::Tuple(operands.iter().map(Operand::py).collect()),
            Py::Int(r),
            Py::Int(w),
            out.py(),
            pl.py(),
            made_shapes,
        ],
    );
    let names: Vec<String> = ports.iter().map(|p| p.name.clone()).collect();
    build(&key, m, ports, given, |t, b| {
        let p: HashMap<String, Buf> = names.iter().cloned().zip(b.iter().cloned()).collect();
        let mut ins: BTreeMap<usize, Buf> = BTreeMap::new();
        for (i, plans) in made {
            let lines = relayout::emit_steps(
                t,
                plans,
                &p[&format!("x{i}")],
                &p,
                &format!("p{i}_"),
                m,
                None,
            )?;
            let shape = &shapes[i];
            let values: i64 = shape.iter().product();
            let words = cdiv(values, rl.lanes).min(plans[plans.len() - 1].dst_words);
            let mut laid = vec![scratch_rows(shape[0], values / shape[0], &rl)];
            laid.extend_from_slice(&shape[1..]);
            let laid = t.buffer(&laid, g::Dtype::F16, vec![])?;
            let walk = [(words, rl.word)];
            t.copy(
                &laid.whole(),
                &lines.whole(),
                Some((&walk, None)),
                Some((&walk, None)),
            )?;
            ins.insert(*i, laid);
        }
        for (i, j) in alias {
            let b = ins[j].clone();
            ins.insert(*i, b);
        }
        let bufs = Bufs {
            ins: (0..operands.len())
                .map(|i| ins.get(&i).unwrap_or_else(|| &p[&format!("in{i}")]).clone())
                .collect(),
            out: p["out"].clone(),
        };
        for (sp, row0, rows) in &parts {
            match rows {
                None => v::rows_stage(
                    t,
                    &bufs,
                    e,
                    operands,
                    out,
                    sp,
                    hw.vc,
                    &Idx::Int(*row0),
                    None,
                    &hw,
                )?,
                Some(rows) => v::tail_band(t, &bufs, e, operands, out, sp, *row0, *rows, w, &hw)?,
            }
        }
        Ok(())
    })
}
