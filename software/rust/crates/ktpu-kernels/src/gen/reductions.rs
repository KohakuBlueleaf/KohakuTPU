//! Single-package reductions (`runtime/launch_reductions.py`): a reduction
//! across a row wider than one column block (its per-block partials halved
//! to one) and a reduction down the rows (a halving tree), each one kernel
//! whose every intermediate is an f16 scratch the next stage reads back.

use std::collections::BTreeMap;
use std::rc::Rc;

use ktpu_graph as g;
use ktpu_machine::Machine;

use super::prepared::{self, Read};
use super::{relayout, vector as v, Hw};
use crate::expr::{self, Expr, OpName, RedOp, E};
use crate::kernel::{build, Kernel, Port};
use crate::mutants;
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, ALL};
use crate::trace::{Buf, Idx, Trace, R};

/// The reverse mutant of the single-package default: every wide row and
/// column reduction planned as separate launches again.
pub const SEPARATE_MUTANT: &str = "rows/separate-launches";

/// The planner knobs of a rows reduction (Python's `runtime/rows.py`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Knobs {
    /// A wide row reduction and a column reduction are each one kernel
    /// (`rows.SINGLE_PACKAGE_AST`), not a launch a stage.
    pub single_package_ast: bool,
}

/// The knobs' defaults: the one source the planner and Python
/// (`_rs.ROWS_DEFAULTS`) read; separate launches under [`SEPARATE_MUTANT`].
pub fn defaults() -> Knobs {
    Knobs {
        single_package_ast: !mutants::on(SEPARATE_MUTANT),
    }
}

/// Python's `rows.MUTANT` modes, by mutant name: the first fold level's
/// partials left out (`drop_member`), the block partials computed after
/// their fold (`wrong_order`), a fold reading one member twice or a tree
/// level writing over its own input (`alias_members`).
pub const EMITTER_MUTANTS: [(&str, &str); 3] = [
    ("reductions/drop_member", "drop_member"),
    ("reductions/wrong_order", "wrong_order"),
    ("reductions/alias_members", "alias_members"),
];

/// The emitter mode on, as Python names it in a kernel's key.
fn mode() -> Option<&'static str> {
    EMITTER_MUTANTS
        .iter()
        .find(|(n, _)| mutants::on(n))
        .map(|(_, m)| *m)
}

fn mode_py() -> Py {
    mode().map_or(Py::None, Py::str)
}

/// How two partials of a reduction combine (`rows.FOLD`, `tree.OP`).
fn fold(op: RedOp) -> OpName {
    match op {
        RedOp::Sum => OpName::Add,
        RedOp::Max => OpName::Max,
        RedOp::Prod => OpName::Mul,
    }
}

fn both(op: OpName, a: E, b: E) -> E {
    Rc::new(Expr::Op(op, vec![a, b]))
}

/// `rows.substitute(e, red, Load(0))`: `e` with `red` read as `Load(0)` and
/// the reads left outside it numbered from 1 in first-read order, with the
/// original indices of those reads.
fn substitute(e: &E, red: &E, keep: &mut Vec<usize>) -> E {
    if Rc::ptr_eq(e, red) {
        return expr::load(0);
    }
    match e.as_ref() {
        Expr::Load(i) => {
            let j = keep.iter().position(|k| k == i).unwrap_or_else(|| {
                keep.push(*i);
                keep.len() - 1
            });
            expr::load(1 + j)
        }
        Expr::Op(op, args) => Rc::new(Expr::Op(
            *op,
            args.iter().map(|a| substitute(a, red, keep)).collect(),
        )),
        Expr::Reduce(op, a) => Rc::new(Expr::Reduce(*op, substitute(a, red, keep))),
        Expr::Const(_) => e.clone(),
    }
}

/// `tree._renumber`: every read `by` further on.
fn renumber(e: &E, by: usize) -> E {
    match e.as_ref() {
        Expr::Load(i) => expr::load(i + by),
        Expr::Const(_) => e.clone(),
        Expr::Op(op, args) => Rc::new(Expr::Op(
            *op,
            args.iter().map(|a| renumber(a, by)).collect(),
        )),
        Expr::Reduce(op, a) => Rc::new(Expr::Reduce(*op, renumber(a, by))),
    }
}

