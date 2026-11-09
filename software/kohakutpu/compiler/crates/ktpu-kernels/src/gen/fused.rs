//! One package of several vector stages (`ktpugrad/launch/emit.py` groups
//! them): flat and rows stages, each of one or several independent members
//! over one domain, and whole-buffer copies, on named buffers. A flat stage
//! off whole rows goes through a padded scratch `staged`: filled from its
//! port first, written back last. A packed rows step also writes its members'
//! consumer MXFP7 packings ([`Step::Packed`]); a quant step packs a written
//! buffer as the separate quant does ([`Step::Quant`]).

use std::collections::{BTreeMap, HashMap};

use ktpu_graph as g;
use ktpu_machine::Machine;

use super::matmul::{self, QDemand, QUANT_SCHEDULE};
use super::{cdiv, convert::prefix::Prefix, prepared, reductions, relayout, vector as v, Hw};
use crate::expr::{self, Expr, E};
use crate::kernel::{build, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, ALL};
use crate::trace::control::At;
use crate::trace::{fail, Arg, Buf, Cond, Idx, Trace, View, R, V};

/// A named buffer of a package.
#[derive(Clone)]
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
    /// read at the output's index but `(i, lane)` of `lanes`; operand
    /// `ins.len() + k` is member k's packed value, stored where `kept[k]`
    /// (each member a reference reads is stored).
    Flat {
        total: i64,
        dtypes: Vec<g::Dtype>,
        out_dtype: g::Dtype,
        exprs: Vec<E>,
        ins: Vec<String>,
        lanes: Vec<(usize, i64)>,
        outs: Vec<String>,
        kept: Vec<bool>,
    },
    /// `exprs` over [r, w] rows of `operands` into `outs`, read as
    /// `out_operands`; operand i of `made` read composed from its source `ins[i]`.
    Rows {
        /// Fold all column-block partials, with an FP16 boundary at each level.
        fold: bool,
        r: i64,
        w: i64,
        exprs: Vec<E>,
        operands: Vec<v::Operand>,
        ins: Vec<String>,
        out_operands: Vec<v::Operand>,
        outs: Vec<String>,
        made: BTreeMap<usize, prepared::Read>,
    },
    /// [`Step::Flat`]'s members over [r, w] rows of `operands` (operand
    /// `ins.len() + k` member k's f16 value, stored where `outs[k]` is named),
    /// each packing's member also packed checked into its port ([`packed`]).
    Packed {
        r: i64,
        w: i64,
        exprs: Vec<E>,
        operands: Vec<v::Operand>,
        ins: Vec<String>,
        outs: Vec<String>,
        packs: Vec<Packing>,
    },
    /// `src`'s first [rows, pitch] values (row-major) packed checked into
    /// `port` as [`matmul::quant_kernel`] packs them ([`quant`]).
    Quant {
        src: String,
        operand: v::Operand,
        rows: i64,
        cols: i64,
        demand: QDemand,
        port: String,
        staged: bool,
        composed: Option<Vec<relayout::Logical>>,
    },
    Copy {
        src: String,
        dst: String,
    },
    /// Independent strided planes, optionally transposed with caller masks.
    Planes(planes::Planes),
    /// FP16 materialized planar QK/softmax/PV stages on named buffers.
    Attention(attention::Attention),
    /// A bounded pitched copy and optional initialization on named buffers.
    Prefix {
        src: String,
        dst: String,
        boundary: Prefix,
    },
}

/// A consumer's MXFP7 packing of one packed rows member, written into `port`.
pub struct Packing {
    pub member: usize,
    pub port: String,
    pub demand: QDemand,
}

/// A package: its ports (each with whether it is written), scratches and
/// stages in order.
pub struct Package {
    /// Placement of private storage, including each member's temporaries.
    pub scratch_space: Option<g::BufSpace>,
    /// Place successive singleton VC stages on successive local cores.
    pub balance_stages: bool,
    pub ports: Vec<(Named, bool)>,
    pub scratch: Vec<Named>,
    pub staged: Vec<Staged>,
    pub steps: Vec<Step>,
}

