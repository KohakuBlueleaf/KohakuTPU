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

/// A made operand written to a line buffer and copied into its port.
pub const COPIED_MUTANT: &str = "kernels.prepared/operand-copied";

/// The reverse mutant of the [`Composed`] default.
pub const MATERIALIZED_MUTANT: &str = "pipe/materialized";

/// When a pending operand's kernel reads it composed (L2.READ.001).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Composed {
    /// Never: its plans run first.
    Off,
    /// Wherever the composed kernel lowers.
    On,
    /// Where it lowers and costs no more than materialized.
    Cost,
}

impl Composed {
    pub fn name(self) -> &'static str {
        match self {
            Composed::Off => "off",
            Composed::On => "on",
            Composed::Cost => "cost",
        }
    }

    pub fn named(name: &str) -> Option<Composed> {
        [Composed::Off, Composed::On, Composed::Cost]
            .into_iter()
            .find(|c| c.name() == name)
    }
}

/// The planner knobs of pending operands (Python's `runtime/forms.py`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Knobs {
    /// A laid-out operand is made inside the kernel that reads it
    /// (`forms.PREPARED`).
    pub prepared: bool,
    /// A packing's operand is deferred to the packing's kernel when a
    /// measured recipe wins (`forms.PREPARED_QUANT`).
    pub prepared_quant: bool,
    /// `forms.COMPOSED`.
    pub composed: Composed,
}

/// The knobs' defaults: the one source the planner and Python
/// (`_rs.PREPARED_DEFAULTS`) read; separate relayouts under
/// [`SEPARATE_MUTANT`], no composed read under [`MATERIALIZED_MUTANT`].
pub fn defaults() -> Knobs {
    Knobs {
        prepared: !mutants::on(SEPARATE_MUTANT),
        prepared_quant: true,
        composed: if mutants::on(MATERIALIZED_MUTANT) {
            Composed::Off
        } else {
            Composed::On
        },
    }
}

/// How a kernel reads a pending operand.
#[derive(Clone, Debug, PartialEq)]
pub enum Read {
    /// Its plans run in the kernel first, into a scratch of the operand.
    Materialized(Vec<relayout::Plan>),
    /// Its steps stated on the loads and packs that read it (L2.READ.001):
    /// the source port alone, no table, no scratch.
    Composed(Vec<relayout::Logical>),
}

impl Read {
    /// The form in a kernel's name.
    pub fn py(&self) -> Py {
        match self {
            Read::Materialized(plans) => Py::Tuple(plans.iter().map(shape_repr).collect()),
            Read::Composed(steps) => Py::Tuple(vec![
                Py::str("composed"),
                Py::Tuple(steps.iter().map(relayout::Logical::py).collect()),
            ]),
        }
    }

    /// `read_map`'s value.
    pub fn steps(steps: &[relayout::Logical]) -> g::Attr {
        g::Attr::Tuple(steps.iter().map(relayout::Logical::attr).collect())
    }

    /// Words of its source port.
    fn source_words(&self, lanes: i64) -> i64 {
        match self {
            Read::Materialized(plans) => plans[0].src_words,
            Read::Composed(steps) => cdiv(steps[0].n, lanes),
        }
    }
}

/// The operands of `made` read composed, as a rows stage takes them.
pub fn mapped(made: &BTreeMap<usize, Read>) -> HashMap<usize, v::Mapped> {
    made.iter()
        .filter_map(|(i, read)| match read {
            Read::Composed(steps) => Some((
                *i,
                v::Mapped {
                    steps: Read::steps(steps),
                    rows: None,
                },
            )),
            Read::Materialized(_) => None,
        })
        .collect()
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

/// An input a kernel reads through relayouts: the port it stands for, the
/// steps' port prefix, its read.
pub struct MadeInput<'a> {
    pub port: String,
    pub prefix: String,
    pub read: &'a Read,
}

/// The inputs `made` (operand index -> read) of a kernel whose operand i
/// is port `in{i}` (`p{i}_` its steps').
pub fn made_inputs(made: &BTreeMap<usize, Read>) -> Vec<MadeInput<'_>> {
    made.iter()
        .map(|(i, read)| MadeInput {
            port: format!("in{i}"),
            prefix: format!("p{i}_"),
            read,
        })
        .collect()
}