/// `tree._place`: `post` with its `Load(0)` as `head` and its j-th other
/// read after the `base` head reads.
fn place(post: &E, head: &E, base: usize) -> E {
    match post.as_ref() {
        Expr::Load(0) => head.clone(),
        Expr::Load(i) => expr::load(base + i - 1),
        Expr::Op(op, args) => Rc::new(Expr::Op(
            *op,
            args.iter().map(|a| place(a, head, base)).collect(),
        )),
        _ => post.clone(),
    }
}

/// `tree._shift`: `o` read `rows` rows further down.
fn shift(o: &v::Operand, rows: i64, w: i64) -> v::Operand {
    let by = match o.kind {
        v::Kind::Full | v::Kind::Tile => rows * if o.pitch == 0 { w } else { o.pitch },
        v::Kind::Row => rows,
        _ => 0,
    };
    v::Operand {
        offset: o.offset + by,
        ..*o
    }
}

/// `launch_reductions.emit_rows_stages`: every full and tail row stage of
/// `e` over [r, w] into the kernel being traced.
#[allow(clippy::too_many_arguments)]
fn emit(
    t: &mut Trace,
    ins: Vec<Buf>,
    out: Buf,
    e: &E,
    ops: &[v::Operand],
    r: i64,
    w: i64,
    dst: &v::Operand,
    hw: &Hw,
) -> R<()> {
    let quantum = if v::holds_stat(ops, dst.kind) {
        hw.stat_rows()
    } else {
        hw.band
    };
    let pl = v::plan(ops, dst.kind, r, w, None, None, quantum, hw)?;
    let bufs = v::Bufs::plain(ins, out);
    for (sp, row, tail) in v::stages(&pl, r, quantum) {
        match tail {
            Some(rows) => v::tail_band(t, &bufs, e, ops, dst, &sp, row, rows, w, hw)?,
            None => v::rows_stage(t, &bufs, e, ops, dst, &sp, hw.vc, &Idx::Int(row), None, hw)?,
        }
    }
    Ok(())
}

/// Ports and the specs given them.
type Ports = (Vec<Port>, Vec<(String, g::Spec)>);

/// The input ports (`in0..`, each `vector.port` over [r, w]) and `out` of
/// `out_shape`.
fn ports(ops: &[v::Operand], r: i64, w: i64, out_shape: &[i64], hw: &Hw) -> R<Ports> {
    let mut ports = Vec::new();
    let mut given = Vec::new();
    for (i, o) in ops.iter().enumerate() {
        let shape = v::port_shape(o, r, w, hw)?;
        let n = format!("in{i}");
        ports.push(Port::input(&n, &shape, g::Dtype::F16));
        given.push((n, spec(&shape, o.dtype, vec![], g::BufSpace::Dram)?));
    }
    ports.push(Port::output("out", out_shape, g::Dtype::F16));
    given.push((
        "out".into(),
        spec(out_shape, g::Dtype::F16, vec![], g::BufSpace::Dram)?,
    ));
    Ok((ports, given))
}