pub(crate) mod attention;
pub(crate) mod groups;
pub(crate) mod planes;

type Env = HashMap<String, Buf>;

fn bufs(env: &Env, names: &[String]) -> Vec<Buf> {
    names.iter().map(|n| env[n].clone()).collect()
}

#[allow(clippy::too_many_arguments)]
fn flat(
    t: &mut Trace,
    env: &Env,
    total: i64,
    first: i64,
    dtypes: &[g::Dtype],
    out_dtype: g::Dtype,
    exprs: &[E],
    ins: &[String],
    lanes: &[(usize, i64)],
    outs: &[String],
    kept: &[bool],
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
    let from = bufs(env, ins);
    let to: Vec<Option<Buf>> = outs
        .iter()
        .zip(kept)
        .map(|(n, k)| k.then(|| env[n].clone()))
        .collect();
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
        let n_in = from.len();
        // A member that is a copy of another stores that member's pack, right
        // after it is made.
        let root = |mut c: usize| {
            while let Expr::Load(i) = exprs[c].as_ref() {
                if *i < n_in {
                    break;
                }
                c = *i - n_in;
            }
            c
        };
        for (k, e) in exprs.iter().enumerate() {
            if root(k) != k {
                continue;
            }
            let x = expr::emit(t, e, &vals)?;
            let held = match x {
                Arg::Val(v) if t.value(v).space == g::ValSpace::L1 => v,
                Arg::Val(v) => {
                    let gt4 = t.value(v).mode == Some(g::Mode::Gt4);
                    t.pack(v, out_dtype, gt4)?
                }
                Arg::Num(_) => {
                    return fail(
                        "DSL.VEC.033",
                        "a fused flat member stores a value, not a number",
                    )
                }
            };
            for c in (k..exprs.len()).filter(|&c| root(c) == k) {
                if let Some(b) = &to[c] {
                    t.store(&b.at(&[sl(r, r.add(h)), ALL])?, Arg::Val(held))?;
                }
                vals.insert(n_in + c, Arg::Val(held));
            }
        }
        Ok(())
    };
    v::flat_trips(t, total, rows, hw.vc, &|t, r, h| trip(t, &r.add(first), h))
}

/// Members over one [r, w] domain spread as one rows kernel spreads
/// ([`v::members_stage`]); a `made` operand read composed, the domain tiled as
/// [`prepared::rows_kernel`] tiles it.
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
    made: &BTreeMap<usize, prepared::Read>,
    hw: &Hw,
) -> R<()> {
    let tiled = prepared::word_tiled(exprs, operands, r, w, outs, made, hw);
    let block = tiled.as_ref().map(|_| hw.kblock);
    let (operands, outs, r, w) = match &tiled {
        Some((ops, o, rows, cols)) => (ops.as_slice(), o.as_slice(), *rows, *cols),
        None => (operands, outs, r, w),
    };
    let rest = block.map_or(0, |b| w % b);
    let mut planned = operands.to_vec();
    planned.extend(outs[1..].iter().map(|_| v::Operand::full(w, 0)));
    let quantum = if v::holds_stat(operands, outs[0].kind) {
        hw.stat_rows()
    } else {
        hw.band
    };
    let pl = v::plan(
        &planned,
        outs[0].kind,
        r,
        w - rest,
        None,
        block,
        quantum,
        hw,
    )?;
    let dsts = bufs(env, names);
    let list: Vec<v::Member> = exprs
        .iter()
        .zip(outs)
        .zip(&dsts)
        .map(|((e, out), dst)| v::Member {
            e,
            out: *out,
            dst: dst.clone(),
        })
        .collect();
    let band = v::Bufs {
        ins: bufs(env, ins),
        out: dsts[0].clone(),
        maps: prepared::mapped(made),
    };
    for (sp, row0, tail) in v::stages(&pl, r, quantum) {
        match tail {
            Some(rows) => {
                v::members_tail_band(t, &band, &list, operands, &sp, row0, rows, w - rest, hw)?
            }
            None => {
                let at = Idx::Int(row0);
                v::members_stage(t, &band, &list, operands, &sp, hw.vc, &at, hw)?
            }
        }
    }
    if rest > 0 {
        v::members_tail_columns(t, &band, &list, operands, r, w - rest, rest, quantum, hw)?;
    }
    Ok(())
}