/// `prepared.input_ports`: `read`'s source `{prefix}x` and any tables.
pub fn input_ports(read: &Read, prefix: &str, m: &Machine) -> R<Ports> {
    let lanes = relayout::Hw::of(m).lanes;
    let x = [read.source_words(lanes), lanes];
    let (mut ports, mut given) = (
        vec![Port::input(&format!("{prefix}x"), &x, g::Dtype::F16)],
        vec![(format!("{prefix}x"), f16(&x)?)],
    );
    if let Read::Materialized(plans) = read {
        let (tp, tg) = relayout::step_ports(plans, prefix)?;
        ports.extend(tp);
        given.extend(tg);
    }
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
        let (tp, tg) = input_ports(mi.read, &mi.prefix, m)?;
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
/// before (`names`), each materialized input built first ([`input_buffer`]),
/// a composed one its source.
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
        let x = match mi.read {
            Read::Materialized(plans) => input_buffer(t, plans, &p, &mi.prefix, shape, m)?,
            Read::Composed(_) => p[&format!("{}x", mi.prefix)].clone(),
        };
        p.insert(mi.port.clone(), x);
    }
    Ok(names.iter().map(|n| p[n].clone()).collect())
}

/// The kernel-name part of `made` (`prepkey`): `((i, read),..)`.
pub fn made_key(made: &BTreeMap<usize, Read>) -> Py {
    Py::Tuple(
        made.iter()
            .map(|(i, read)| Py::Tuple(vec![Py::Int(*i as i64), read.py()]))
            .collect(),
    )
}

/// [`v::rows_kernel`] of `e` with operand i of `made` read from port `x{i}`
/// by its [`Read`] (materialized: tables `p{i}_index{j}` / `p{i}_mask{j}`,
/// a scratch shaped as its port), operand i of `alias` reading operand
/// `alias[i]`'s.
#[allow(clippy::too_many_arguments)]
pub fn rows_kernel(
    e: &E,
    operands: &[Operand],
    r: i64,
    w: i64,
    out: &Operand,
    made: &BTreeMap<usize, Read>,
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
        if let Some(read) = made.get(&i) {
            let x = [read.source_words(rl.lanes), rl.lanes];
            ports.push(Port::input(&format!("x{i}"), &x, g::Dtype::F16));
            given.push((format!("x{i}"), f16(&x)?));
            if let Read::Materialized(plans) = read {
                let (tp, tg) = relayout::step_ports(plans, &format!("p{i}_"))?;
                ports.extend(tp);
                given.extend(tg);
            }
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
        let mut maps = mapped(made);
        for (i, read) in made {
            let Read::Materialized(plans) = read else {
                ins.insert(*i, p[&format!("x{i}")].clone());
                continue;
            };
            let (x, prefix) = (&p[&format!("x{i}")], format!("p{i}_"));
            let shape = &shapes[i];
            let values: i64 = shape.iter().product();
            let dst_words = plans[plans.len() - 1].dst_words;
            let words = cdiv(values, rl.lanes).min(dst_words);
            let mut dims = vec![scratch_rows(shape[0], values / shape[0], &rl)];
            dims.extend_from_slice(&shape[1..]);
            if words == dst_words && direct_destination(plans) && !mutants::on(COPIED_MUTANT) {
                let laid = t.buffer(&dims, g::Dtype::F16, vec![])?;
                relayout::emit_steps(t, plans, x, &p, &prefix, m, Some(&laid))?;
                ins.insert(*i, laid);
                continue;
            }
            let lines = relayout::emit_steps(t, plans, x, &p, &prefix, m, None)?;
            let laid = t.buffer(&dims, g::Dtype::F16, vec![])?;
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
            if let Some(read) = maps.get(j).cloned() {
                maps.insert(*i, read);
            }
        }
        let bufs = Bufs {
            ins: (0..operands.len())
                .map(|i| ins.get(&i).unwrap_or_else(|| &p[&format!("in{i}")]).clone())
                .collect(),
            out: p["out"].clone(),
            maps,
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