/// `launch_reductions.wide_fold_kernel`: `e`, holding one reduction across
/// a row of `w` columns wider than one column block, over [r, w] into
/// `out`. The block partials go to an [nblk, rq] scratch, fold pairwise
/// (an odd middle block carried) to one, and `e`'s expression around the
/// reduction applies to that row; returns the kernel and its column blocks.
pub fn wide_fold_kernel(
    e: &E,
    ops: &[v::Operand],
    r: i64,
    w: i64,
    out: &v::Operand,
    m: &Machine,
) -> R<(Kernel, i64)> {
    let hw = Hw::of(m);
    let red = expr::reduces(e)[0].clone();
    let Expr::Reduce(op, _) = red.as_ref() else {
        unreachable!()
    };
    let mut keep = Vec::new();
    let post = substitute(e, &red, &mut keep);
    let part = v::Operand::new(v::Kind::Row);
    let pl = v::plan(ops, v::Kind::Row, r, w, None, None, hw.stat_rows(), &hw)?;
    let nblk = pl.nblk;
    let rq = part.rows.max(v::stat_words(r, &hw) * hw.stat_rows());
    let out_shape = if out.kind == v::Kind::Row {
        vec![out.rows.max(v::stat_words(r, &hw) * hw.stat_rows())]
    } else {
        v::port_shape(out, r, hw.kblock, &hw)?
    };
    let (ports, given) = ports(ops, r, w, &out_shape, &hw)?;
    let key = name(
        "rows",
        vec![
            Py::str("wide_fold"),
            Py::Str(expr::py(e).repr()),
            Py::Tuple(ops.iter().map(v::Operand::py).collect()),
            Py::Int(r),
            Py::Int(w),
            out.py(),
            pl.py(),
            Py::Int(hw.vc),
            mode_py(),
        ],
    );
    let mode = mode();
    let k = build(&key, m, ports, given, |t, p| {
        let s0_2d = t.buffer(&[nblk, rq], g::Dtype::F16, vec![])?;
        let s0 = t.buffer(&[nblk * rq], g::Dtype::F16, vec![])?;
        let s1 = t.buffer(&[nblk * rq], g::Dtype::F16, vec![])?;
        let partials = |t: &mut Trace| -> R<()> {
            let ins = p[..ops.len()].to_vec();
            emit(t, ins, s0_2d.clone(), &red, ops, r, w, &part, &hw)?;
            t.copy(&s0.whole(), &s0_2d.whole(), None, None)
        };
        if mode != Some("wrong_order") {
            partials(t)?;
        }
        let (mut src, mut dst) = (s0.clone(), s1);
        let pair = both(fold(*op), expr::load(0), expr::load(1));
        let mut width = nblk;
        while width > 1 {
            let half = width / 2;
            let n = half * rq;
            let second = if mode == Some("alias_members") {
                0
            } else {
                width - half
            };
            let o = [
                v::Operand { rows: n, ..part },
                v::Operand {
                    rows: n,
                    offset: second * rq,
                    ..part
                },
            ];
            if mode == Some("drop_member") && width == nblk {
                std::mem::swap(&mut src, &mut dst);
                width -= half;
                continue;
            }
            let ins = vec![src.clone(), src.clone()];
            emit(t, ins, dst.clone(), &pair, &o, n, hw.kblock, &part, &hw)?;
            if width - half > half {
                let at = half * rq;
                t.copy(
                    &dst.at(&[sl(at, at + rq)])?,
                    &src.at(&[sl(at, at + rq)])?,
                    None,
                    None,
                )?;
            }
            std::mem::swap(&mut src, &mut dst);
            width -= half;
        }
        if mode == Some("wrong_order") {
            partials(t)?;
        }
        let mut ins = vec![src];
        let mut post_ops = vec![v::Operand { rows: r, ..part }];
        for &i in &keep {
            ins.push(p[i].clone());
            post_ops.push(ops[i]);
        }
        let out_buf = p[ops.len()].clone();
        emit(t, ins, out_buf, &post, &post_ops, r, hw.kblock, out, &hw)
    })?;
    Ok((k, nblk))
}