fn gcd(a: i64, b: i64) -> i64 {
    if b == 0 {
        a
    } else {
        gcd(b, a % b)
    }
}

/// The widest column block of [`Step::Packed`] over [r, w]: whole chunks of each
/// demand's K blocks whose band fits the L1; None unless each demand packs all of
/// [r, w] (`cp` = w, `rp` >= r) and r is whole bands.
pub fn packed_block(
    operands: &[v::Operand],
    outs: usize,
    r: i64,
    w: i64,
    demands: &[QDemand],
    hw: &Hw,
) -> Option<i64> {
    let chunk = matmul::packed_columns(demands, hw).ok()?;
    let whole = |d: &QDemand| d.groups > 0 && d.cp == w && d.rp >= r && d.rp % hw.band == 0;
    if r % hw.band != 0 || w % chunk != 0 || !demands.iter().all(whole) {
        return None;
    }
    let mut planned = operands.to_vec();
    planned.extend((1..outs + demands.len()).map(|_| v::Operand::full(w, r)));
    (1..=w.min(hw.max_block()) / chunk)
        .rev()
        .map(|k| k * chunk)
        .filter(|wc| w % wc == 0)
        .find(|&wc| {
            v::plan(
                &planned,
                v::Kind::Full,
                r,
                w,
                Some(1),
                Some(wc),
                hw.band,
                hw,
            )
            .is_ok()
        })
}

/// A member's band as the f16 @l1 value its store writes and later members read.
fn band_f16(t: &mut Trace, x: Arg) -> R<V> {
    match x {
        Arg::Val(v) if t.value(v).space == g::ValSpace::L1 => Ok(v),
        Arg::Val(v) => {
            let gt4 = t.value(v).mode == Some(g::Mode::Gt4);
            t.pack(v, g::Dtype::F16, gt4)
        }
        Arg::Num(_) => fail(
            "DSL.VEC.033",
            "a fused packed member stores a value, not a number",
        ),
    }
}

