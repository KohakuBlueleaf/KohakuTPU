//! One package of several vector stages (`ktpugrad/launch_emit.py` groups
//! them): flat and rows stages, each of one or several independent members
//! over one domain, and whole-buffer copies, on named buffers. A flat stage
//! off whole rows goes through a padded scratch `staged`: filled from its
//! port first, written back last.

use std::collections::HashMap;

use ktpu_graph as g;
use ktpu_machine::Machine;

use super::{reductions, vector as v, Hw};
use crate::expr::{self, E};
use crate::kernel::{build, Kernel, Port};
use crate::mutants;
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, ALL};
use crate::trace::control::At;
use crate::trace::{Arg, Buf, Idx, Trace, R};

/// The reverse mutant of [`members`]: a band stores its first member alone.
pub const LOST_MEMBER_MUTANT: &str = "fused/lost-member";

/// A named buffer of a package.
pub struct Named {
    pub name: String,
    pub shape: Vec<i64>,
    pub dtype: g::Dtype,
}

/// A padded scratch standing for `words` whole words of a port.
pub struct Staged {
    pub scratch: String,
    pub port: String,
    pub words: i64,
    /// Filled from the port before the stages.
    pub read: bool,
    /// Written to the port after the stages.
    pub written: bool,
}

/// One stage of a package.
pub enum Step {
    /// `exprs` into `outs` over `total` rows of `vc.vlmax` values, every
    /// read at the output's index but `(i, lane)` of `lanes`.
    Flat {
        total: i64,
        dtypes: Vec<g::Dtype>,
        out_dtype: g::Dtype,
        exprs: Vec<E>,
        ins: Vec<String>,
        lanes: Vec<(usize, i64)>,
        outs: Vec<String>,
    },
    /// `exprs` over [r, w] rows of `operands` into `outs`, read as
    /// `out_operands`.
    Rows {
        r: i64,
        w: i64,
        exprs: Vec<E>,
        operands: Vec<v::Operand>,
        ins: Vec<String>,
        out_operands: Vec<v::Operand>,
        outs: Vec<String>,
    },
    Copy {
        src: String,
        dst: String,
    },
}

/// A package: its ports (each with whether it is written), scratches and
/// stages in order.
pub struct Package {
    pub ports: Vec<(Named, bool)>,
    pub scratch: Vec<Named>,
    pub staged: Vec<Staged>,
    pub steps: Vec<Step>,
}

type Env = HashMap<String, Buf>;

fn bufs(env: &Env, names: &[String]) -> Vec<Buf> {
    names.iter().map(|n| env[n].clone()).collect()
}

#[allow(clippy::too_many_arguments)]
fn flat(
    t: &mut Trace,
    env: &Env,
    total: i64,
    dtypes: &[g::Dtype],
    out_dtype: g::Dtype,
    exprs: &[E],
    ins: &[String],
    lanes: &[(usize, i64)],
    outs: &[String],
    hw: &Hw,
) -> R<()> {
    let lane_of = |i: usize| lanes.iter().find(|(j, _)| *j == i).map(|(_, l)| *l);
    let reads: Vec<g::Dtype> = dtypes
        .iter()
        .enumerate()
        .filter(|(i, _)| lane_of(*i).is_none())
        .map(|(_, d)| *d)
        .collect();
    let rows = v::flat_rows(&reads, lanes.len(), &vec![out_dtype; outs.len()], hw);
    let (from, to) = (bufs(env, ins), bufs(env, outs));
    let trip = |t: &mut Trace, r: &Idx, h: i64| -> R<()> {
        let mut vals = HashMap::new();
        for (i, b) in from.iter().enumerate() {
            let x = match lane_of(i) {
                Some(l) => {
                    let x = t.load(&b.at(&[sl(0, hw.lanes)])?, false)?;
                    t.unpack(x, Some(l))?
                }
                None => t.load(&b.at(&[sl(r, r.add(h)), ALL])?, false)?,
            };
            vals.insert(i, Arg::Val(x));
        }
        for (e, b) in exprs.iter().zip(&to) {
            let x = expr::emit(t, e, &vals)?;
            t.store(&b.at(&[sl(r, r.add(h)), ALL])?, x)?;
        }
        Ok(())
    };
    v::flat_trips(t, total, rows, hw.vc, &trip)
}