/// `launch_reductions.column_tree_kernel`: `e`, holding one reduction down
/// the rows, over [r, w] into the one row `out`. Each level folds the top
/// half of the rows onto the bottom half (an odd last row carried into the
/// next level's scratch); the first level computes the reduced expression
/// on both halves on the fly unless its reads, twice over, pass a core's
/// memory views or a statistic operand's rows do not split in whole words,
/// when it is computed into a scratch first. The expression around the
/// reduction applies to the last row. Operand i of `made` is made inside
/// the kernel by its plans (`prepared.input_buffer`).
pub fn column_tree_kernel(
    e: &E,
    ops: &[v::Operand],
    r: i64,
    w: i64,
    out: &v::Operand,
    made: &BTreeMap<usize, Vec<relayout::Plan>>,
    m: &Machine,
) -> R<Kernel> {
    let hw = Hw::of(m);
    let red = expr::reduces(e)[0].clone();
    let Expr::Reduce(op, arg) = red.as_ref() else {
        unreachable!()
    };
    let mut keep = Vec::new();
    let post = substitute(e, &red, &mut keep);
    let out_shape = if out.kind == v::Kind::Row {
        vec![out.rows.max(v::stat_words(1, &hw) * hw.stat_rows())]
    } else {
        v::port_shape(out, 1, w, &hw)?
    };
    let plain = ports(ops, r, w, &out_shape, &hw)?;
    let names: Vec<String> = plain.0.iter().map(|p| p.name.clone()).collect();
    let plans: BTreeMap<usize, Read> = made
        .iter()
        .map(|(i, p)| (*i, Read::Materialized(p.clone())))
        .collect();
    let made = prepared::made_inputs(&plans);
    let ((ports, given), shapes) = prepared::made_ports(plain, &made, m)?;
    let built: Vec<String> = ports.iter().map(|p| p.name.clone()).collect();
    let mut key = vec![
        Py::str("col_tree"),
        Py::Str(expr::py(e).repr()),
        Py::Tuple(ops.iter().map(v::Operand::py).collect()),
        Py::Int(r),
        Py::Int(w),
        out.py(),
        Py::Int(hw.vc),
        mode_py(),
    ];
    if !made.is_empty() {
        key.push(prepared::made_key(&plans));
    }
    let key = name("rows", key);
    let mode = mode();
    let views = m.facts().vc.descriptors as usize;
    build(&key, m, ports, given, |t, b| {
        let p = &prepared::made_bufs(t, &names, &built, b, &made, &shapes, m)?;
        let k = ops.len();
        let inputs = p[..k].to_vec();
        let full = v::Operand::full(w, 0);
        let mut n = r;
        let mut cur: Option<Buf> = None;
        let row_split = (n / 2) % hw.stat_rows() != 0 || n % 2 != 0;
        if n > 1 && (2 * k + 1 > views || (ops.iter().any(|o| o.kind == v::Kind::Row) && row_split))
        {
            let b = t.buffer(&[n, w], g::Dtype::F16, vec![])?;
            emit(t, inputs.clone(), b.clone(), arg, ops, n, w, &full, &hw)?;
            cur = Some(b);
        }
        let elide =
            r > 1 && matches!(post.as_ref(), Expr::Load(0)) && keep.is_empty() && mode.is_none();
        while n > 1 {
            let h = n / 2;
            let next = h + n % 2;
            let (target, target_op) = if next == 1 && elide {
                (p[k].clone(), *out)
            } else if let (Some("alias_members"), Some(c)) = (mode, &cur) {
                (c.clone(), v::Operand::full(w, next))
            } else {
                let b = t.buffer(&[next, w], g::Dtype::F16, vec![])?;
                (b, v::Operand::full(w, next))
            };
            match &cur {
                None => {
                    let by = if mode == Some("wrong_order") { 0 } else { h };
                    let lvl = both(fold(*op), arg.clone(), renumber(arg, k));
                    let mut lvl_ops = ops.to_vec();
                    lvl_ops.extend(ops.iter().map(|o| shift(o, by, w)));
                    let mut lvl_ins = inputs.clone();
                    lvl_ins.extend_from_slice(&inputs);
                    if n % 2 != 0 && mode != Some("drop_member") {
                        let last: Vec<v::Operand> =
                            ops.iter().map(|o| shift(o, n - 1, w)).collect();
                        let dst = v::Operand {
                            offset: h * w,
                            ..v::Operand::full(w, next)
                        };
                        emit(
                            t,
                            inputs.clone(),
                            target.clone(),
                            arg,
                            &last,
                            1,
                            w,
                            &dst,
                            &hw,
                        )?;
                    }
                    emit(
                        t,
                        lvl_ins,
                        target.clone(),
                        &lvl,
                        &lvl_ops,
                        h,
                        w,
                        &target_op,
                        &hw,
                    )?;
                }
                Some(c) => {
                    if mode == Some("drop_member") {
                        cur = Some(target);
                        break;
                    }
                    let lvl = both(fold(*op), expr::load(0), expr::load(1));
                    let lvl_ops = [
                        v::Operand::full(w, n),
                        v::Operand {
                            offset: h * w,
                            ..v::Operand::full(w, n)
                        },
                    ];
                    if n % 2 != 0 {
                        t.copy(
                            &target.at(&[sl(h, h + 1), ALL])?,
                            &c.at(&[sl(n - 1, n), ALL])?,
                            None,
                            None,
                        )?;
                    }
                    let ins = vec![c.clone(), c.clone()];
                    emit(
                        t,
                        ins,
                        target.clone(),
                        &lvl,
                        &lvl_ops,
                        h,
                        w,
                        &target_op,
                        &hw,
                    )?;
                }
            }
            cur = Some(target);
            n = next;
        }
        if elide {
            return Ok(());
        }
        let (head, mut head_ins, mut head_ops) = match cur {
            None => (arg.clone(), inputs, ops.to_vec()),
            Some(b) => (expr::load(0), vec![b], vec![v::Operand::full(w, 1)]),
        };
        let fin = place(&post, &head, head_ops.len());
        for &i in &keep {
            head_ops.push(ops[i]);
            head_ins.push(p[i].clone());
        }
        emit(t, head_ins, p[k].clone(), &fin, &head_ops, 1, w, out, &hw)
    })
}