/// [`Step::Packed`]: an instance a packed tile of rows by a column block (its
/// store views step affinely only inside one tile), a trip one band (L2 packs
/// MXFP7 a band a trip); rows past r to `rp` packed zero.
#[allow(clippy::too_many_arguments)]
fn packed(
    t: &mut Trace,
    env: &Env,
    r: i64,
    w: i64,
    exprs: &[E],
    operands: &[v::Operand],
    ins: &[String],
    outs: &[String],
    packs: &[Packing],
    hw: &Hw,
) -> R<()> {
    let demands: Vec<QDemand> = packs.iter().map(|p| p.demand).collect();
    let stored = outs.iter().filter(|n| !n.is_empty()).count();
    let Some(wc) = packed_block(operands, stored, r, w, &demands, hw) else {
        return fail(
            "API.OP.007",
            format!("packings {demands:?} of [{r}, {w}] rows: whole bands, K whole chunks"),
        );
    };
    let pl = v::Plan {
        wc,
        nblk: w / wc,
        rows: hw.band,
        nb: r / hw.band,
        rp: r,
    };
    // Without packings an instance is a core's share of the trips.
    let per = match demands.is_empty() {
        true => cdiv(pl.nb, hw.vc),
        false => demands.iter().map(|d| d.groups).fold(0, gcd),
    };
    let tile = per * hw.band;
    let n = cdiv(pl.nb, per);
    let rest = pl.nb - (n - 1) * per;
    let record = ktpu_machine::packing::STATUS_RECORD / 2;
    let status = match packs.is_empty() {
        true => None,
        false => Some(t.buffer(&[pl.nblk * n * record], g::Dtype::F16, vec![])?),
    };
    let dsts: Vec<Option<Buf>> = outs
        .iter()
        .map(|n| (!n.is_empty()).then(|| env[n].clone()))
        .collect();
    let qs: Vec<Buf> = packs.iter().map(|p| env[&p.port].clone()).collect();
    let Some(any) = dsts.iter().flatten().next() else {
        return fail("API.OP.001", "a fused packed step stores no member");
    };
    let band = v::Bufs::plain(bufs(env, ins), any.clone());
    let out = v::Operand::full(w, r);
    let on = if n > hw.vc { At::Any } else { At::Var };
    for cb in 0..pl.nblk {
        t.par(n, g::UnitKind::Vc, on, |t, u| {
            let rec = u.add(cb * n).mul(record);
            let status: Option<View> = match &status {
                Some(s) => Some(s.at(&[sl(&rec, rec.add(record))])?),
                None => None,
            };
            let col = Idx::Int(cb * pl.wc);
            let pro = v::prologue(t, &band, operands, &pl, 0, &col)?;
            let trips = |t: &mut Trace, count: i64| {
                t.range(count, |t, k| {
                    let row = u.mul(tile).add(k.mul(hw.band));
                    let mut at = v::Origin::rows(&row, operands.len());
                    for x in at.ins.iter_mut() {
                        x.1 = col.clone();
                    }
                    at.out.1 = col.clone();
                    let mut vals = v::band_values(t, &band, operands, &pl, &at, 0, &pro, hw)?;
                    for (j, (e, dst)) in exprs.iter().zip(&dsts).enumerate() {
                        let x = expr::emit(t, e, &vals)?;
                        let p = band_f16(t, x)?;
                        if let Some(dst) = dst {
                            v::band_store(t, dst, &out, &pl, &at, 0, Arg::Val(p))?;
                        }
                        vals.insert(operands.len() + j, Arg::Val(p));
                        let mine: Vec<&Buf> = packs
                            .iter()
                            .zip(&qs)
                            .filter(|(pk, _)| pk.member == j)
                            .map(|(_, q)| q)
                            .collect();
                        if mine.is_empty() {
                            continue;
                        }
                        if t.value(p).shape != [pl.rows, pl.wc] {
                            return fail(
                                "API.OP.007",
                                "a packed member's band is its own values, not a broadcast",
                            );
                        }
                        let back = t.unpack(p, None)?;
                        for q in mine {
                            let s = status.as_ref().expect("a packing's record");
                            let qv = t.pack_checked(back, q.spec.dtype, s)?;
                            let view =
                                q.at(&[sl(&row, row.add(hw.band)), sl(&col, col.add(pl.wc))])?;
                            t.store(&view, Arg::Val(qv))?;
                        }
                    }
                    Ok(())
                })
            };
            if rest == per {
                return trips(t, per);
            }
            t.when(Cond::new(&u, g::CmpOp::Lt, n - 1), |t| trips(t, per))?;
            t.when(Cond::new(&u, g::CmpOp::Eq, n - 1), |t| trips(t, rest))
        })?;
    }
    for (p, q) in packs.iter().zip(&qs) {
        let rp = p.demand.rp;
        if rp > r {
            let z = t.buffer(&[rp - r, w], g::Dtype::F16, vec![])?;
            t.fill_value(&z.whole(), 0, None, None)?;
            t.quant_scheduled(
                &q.at(&[sl(r, rp), ALL])?,
                &z.whole(),
                true,
                QUANT_SCHEDULE,
                false,
            )?;
        }
    }
    match &status {
        Some(s) => t.quant_check(&s.whole()),
        None => Ok(()),
    }
}