/// Several members over one [r, w] domain, one read of each operand a band
/// of all of them.
#[allow(clippy::too_many_arguments)]
fn members(
    t: &mut Trace,
    env: &Env,
    r: i64,
    w: i64,
    exprs: &[E],
    operands: &[v::Operand],
    ins: &[String],
    outs: &[v::Operand],
    names: &[String],
    hw: &Hw,
) -> R<()> {
    let mut planned = operands.to_vec();
    planned.extend(outs[1..].iter().map(|_| v::Operand::full(w, 0)));
    let quantum = if v::holds_stat(operands, outs[0].kind) {
        hw.stat_rows()
    } else {
        hw.band
    };
    let pl = v::plan(&planned, outs[0].kind, r, w, None, None, quantum, hw)?;
    let (ins, dsts) = (bufs(env, ins), bufs(env, names));
    for (sp, row0, tail) in v::stages(&pl, r, quantum) {
        if let Some(rows) = tail {
            for ((e, out), dst) in exprs.iter().zip(outs).zip(&dsts) {
                let band = v::Bufs {
                    ins: ins.clone(),
                    out: dst.clone(),
                };
                v::tail_band(t, &band, e, operands, out, &sp, row0, rows, w, hw)?;
            }
            continue;
        }
        let band = v::Bufs {
            ins: ins.clone(),
            out: dsts[0].clone(),
        };
        for cb in 0..sp.nblk {
            let split = v::split(sp.nb, hw.vc);
            t.par(split.cores, g::UnitKind::Vc, At::Var, |t, c| {
                let (first, trips) = (split.start(&c), split.trips(&c));
                let pro = v::prologue(t, &band, operands, &sp, cb, &Idx::Int(0))?;
                t.range(trips, |t, k| {
                    let r = first.mul(sp.rows).add(k.mul(sp.rows)).add(row0);
                    let at = v::Origin::rows(&r, operands.len());
                    let vals = v::band_values(t, &band, operands, &sp, &at, cb, &pro, hw)?;
                    let stored = if mutants::on(LOST_MEMBER_MUTANT) {
                        1
                    } else {
                        exprs.len()
                    };
                    for ((e, out), dst) in exprs.iter().zip(outs).zip(&dsts).take(stored) {
                        let x = expr::emit(t, e, &vals)?;
                        v::band_store(t, dst, out, &sp, &at, cb, x)?;
                    }
                    Ok(())
                })
            })?;
        }
    }
    Ok(())
}

/// The kernel of `p`, its ports bound by name.
pub fn kernel(p: &Package, key: &str, m: &Machine) -> R<Kernel> {
    let hw = Hw::of(m);
    let mut ports = Vec::new();
    let mut given = Vec::new();
    for (b, written) in &p.ports {
        let port = if *written {
            Port::output(&b.name, &b.shape, b.dtype)
        } else {
            Port::input(&b.name, &b.shape, b.dtype)
        };
        ports.push(port);
        given.push((
            b.name.clone(),
            spec(&b.shape, b.dtype, vec![], g::BufSpace::Dram)?,
        ));
    }
    let key = name("rows", vec![Py::str("fused"), Py::str(key)]);
    build(&key, m, ports, given, |t, b| {
        let mut env: Env = p
            .ports
            .iter()
            .map(|(n, _)| n.name.clone())
            .zip(b.iter().cloned())
            .collect();
        for s in &p.scratch {
            let buf = t.buffer(&s.shape, s.dtype, vec![])?;
            env.insert(s.name.clone(), buf);
        }
        for s in p.staged.iter().filter(|s| s.read) {
            let walk = [(s.words, hw.word_bytes)];
            t.fill_value(&env[&s.scratch].whole(), 0, None, None)?;
            t.copy(
                &env[&s.scratch].whole(),
                &env[&s.port].whole(),
                Some((&walk, None)),
                Some((&walk, None)),
            )?;
        }
        for step in &p.steps {
            match step {
                Step::Flat {
                    total,
                    dtypes,
                    out_dtype,
                    exprs,
                    ins,
                    lanes,
                    outs,
                } => flat(
                    t, &env, *total, dtypes, *out_dtype, exprs, ins, lanes, outs, &hw,
                )?,
                Step::Rows {
                    r,
                    w,
                    exprs,
                    operands,
                    ins,
                    out_operands,
                    outs,
                } if exprs.len() == 1 => reductions::emit(
                    t,
                    bufs(&env, ins),
                    env[&outs[0]].clone(),
                    &exprs[0],
                    operands,
                    *r,
                    *w,
                    &out_operands[0],
                    &hw,
                )?,
                Step::Rows {
                    r,
                    w,
                    exprs,
                    operands,
                    ins,
                    out_operands,
                    outs,
                } => members(
                    t,
                    &env,
                    *r,
                    *w,
                    exprs,
                    operands,
                    ins,
                    out_operands,
                    outs,
                    &hw,
                )?,
                Step::Copy { src, dst } => {
                    t.copy(&env[dst].whole(), &env[src].whole(), None, None)?
                }
            }
        }
        for s in p.staged.iter().filter(|s| s.written) {
            let walk = [(s.words, hw.word_bytes)];
            t.copy(
                &env[&s.port].whole(),
                &env[&s.scratch].whole(),
                Some((&walk, None)),
                Some((&walk, None)),
            )?;
        }
        Ok(())
    })
}