/// [`Step::Quant`]: the separate quant's checked packing of `src`, read through
/// `composed`, else from [rows, pitch] whole words (copied out where `src` is not so).
#[allow(clippy::too_many_arguments)]
fn quant(
    t: &mut Trace,
    env: &Env,
    src: &str,
    operand: &v::Operand,
    rows: i64,
    cols: i64,
    d: &QDemand,
    port: &str,
    staged: bool,
    composed: Option<&[relayout::Logical]>,
    m: &Machine,
    hw: &Hw,
) -> R<()> {
    let (x, q) = (&env[src], &env[port]);
    if let Some(steps) = composed {
        let (qv, xv) = (q.whole(), x.whole());
        return matmul::quant_composed(
            t,
            &qv,
            &xv,
            operand,
            rows,
            cols,
            d.rp,
            1,
            false,
            QUANT_SCHEDULE,
            steps,
        );
    }
    let laid = [rows, operand.pitch];
    let x = match x.shape() == laid {
        true => x.clone(),
        false => {
            let z = t.buffer(&laid, g::Dtype::F16, vec![])?;
            let walk = [(rows * operand.pitch / hw.lanes, hw.word_bytes)];
            t.copy(
                &z.whole(),
                &x.whole(),
                Some((&walk, None)),
                Some((&walk, None)),
            )?;
            z
        }
    };
    matmul::quant_into(
        t,
        &x,
        q,
        operand,
        rows,
        cols,
        d.rp,
        d.cp,
        d.nk,
        1,
        m,
        false,
        QUANT_SCHEDULE,
        staged,
        false,
    )
}

/// The extents of each port a rows step with a composed read names, its
/// domain tiled ([`prepared::word_tiled`]): the step's own ports, the same
/// bytes.
fn tiled_ports(p: &Package, hw: &Hw) -> R<HashMap<String, Vec<i64>>> {
    let mut shapes = HashMap::new();
    for step in &p.steps {
        let Step::Rows {
            r,
            w,
            exprs,
            operands,
            ins,
            out_operands,
            outs,
            made,
            ..
        } = step
        else {
            continue;
        };
        let tiled = prepared::word_tiled(exprs, operands, *r, *w, out_operands, made, hw);
        let Some((ops, out_ops, rows, cols)) = tiled else {
            continue;
        };
        let plain = ins.iter().zip(&ops).enumerate();
        for (_, (name, o)) in plain.filter(|(i, _)| !made.contains_key(i)) {
            shapes.insert(name.clone(), v::port_shape(o, rows, cols, hw)?);
        }
        for (name, o) in outs.iter().zip(&out_ops) {
            shapes.insert(name.clone(), v::port_shape(o, rows, cols, hw)?);
        }
    }
    Ok(shapes)
}

/// The kernel of `p`, its ports bound by name.
pub fn kernel(p: &Package, key: &str, m: &Machine) -> R<Kernel> {
    kernel_at(p, key, None, m, true)
}

/// One complete-VL range of a flat stage in the package's global buffers.
pub(crate) fn flat_range_kernel(
    p: &Package,
    first: i64,
    count: i64,
    key: &str,
    m: &Machine,
) -> R<Kernel> {
    let key = name(
        "flat_range",
        vec![Py::str(key), Py::Int(first), Py::Int(count)],
    );
    kernel_at(p, &key, Some((first, count)), m, true)
}

/// Complete local storage/ordering selection at the package boundary.
pub(crate) fn scheduled(p: &Package, a: &serde_json::Value, key: &str, m: &Machine) -> R<Kernel> {
    let kernel = unfinalized(p, key, m)?;
    super::chain::sequence::finish(kernel, a, m)
}

pub(crate) fn unfinalized(p: &Package, key: &str, m: &Machine) -> R<Kernel> {
    kernel_at(p, key, None, m, false)
}

fn kernel_at(
    p: &Package,
    key: &str,
    range: Option<(i64, i64)>,
    m: &Machine,
    pool: bool,
) -> R<Kernel> {
    let hw = Hw::of(m);
    for step in &p.steps {
        if let Step::Planes(planes) = step {
            planes.validate(p, m)?;
        }
        if let Step::Attention(attention) = step {
            if let Some(merge) = &attention.merge {
                merge.validate(p, m)?;
            }
            if attention.compact_mask
                && attention.tail.as_ref().is_some_and(|name| {
                    !p.ports
                        .iter()
                        .any(|(buffer, written)| &buffer.name == name && !written)
                        || p.steps.iter().any(|step| match step {
                            Step::Flat { outs, .. } | Step::Rows { outs, .. } => {
                                outs.contains(name)
                            }
                            Step::Packed { outs, packs, .. } => {
                                outs.contains(name) || packs.iter().any(|pack| &pack.port == name)
                            }
                            Step::Quant { port, .. } => port == name,
                            Step::Copy { dst, .. } | Step::Prefix { dst, .. } => dst == name,
                            Step::Planes(planes) => &planes.dst == name,
                            Step::Attention(attention) => {
                                &attention.out == name
                                    || attention
                                        .merge
                                        .as_ref()
                                        .is_some_and(|merge| &merge.dst == name)
                            }
                        })
                })
            {
                return fail(
                    "API.OP.001",
                    "reused attention mask needs an immutable public tail",
                );
            }
            if p.ports
                .iter()
                .any(|(buffer, written)| buffer.name == attention.out && !written)
            {
                return fail("API.OP.001", "attention output must be writable");
            }
        }
        if let Step::Prefix { src, dst, boundary } = step {
            boundary.validate(m)?;
            for (name, shape) in [
                (src, [boundary.src_rows, boundary.src_pitch]),
                (dst, [boundary.dst_rows, boundary.dst_pitch]),
            ] {
                let declared = p
                    .ports
                    .iter()
                    .map(|(buffer, _)| buffer)
                    .chain(p.scratch.iter())
                    .find(|buffer| &buffer.name == name);
                if !declared
                    .is_some_and(|buffer| buffer.dtype == g::Dtype::F16 && buffer.shape == shape)
                {
                    return fail(
                        "API.OP.001",
                        "fused prefix must match its named FP16 allocations",
                    );
                }
            }
            if src == dst && boundary.initializes_source() {
                return fail(
                    "API.OP.001",
                    "prefix initialization overlaps its copied source",
                );
            }
        }
    }
    let tiled = tiled_ports(p, &hw)?;
    let mut ports = Vec::new();
    let mut given = Vec::new();
    for (b, written) in &p.ports {
        let shape = tiled.get(&b.name).unwrap_or(&b.shape);
        let port = if *written {
            Port::output(&b.name, shape, b.dtype)
        } else {
            Port::input(&b.name, shape, b.dtype)
        };
        ports.push(port);
        given.push((
            b.name.clone(),
            spec(shape, b.dtype, vec![], g::BufSpace::Dram)?,
        ));
    }
    let mut names: Vec<String> = p.ports.iter().map(|(n, _)| n.name.clone()).collect();
    let mut packings: Vec<(&QDemand, &String)> = vec![];
    for step in &p.steps {
        match step {
            Step::Packed { packs, .. } => {
                packings.extend(packs.iter().map(|pk| (&pk.demand, &pk.port)))
            }
            Step::Quant { demand, port, .. } => packings.push((demand, port)),
            _ => {}
        }
    }
    for (demand, name) in packings {
        let (port, named) = demand.named_port(name)?;
        ports.push(port);
        given.push(named);
        names.push(name.clone());
    }
    let key = name("rows", vec![Py::str("fused"), Py::str(key)]);
    let mut kernel = build(&key, m, ports, given, |t, b| {
        let mut env: Env = names.iter().cloned().zip(b.iter().cloned()).collect();
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
        let mut attention_masks = attention::MaskCache::new();
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
                    kept,
                } => flat(
                    t,
                    &env,
                    range.map_or(*total, |(_, count)| count),
                    range.map_or(0, |(first, _)| first),
                    dtypes,
                    *out_dtype,
                    exprs,
                    ins,
                    lanes,
                    outs,
                    kept,
                    &hw,
                )?,
                Step::Rows {
                    fold,
                    r,
                    w,
                    exprs,
                    operands,
                    ins,
                    out_operands,
                    outs,
                    made,
                } => {
                    if *fold {
                        let inputs = bufs(&env, ins);
                        for ((e, out), dst) in exprs.iter().zip(out_operands).zip(outs) {
                            reductions::wide_fold(
                                t,
                                &inputs,
                                env[dst].clone(),
                                e,
                                operands,
                                *r,
                                *w,
                                out,
                                &hw,
                            )?;
                        }
                    } else {
                        members(
                            t,
                            &env,
                            *r,
                            *w,
                            exprs,
                            operands,
                            ins,
                            out_operands,
                            outs,
                            made,
                            &hw,
                        )?;
                    }
                }
                Step::Packed {
                    r,
                    w,
                    exprs,
                    operands,
                    ins,
                    outs,
                    packs,
                } => packed(t, &env, *r, *w, exprs, operands, ins, outs, packs, &hw)?,
                Step::Quant {
                    src,
                    operand,
                    rows,
                    cols,
                    demand,
                    port,
                    staged,
                    composed,
                } => quant(
                    t,
                    &env,
                    src,
                    operand,
                    *rows,
                    *cols,
                    demand,
                    port,
                    *staged,
                    composed.as_deref(),
                    m,
                    &hw,
                )?,
                Step::Copy { src, dst } => {
                    t.copy(&env[dst].whole(), &env[src].whole(), None, None)?
                }
                Step::Planes(planes) => planes.emit(t, &env, range, &hw)?,
                Step::Attention(attention) => attention.emit(t, &env, m, &mut attention_masks)?,
                Step::Prefix { src, dst, boundary } => {
                    let (first, count) = range.unwrap_or((0, boundary.rows));
                    boundary.emit_into(t, &env[src], &env[dst], first, count, &hw)?;
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
    })?;
    if let Some(space) = p.scratch_space {
        for scratch in &mut kernel.graph.scratch {
            scratch.spec.space = space;
        }
    }
    if p.balance_stages {
        balance_stages(&mut kernel.graph.body, hw.vc);
    }
    if pool {
        super::chain::reuse_stage(kernel, m)
    } else {
        Ok(kernel)
    }
}

/// Local instance placement; each explicitly scoped sysnode has its own cursor.
fn balance_stages(body: &mut [g::Stmt], cores: i64) {
    let mut next = 0;
    for stmt in body {
        if let g::Node::Par {
            lo, hi, unit, body, ..
        } = &mut stmt.node
        {
            if unit.kind == g::UnitKind::Node {
                balance_stages(body, cores);
            } else if unit.kind == g::UnitKind::Vc
                && *lo == g::Index::Int(0)
                && *hi == g::Index::Int(1)
            {
                unit.index = Some(g::Index::Int(next % cores));
                next += 1;
            }
        }
    }
}

/// Destination-owned prefix stage with the package's global public signature.
/// The shared prefix planner supplies exact reads, padding-only owners and
/// destination-line writes; only allocation names differ in a fused package.
pub(crate) fn prefix_split(
    p: &Package,
    key: &str,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    let [Step::Prefix { src, dst, boundary }] = p.steps.as_slice() else {
        return fail("API.OP.001", "a prefix split needs one boundary stage");
    };
    let signature = kernel(p, key, m)?;
    let rename = |region: &mut crate::split::Region| {
        region.allocation = if region.allocation == "src" {
            src.clone()
        } else {
            dst.clone()
        };
    };
    let mut options = boundary.options(request, m)?;
    for option in &mut options {
        for region in &mut option.output.valid_region {
            rename(region);
        }
        for (_, region) in &mut option.output.ownership {
            rename(region);
        }
        for shard in &mut option.shards {
            for region in shard
                .read_regions
                .iter_mut()
                .chain(&mut shard.write_regions)
            {
                rename(region);
            }
        }
    }
    v::split::kernel_options(options, signature, request, m, |first, count| {
        let key = name(
            "prefix_range",
            vec![Py::str(key), Py::Int(first), Py::Int(count)],
        );
        kernel_at(p, &key, Some((first, count)), m, true)
    })
}
