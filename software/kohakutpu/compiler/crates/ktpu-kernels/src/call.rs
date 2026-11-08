//! A generator call as the planner records it: `{"gen": "<module>.<fn>", "args": {python
//! keyword: value}}`. An `Operand` is its field dict, an expression its node
//! table ([`expr::from_table`]), an `Mm` / lanes plan `{"made_by": CALL}`
//! (the `matmul.plan` / `lanes.plan` call, made again here), tuples lists,
//! a dict keyed by operand index an object of decimal keys.
//! `cores` / `clusters` are the machine's (RUL.016); a recorded value is
//! not read. An argument left out takes the Python generator's default
//! (`shared` True, `quantum` 4, `batches` 1, `out_dtype` f16, `scalars` and
//! `bands` none).

use crate::expr;
use crate::gen::vector::{Kind, Operand};
use crate::gen::{
    chain, classes, convert, fused, lanes, matmul, prepared, reductions, relayout, transpose,
    vector, Hw,
};
use crate::kernel::Kernel;
use crate::trace::{fail, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

/// A generator call: `{gen: "vector.rows_kernel", args: {...}}`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Call {
    pub gen: String,
    pub args: Value,
}

fn arg<'a>(a: &'a Value, k: &str) -> R<&'a Value> {
    match a.get(k) {
        Some(v) => Ok(v),
        None => fail("API.OP.001", format!("a generator call without {k:?}")),
    }
}

fn int(a: &Value, k: &str) -> R<i64> {
    match arg(a, k)?.as_i64() {
        Some(v) => Ok(v),
        None => fail("API.OP.001", format!("{k} is not an int")),
    }
}

fn boolean(a: &Value, k: &str) -> R<bool> {
    match arg(a, k)?.as_bool() {
        Some(v) => Ok(v),
        None => fail("API.OP.001", format!("{k} is not a bool")),
    }
}

fn dtype(v: &Value) -> R<g::Dtype> {
    serde_json::from_value(v.clone())
        .or_else(|_| fail("DSL.SPEC.013", format!("no element type {v}")))
}

fn walk(v: &Value) -> R<Vec<(i64, i64)>> {
    let levels = v.as_array().map(Vec::as_slice).unwrap_or_default();
    levels
        .iter()
        .map(|l| {
            match (
                l.get(0).and_then(Value::as_i64),
                l.get(1).and_then(Value::as_i64),
            ) {
                (Some(n), Some(s)) => Ok((n, s)),
                _ => fail("API.OP.001", format!("a walk level {l}")),
            }
        })
        .collect()
}

/// An `Operand` from its field dict (or the dict under `"operand"`).
pub fn operand(v: &Value) -> R<Operand> {
    let o = v.get("operand").unwrap_or(v);
    let kind = arg(o, "kind")?.as_str().and_then(Kind::parse);
    let Some(kind) = kind else {
        return fail("API.READ.001", format!("an operand kind in {o}"));
    };
    Ok(Operand {
        kind,
        dtype: dtype(arg(o, "dtype")?)?,
        offset: int(o, "offset")?,
        pitch: int(o, "pitch")?,
        rows: int(o, "rows")?,
    })
}

fn operands(v: &Value) -> R<Vec<Operand>> {
    v.as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(operand)
        .collect()
}

/// A dict keyed by operand index (JSON object keys), in index order.
fn indexed(v: &Value) -> R<Vec<(usize, &Value)>> {
    let mut out = vec![];
    for (k, x) in v.as_object().into_iter().flatten() {
        match k.parse::<usize>() {
            Ok(i) => out.push((i, x)),
            Err(_) => return fail("API.OP.001", format!("an operand index {k:?}")),
        }
    }
    out.sort_by_key(|p| p.0);
    Ok(out)
}

fn made_by<'a>(v: &'a Value, gen: &str) -> R<&'a Value> {
    match v.get("made_by") {
        Some(c) if c.get("gen").and_then(Value::as_str) == Some(gen) => arg(c, "args"),
        _ => fail("API.OP.001", format!("an argument not made by {gen}: {v}")),
    }
}

/// `matmul.plan`'s tiling from its call's arguments.
pub fn mm_plan(a: &Value, m: &Machine) -> R<matmul::Mm> {
    matmul::plan(
        int(a, "bt")?,
        int(a, "m")?,
        int(a, "n")?,
        int(a, "k")?,
        a.get("shared").map_or(Ok(true), |_| boolean(a, "shared"))?,
        a.get("quantum").map_or(Ok(4), |_| int(a, "quantum"))?,
        match a.get("pairing") {
            None => ktpu_machine::PAIRING,
            Some(p) => match p.as_str().and_then(ktpu_machine::Pairing::parse) {
                Some(p) => p,
                None => return fail("API.OP.001", format!("a pairing (vector, unpaired): {p}")),
            },
        },
        m,
        &matmul::unrecorded().read(a, matmul::Group::Plan)?,
    )
}

/// A `matmul.mm_kernel` call's `q`: each consumer packing `{side, g, nk, rp, cp}`.
fn demands(a: &Value) -> R<Vec<matmul::QDemand>> {
    let list = a.get("q").and_then(Value::as_array);
    list.map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(demand)
        .collect()
}

fn demand(d: &Value) -> R<matmul::QDemand> {
    Ok(matmul::QDemand {
        side: quant_side(d)?,
        groups: int(d, "g")?,
        nk: int(d, "nk")?,
        rp: int(d, "rp")?,
        cp: int(d, "cp")?,
    })
}

/// Whether a packed fused rows step over `R` x `W` of `operands` into `outs`
/// outputs writes every packing `q` names ([`fused::packed_block`]).
pub fn fused_packs(a: &Value, m: &Machine) -> R<bool> {
    let block = fused::packed_block(
        &operands(arg(a, "operands")?)?,
        int(a, "outs")? as usize,
        int(a, "R")?,
        int(a, "W")?,
        &demands(a)?,
        &Hw::of(m),
    );
    Ok(block.is_some())
}

/// A `matmul.mm_kernel` call's kernel knobs as its kernel takes them, whether
/// that is the kernel of none, and whether it writes every packing `q` names.
pub fn mm_resolved(a: &Value, m: &Machine) -> R<Value> {
    let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
    let e = expr::from_table(arg(a, "e")?)?;
    let knobs = matmul::unrecorded().read(a, matmul::Group::Kernel)?;
    let out = operand(arg(a, "out")?)?;
    let extras = operands(a.get("extras").unwrap_or(&Value::Null))?;
    let q = matmul::packs_demands(&mm, &e, &extras, &out, &knobs, m, &demands(a)?)?;
    let got = matmul::effective(&mm, &e, &out, &knobs, m);
    let plain = got.args(matmul::Group::Kernel) == matmul::unrecorded().args(matmul::Group::Kernel);
    let knobs: serde_json::Map<String, Value> = got
        .args(matmul::Group::Kernel)
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect();
    Ok(serde_json::json!({"knobs": knobs, "plain": plain, "q": q}))
}

/// Whether a contraction of `a`'s `k` and `nk` takes A as f16 under the
/// kernel knobs `a` names (`matmul::takes_f16`).
pub fn mm_takes_f16(a: &Value, m: &Machine) -> R<bool> {
    let knobs = matmul::unrecorded().read(a, matmul::Group::Kernel)?;
    Ok(matmul::takes_f16(int(a, "k")?, int(a, "nk")?, &knobs, m))
}

/// `lanes.plan`'s relayout from its call's arguments.
pub fn lanes_plan(a: &Value, m: &Machine) -> R<lanes::Plan> {
    lanes::plan(
        int(a, "src_n")?,
        &walk(arg(a, "walk")?)?,
        int(a, "at")?,
        &walk(arg(a, "dwalk")?)?,
        int(a, "dat")?,
        int(a, "m")?,
        boolean(a, "keep")?,
        m,
    )
}

/// `relayout.plan`'s relayout from its call's arguments (`z0` 0 and `tree`
/// [`relayout::PULL_TREE`] when left out): `Err(Unplanned)` inside for a
/// walk pair no form takes.
pub fn relayout_plan(a: &Value, m: &Machine) -> R<Result<relayout::Plan, relayout::Unplanned>> {
    relayout::plan(
        int(a, "src_n")?,
        &walk(arg(a, "walk")?)?,
        int(a, "at")?,
        &walk(arg(a, "dwalk")?)?,
        int(a, "dat")?,
        int(a, "m")?,
        boolean(a, "keep")?,
        &relayout::Hw::of(m),
        a.get("z0").map_or(Ok(0), |_| int(a, "z0"))?,
        a.get("tree")
            .map_or(Ok(relayout::PULL_TREE), |_| boolean(a, "tree"))?,
    )
}

/// `vector.rows_kernel`'s shape from its call's arguments.
pub fn rows_shape(a: &Value, m: &Machine) -> R<vector::RowsShape> {
    vector::rows_shape(
        &expr::from_table(arg(a, "e")?)?,
        &operands(arg(a, "operands")?)?,
        int(a, "R")?,
        int(a, "W")?,
        &operand(arg(a, "out")?)?,
        a.get("bands").and_then(Value::as_i64),
        &Hw::of(m),
    )
}

/// The buffer extents of one `operand` over `R` rows of `W`.
pub fn port_shape(a: &Value, m: &Machine) -> R<Vec<i64>> {
    vector::port_shape(
        &operand(arg(a, "operand")?)?,
        int(a, "R")?,
        int(a, "W")?,
        &Hw::of(m),
    )
}

fn names(v: &Value) -> Vec<String> {
    v.as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .filter_map(|n| n.as_str().map(str::to_string))
        .collect()
}

fn exprs(v: &Value) -> R<Vec<expr::E>> {
    v.as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(expr::from_table)
        .collect()
}

fn text(a: &Value, k: &str) -> R<String> {
    match arg(a, k)?.as_str() {
        Some(v) => Ok(v.to_string()),
        None => fail("API.OP.001", format!("{k} is not a name")),
    }
}

fn named(v: &Value) -> R<fused::Named> {
    let dims = arg(v, "shape")?.as_array();
    let shape = dims.map(Vec::as_slice).unwrap_or_default();
    Ok(fused::Named {
        name: text(v, "name")?,
        shape: shape.iter().filter_map(Value::as_i64).collect(),
        dtype: dtype(arg(v, "dtype")?)?,
    })
}

fn dtypes(v: &Value) -> R<Vec<g::Dtype>> {
    v.as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(dtype)
        .collect()
}

/// A flat stage's `kept`: one flag a member.
fn kept(s: &Value, members: usize) -> R<Vec<bool>> {
    let flags: Vec<bool> = arg(s, "kept")?
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .filter_map(Value::as_bool)
        .collect();
    if flags.len() != members {
        return fail(
            "API.OP.001",
            format!("kept names {} of {members} members", flags.len()),
        );
    }
    Ok(flags)
}

fn fused_step(s: &Value) -> R<fused::Step> {
    let folded = s.get("fold").map_or(Ok(false), |_| boolean(s, "fold"))?;
    if folded && !indexed(s.get("made").unwrap_or(&Value::Null))?.is_empty() {
        return fail(
            "API.OP.001",
            "a complete fused fold reads ordinary operands",
        );
    }
    Ok(match arg(s, "kind")?.as_str() {
        Some("flat") => {
            let exprs = exprs(arg(s, "exprs")?)?;
            fused::Step::Flat {
                total: int(s, "total")?,
                dtypes: dtypes(arg(s, "dtypes")?)?,
                out_dtype: dtype(arg(s, "out_dtype")?)?,
                kept: kept(s, exprs.len())?,
                exprs,
                ins: names(arg(s, "ins")?),
                lanes: walk(arg(s, "lanes")?)?
                    .into_iter()
                    .map(|(i, l)| (i as usize, l))
                    .collect(),
                outs: names(arg(s, "outs")?),
            }
        }
        Some("rows") => fused::Step::Rows {
            fold: folded,
            r: int(s, "R")?,
            w: int(s, "W")?,
            exprs: exprs(arg(s, "exprs")?)?,
            operands: operands(arg(s, "operands")?)?,
            ins: names(arg(s, "ins")?),
            out_operands: operands(arg(s, "out_operands")?)?,
            outs: names(arg(s, "outs")?),
            made: indexed(s.get("made").unwrap_or(&Value::Null))?
                .into_iter()
                .map(|(i, steps)| Ok((i, prepared::Read::Composed(logical_list(steps)?))))
                .collect::<R<_>>()?,
        },
        Some("packed") => fused::Step::Packed {
            r: int(s, "R")?,
            w: int(s, "W")?,
            exprs: exprs(arg(s, "exprs")?)?,
            operands: operands(arg(s, "operands")?)?,
            ins: names(arg(s, "ins")?),
            outs: names(arg(s, "outs")?),
            packs: arg(s, "packs")?
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or_default()
                .iter()
                .map(|p| {
                    Ok(fused::Packing {
                        member: int(p, "member")? as usize,
                        port: text(p, "port")?,
                        demand: demand(p)?,
                    })
                })
                .collect::<R<_>>()?,
        },
        Some("quant") => fused::Step::Quant {
            src: text(s, "x")?,
            operand: operand(arg(s, "src")?)?,
            rows: int(s, "rows")?,
            cols: int(s, "cols")?,
            demand: demand(s)?,
            port: text(s, "port")?,
            staged: s.get("pad_strategy").and_then(Value::as_str) == Some("staged"),
            composed: match s.get("composed") {
                Some(steps) => Some(logical_list(steps)?),
                None => None,
            },
        },
        Some("copy") => fused::Step::Copy {
            src: text(s, "src")?,
            dst: text(s, "dst")?,
        },
        Some("attention") => fused::Step::Attention(
            serde_json::from_value(arg(s, "attention")?.clone()).map_err(|error| {
                ktpu_machine::Refusal::new("API.OP.001", format!("attention stage: {error}"))
            })?,
        ),
        Some("planes") => {
            fused::Step::Planes(serde_json::from_value(arg(s, "planes")?.clone()).map_err(
                |error| ktpu_machine::Refusal::new("API.OP.001", format!("plane stage: {error}")),
            )?)
        }
        Some("prefix") => fused::Step::Prefix {
            src: text(s, "src")?,
            dst: text(s, "dst")?,
            boundary: convert::prefix::Prefix::parse(arg(s, "boundary")?)?,
        },
        _ => return fail("API.OP.001", format!("a fused stage {s}")),
    })
}

fn fused_package(a: &Value) -> R<fused::Package> {
    let list =
        |k: &str| -> R<Vec<Value>> { Ok(arg(a, k)?.as_array().cloned().unwrap_or_default()) };
    Ok(fused::Package {
        scratch_space: a
            .get("scratch_space")
            .map(|value| {
                serde_json::from_value(value.clone())
                    .map_err(|e| ktpu_machine::Refusal::new("API.OP.001", e.to_string()))
            })
            .transpose()?,
        balance_stages: a
            .get("balance_stages")
            .map_or(Ok(false), |_| boolean(a, "balance_stages"))?,
        ports: list("ports")?
            .iter()
            .map(|p| Ok((named(p)?, boolean(p, "written")?)))
            .collect::<R<_>>()?,
        scratch: list("scratch")?.iter().map(named).collect::<R<_>>()?,
        staged: list("staged")?
            .iter()
            .map(|s| {
                Ok(fused::Staged {
                    scratch: text(s, "scratch")?,
                    port: text(s, "port")?,
                    words: int(s, "words")?,
                    read: boolean(s, "read")?,
                    written: boolean(s, "written")?,
                })
            })
            .collect::<R<_>>()?,
        steps: list("steps")?.iter().map(fused_step).collect::<R<_>>()?,
    })
}

/// A list of `relayout.plan` calls, each planned (an unplanned one
/// refused).
fn plan_list(v: &Value, m: &Machine) -> R<Vec<relayout::Plan>> {
    let mut out = vec![];
    for p in v.as_array().map(Vec::as_slice).unwrap_or_default() {
        match relayout_plan(made_by(p, "relayout.plan")?, m)? {
            Ok(pl) => out.push(pl),
            Err(relayout::Unplanned(why)) => {
                return fail(
                    "API.OP.001",
                    format!("a relayout.plan call no form takes: {why}"),
                )
            }
        }
    }
    Ok(out)
}

/// Refine both walks at the same iteration boundaries, innermost first.
fn paired_walks(
    source: &[(i64, i64)],
    destination: &[(i64, i64)],
) -> R<(Vec<(i64, i64)>, Vec<(i64, i64)>)> {
    if source
        .iter()
        .map(|x| x.0)
        .eq(destination.iter().map(|x| x.0))
    {
        return Ok((source.to_vec(), destination.to_vec()));
    }
    let mut src = source.iter().rev().copied().filter(|x| x.0 != 1);
    let mut dst = destination.iter().rev().copied().filter(|x| x.0 != 1);
    let (mut s, mut d) = (src.next(), dst.next());
    let (mut sw, mut dw) = (Vec::new(), Vec::new());
    while let (Some((sc, ss)), Some((dc, ds))) = (s, d) {
        let count = sc.min(dc);
        if count <= 0 || sc % count != 0 || dc % count != 0 {
            return fail(
                "API.OP.001",
                "composed walks need common iteration boundaries",
            );
        }
        sw.push((count, ss));
        dw.push((count, ds));
        s = if sc == count {
            src.next()
        } else {
            let Some(stride) = ss.checked_mul(count) else {
                return fail("API.OP.001", "composed source stride exceeds i64");
            };
            Some((sc / count, stride))
        };
        d = if dc == count {
            dst.next()
        } else {
            let Some(stride) = ds.checked_mul(count) else {
                return fail("API.OP.001", "composed destination stride exceeds i64");
            };
            Some((dc / count, stride))
        };
    }
    if s.is_some() || d.is_some() {
        return fail("API.OP.001", "composed walks need equal iteration extents");
    }
    sw.reverse();
    dw.reverse();
    Ok((sw, dw))
}

/// The walk pairs of a list of `relayout.plan` calls.
fn logical_list(v: &Value) -> R<Vec<relayout::Logical>> {
    let mut out = vec![];
    for p in v.as_array().map(Vec::as_slice).unwrap_or_default() {
        let a = made_by(p, "relayout.plan")?;
        let source = walk(arg(a, "walk")?)?;
        let destination = walk(arg(a, "dwalk")?)?;
        let (walk, dwalk) = paired_walks(&source, &destination)?;
        out.push(relayout::Logical {
            n: int(a, "src_n")?,
            at: int(a, "at")?,
            dat: int(a, "dat")?,
            m: int(a, "m")?,
            walk,
            dwalk,
        });
    }
    Ok(out)
}

fn composed_read(a: &Value, i: usize) -> bool {
    a.get("composed")
        .and_then(Value::as_array)
        .is_some_and(|l| l.iter().any(|x| x.as_u64() == Some(i as u64)))
}

/// A pending read: composed when `composed` lists `i`, else planned.
fn read_of(a: &Value, i: usize, steps: &Value, m: &Machine) -> R<prepared::Read> {
    if composed_read(a, i) {
        Ok(prepared::Read::Composed(logical_list(steps)?))
    } else {
        Ok(prepared::Read::Materialized(plan_list(steps, m)?))
    }
}

/// `made`: operand index -> its read, by [`read_of`].
fn made_reads(a: &Value, m: &Machine) -> R<BTreeMap<usize, prepared::Read>> {
    let mut made = BTreeMap::new();
    for (i, steps) in indexed(a.get("made").unwrap_or(&Value::Null))? {
        made.insert(i, read_of(a, i, steps, m)?);
    }
    Ok(made)
}

fn quant_side(a: &Value) -> R<matmul::Side> {
    match arg(a, "side")?.as_str() {
        Some("a") => Ok(matmul::Side::A),
        Some("b") => Ok(matmul::Side::B),
        _ => fail("API.FORM.006", format!("a packing side in {a}")),
    }
}

/// `schedule`'s keywords over [`matmul::QUANT_SCHEDULE`]'s defaults.
fn quant_schedule(a: &Value) -> R<Vec<(&'static str, i64)>> {
    matmul::QUANT_SCHEDULE
        .iter()
        .map(|&(key, default)| {
            let value = a.get("schedule").and_then(|s| s.get(key));
            match value {
                None => Ok((key, default)),
                Some(v) => match v.as_i64() {
                    Some(v) => Ok((key, v)),
                    None => fail("API.OP.001", format!("quant schedule {key} is an integer")),
                },
            }
        })
        .collect()
}

fn class_of(v: &Value) -> R<classes::ShapeClass> {
    let policy = arg(v, "policy")?.as_str().and_then(classes::Policy::parse);
    let cores = arg(v, "cores")?.as_str().and_then(classes::Cores::parse);
    let (Some(policy), Some(cores)) = (policy, cores) else {
        return fail("API.OP.001", format!("a shape class {v}"));
    };
    Ok(classes::ShapeClass {
        kblock: int(v, "kblock")?,
        max_block: int(v, "max_block")?,
        policy,
        cores,
    })
}

/// `class_schedule`'s knobs over [`classes::SCHEDULE`].
fn class_schedule(a: &Value) -> classes::Schedule {
    let k = a.get("class_schedule").unwrap_or(&Value::Null);
    let flag = |key: &str| k.get(key).and_then(Value::as_bool).unwrap_or(false);
    let d = classes::SCHEDULE;
    classes::Schedule {
        band: k.get("band").and_then(Value::as_i64).unwrap_or(d.band),
        wide_width: k.get("wide_width").and_then(Value::as_i64),
        rows_direct: flag("rows_direct"),
        wide_direct: flag("wide_direct"),
    }
}

/// `class_rows`'s call to [`classes::rows`] with the arguments it reads.
fn class_rows<T>(a: &Value, run: impl FnOnce(&classes::Rows) -> R<T>) -> R<T> {
    let e = expr::from_table(arg(a, "e")?)?;
    let ops = operands(arg(a, "operands")?)?;
    let out = operand(arg(a, "out")?)?;
    let class = class_of(arg(a, "shape_class")?)?;
    let schedule = class_schedule(a);
    run(&classes::Rows {
        e: &e,
        operands: &ops,
        r: int(a, "R")?,
        w: int(a, "W")?,
        out: &out,
        bands: a.get("bands").and_then(Value::as_i64),
        class: &class,
        schedule: &schedule,
    })
}

/// Whether a `vector.rows_kernel` call's class stages fit the L1.
pub fn class_fits(a: &Value, m: &Machine) -> R<bool> {
    class_rows(a, |rows| classes::rows::fits(rows, m))
}

fn lanes_class(v: &Value) -> R<lanes::Class> {
    Ok(lanes::Class {
        taps: walk(arg(v, "taps")?)?,
        words: int(v, "words")?,
        keep: boolean(v, "keep")?,
    })
}

/// The class kernel of one generator call taking a `shape_class`.
pub fn classed(gen: &str, a: &Value, m: &Machine) -> R<classes::Classed> {
    if gen == "vector.rows_kernel" {
        return class_rows(a, |rows| classes::rows::kernel(rows, false, m));
    }
    if gen == "lanes.class_kernel" {
        let policy = arg(a, "core_policy")?.as_str();
        let Some(cores) = policy.and_then(classes::Cores::parse) else {
            return fail("API.OP.001", format!("a core policy in {a}"));
        };
        return classes::lanes::kernel(
            &lanes_class(arg(a, "class")?)?,
            int(a, "src_words")?,
            int(a, "dst_words")?,
            cores,
            m,
        );
    }
    let class = class_of(arg(a, "shape_class")?)?;
    match gen {
        "vector.flat_kernel" => {
            let scalars = walk(a.get("scalars").unwrap_or(&Value::Null))?;
            let scalars: Vec<(usize, i64)> =
                scalars.into_iter().map(|(i, l)| (i as usize, l)).collect();
            classes::rows::flat_kernel(
                &expr::from_table(arg(a, "e")?)?,
                &dtypes(arg(a, "dtypes")?)?,
                int(a, "n")?,
                a.get("out_dtype").map_or(Ok(g::Dtype::F16), dtype)?,
                &scalars,
                &class,
                &class_schedule(a),
                m,
            )
        }
        "convert.copy_kernel" => classes::moves::copy_kernel(int(a, "nbytes")?, &class, m),
        "convert.walk_kernel" => classes::moves::walk_kernel(
            int(a, "src_bytes")?,
            &walk(arg(a, "levels")?)?,
            int(a, "at")?,
            int(a, "dst_bytes")?,
            &walk(arg(a, "dlevels")?)?,
            int(a, "dat")?,
            &class,
            m,
        ),
        "transpose.transpose_kernel" => {
            classes::moves::transpose_kernel(int(a, "nb")?, int(a, "A")?, int(a, "B")?, &class, m)
        }
        "matmul.quant_kernel" => classes::moves::quant_kernel(
            &operand(arg(a, "src")?)?,
            int(a, "rows")?,
            int(a, "cols")?,
            int(a, "rp")?,
            int(a, "cp")?,
            quant_side(a)?,
            int(a, "g")?,
            int(a, "nk")?,
            a.get("batches").map_or(Ok(1), |_| int(a, "batches"))?,
            &class,
            a.get("class_direct").and_then(Value::as_bool),
            m,
            a.get("finite")
                .map_or(Ok(false), |_| boolean(a, "finite"))?,
            &quant_schedule(a)?,
            &plan_list(a.get("prepared").unwrap_or(&Value::Null), m)?,
            a.get("pad_strategy").and_then(Value::as_str) == Some("staged"),
        ),
        "matmul.mm_kernel" => classes::mm::kernel(
            &mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?,
            &expr::from_table(arg(a, "e")?)?,
            &operands(arg(a, "extras")?)?,
            &operand(arg(a, "out")?)?,
            &class,
            m,
        ),
        other => fail("API.OP.001", format!("no class of generator {other:?}")),
    }
}

/// What a class call decided, with each port's `nbytes`.
pub fn class_info(gen: &str, a: &Value, m: &Machine) -> R<Value> {
    let got = classed(gen, a, m)?;
    let nbytes: serde_json::Map<String, Value> = got
        .kernel
        .given
        .iter()
        .map(|(port, s)| {
            let n = s.shape.iter().product::<i64>() * Hw::item(s.dtype);
            (port.clone(), n.into())
        })
        .collect();
    let mut info = got.info;
    info["nbytes"] = nbytes.into();
    Ok(info)
}

fn has_product(gen: &str, a: &Value) -> R<bool> {
    match gen {
        "matmul.mm_kernel" => Ok(true),
        "kernel.chain" => {
            let first = arg(a, "first")?;
            let second = arg(a, "second")?;
            Ok(has_product(&text(first, "gen")?, arg(first, "args")?)?
                || has_product(&text(second, "gen")?, arg(second, "args")?)?)
        }
        _ => Ok(false),
    }
}

/// Products whose independent row domain is represented by ordinary f16 A.
/// Other product forms retain the dependent-stage planner and its split options.
fn row_product(a: &Value, m: &Machine) -> R<Option<matmul::Mm>> {
    if a.get("shape_class").is_some()
        || !demands(a)?.is_empty()
        || !indexed(a.get("made").unwrap_or(&Value::Null))?.is_empty()
    {
        return Ok(None);
    }
    let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
    let knobs = matmul::unrecorded().read(a, matmul::Group::Kernel)?;
    let out = operand(arg(a, "out")?)?;
    let extras = operands(arg(a, "extras")?)?;
    let quantum = if vector::holds_stat(&extras, out.kind) {
        vector::split::common_quantum(mm.quantum, Hw::of(m).stat_rows())?
    } else {
        mm.quantum
    };
    Ok((mm.bt == 1
        && mm.m == mm.mp
        && mm.m % quantum == 0
        && mm.tm >= quantum
        && matmul::takes_f16(mm.k, mm.nk, &knobs, m)
        && out.kind == Kind::Full
        && out.pitch == mm.n
        && out.offset == 0
        && extras.iter().all(|o| o.kind != Kind::Tile))
    .then_some(mm))
}

/// An elementwise chain consumer with exactly the linked input and identical
/// row-major output coordinates. Materialization is retained by the emitter.
fn wave_consumer(a: &Value, m: &Machine) -> R<Option<chain::wave::Consumer>> {
    let enabled = a
        .get("materialized_pointwise")
        .map_or(Ok(false), |_| boolean(a, "materialized_pointwise"))?;
    if !enabled {
        return Ok(None);
    }
    if boolean(a, "keep")? || text(a, "from")? != "in0" {
        return Ok(None);
    }
    let first = arg(a, "first")?;
    if text(first, "gen")? != "matmul.mm_kernel" || row_product(arg(first, "args")?, m)?.is_none() {
        return Ok(None);
    }
    let second = arg(a, "second")?;
    if text(second, "gen")? != "vector.rows_kernel" {
        return Ok(None);
    }
    let args = arg(second, "args")?;
    if args.get("shape_class").is_some() {
        return Ok(None);
    }
    let r = int(args, "R")?;
    let w = int(args, "W")?;
    let inputs = operands(arg(args, "operands")?)?;
    let output = operand(arg(args, "out")?)?;
    let plain = |o: &Operand| {
        o.kind == Kind::Full
            && o.dtype == g::Dtype::F16
            && o.pitch == w
            && o.offset == 0
            && (o.rows == 0 || o.rows == r)
    };
    if inputs.len() != 1 || !plain(&inputs[0]) || !plain(&output) {
        return Ok(None);
    }
    let expression = expr::from_table(arg(args, "e")?)?;
    if !expr::reduces(&expression).is_empty() || expr::loads(&expression).iter().any(|i| *i != 0) {
        return Ok(None);
    }
    Ok(Some(chain::wave::Consumer {
        expression,
        output: "out".into(),
        rows: r,
        width: w,
        bands: args.get("bands").and_then(Value::as_i64),
    }))
}

/// Generate each stage on the same row owner; private edges do not cross nodes.
fn row_kernel_at(gen: &str, a: &Value, first: i64, count: i64, m: &Machine) -> R<Kernel> {
    match gen {
        "matmul.mm_kernel" => {
            let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
            let kernel = matmul::rows::kernel(
                &mm,
                &expr::from_table(arg(a, "e")?)?,
                &operands(arg(a, "extras")?)?,
                &operand(arg(a, "out")?)?,
                &matmul::unrecorded().read(a, matmul::Group::Kernel)?,
                first,
                count,
                m,
            )?;
            chain::quant::kernel(kernel, chain::quant::Backend::read(a)?, m)
        }
        "kernel.chain" => {
            let part = |key: &str| -> R<Kernel> {
                let part = arg(a, key)?;
                row_kernel_at(&text(part, "gen")?, arg(part, "args")?, first, count, m)
            };
            let left = arg(a, "first")?;
            let shape = composite_shape(&text(left, "gen")?, arg(left, "args")?, m)?;
            let to = text(a, "to")?;
            let compact = shape.writes.iter().any(|tensor| {
                tensor.name == to
                    && tensor.operand.offset == 0
                    && tensor.planes == [0]
                    && (tensor.operand.kind == Kind::Row
                        || tensor.operand.kind == Kind::Full
                            && tensor.operand.pitch == tensor.width)
            });
            let kernel = chain::row_kernel(
                part("first")?,
                part("second")?,
                &text(a, "from")?,
                &to,
                boolean(a, "keep")?,
                first,
                count,
                compact.then_some(shape.quantum),
                wave_consumer(a, m)?.as_ref(),
                chain::link::View::read(a)?,
                m,
            )?;
            let (kernel, _) = chain::finish(kernel, a, m, true)?;
            chain::pooled_ordered(kernel, a, m)
        }
        _ => call(gen, &shifted_rows_call(gen, a, first, count, m)?, m),
    }
}

/// Common independent domain of typed row compositions.
fn row_domain(gen: &str, a: &Value, m: &Machine) -> R<Option<i64>> {
    match gen {
        "vector.rows_kernel" | "reductions.wide_fold_kernel" => Ok(Some(int(a, "R")?)),
        "prepared.rows_kernel" => Ok(prepared_row_made(a)?.map(|_| int(a, "R")).transpose()?),
        "matmul.mm_kernel" => Ok(row_product(a, m)?.map(|mm| mm.m)),
        "kernel.chain" => {
            let first = arg(a, "first")?;
            let second = arg(a, "second")?;
            let left = row_domain(&text(first, "gen")?, arg(first, "args")?, m)?;
            let right = row_domain(&text(second, "gen")?, arg(second, "args")?, m)?;
            Ok(left.filter(|_| left == right))
        }
        "fused.kernel" => {
            let package = fused_package(a)?;
            let mut domain = None;
            for step in package.steps {
                let fused::Step::Rows { r, .. } = step else {
                    return Ok(None);
                };
                if domain.is_some_and(|n| n != r) {
                    return Ok(None);
                }
                domain = Some(r);
            }
            if !package.staged.is_empty() {
                return Ok(None);
            }
            Ok(domain)
        }
        _ => Ok(None),
    }
}

fn operand_field(o: &mut Value, field: &str, value: i64) {
    let o = if o.get("operand").is_some() {
        &mut o["operand"]
    } else {
        o
    };
    o[field] = value.into();
}

fn row_operands_at(a: &mut Value, key: &str, first: i64) -> R<()> {
    let ops = operands(arg(a, key)?)?;
    for (i, o) in ops.iter().enumerate() {
        operand_field(
            &mut a[key][i],
            "offset",
            vector::split::shifted(o, first).offset,
        );
    }
    Ok(())
}

/// Made operands with composed maps or one materialized relayout whose walks
/// both lead with `R` rows. Composed maps retain destination coordinates while
/// the consumer operands select each share's rows.
fn prepared_row_made(a: &Value) -> R<Option<Vec<usize>>> {
    let rows = int(a, "R")?;
    let mut made = Vec::new();
    let Some(object) = a.get("made").and_then(Value::as_object) else {
        return Ok(Some(made));
    };
    for (index, reads) in object {
        let Ok(index) = index.parse::<usize>() else {
            return Ok(None);
        };
        if composed_read(a, index) {
            made.push(index);
            continue;
        }
        let [read] = reads.as_array().map(Vec::as_slice).unwrap_or(&[]) else {
            return Ok(None);
        };
        let by = &read["made_by"];
        let leading = |key: &str| by["args"][key][0][0].as_i64() == Some(rows);
        if by["gen"] != "relayout.plan"
            || !leading("walk")
            || !leading("dwalk")
            || by["args"]["dat"].as_i64() != Some(0)
            || by["args"]["m"].as_i64().is_none()
            || !rows_outermost(&by["args"]["dwalk"], &a["operands"][index])
        {
            return Ok(None);
        }
        made.push(index);
    }
    Ok(Some(made))
}

/// Whether a materialized relayout's destination keeps each consumer row whole
/// in its own row block: the row level outermost, every inner level within one
/// row stride, and the consumer reading that stride as its pitch from offset 0.
/// A row share is then the relayout's first rows.
fn rows_outermost(dwalk: &Value, consumer: &Value) -> bool {
    let levels: Option<Vec<(i64, i64)>> = dwalk.as_array().and_then(|levels| {
        levels
            .iter()
            .map(|l| Some((l[0].as_i64()?, l[1].as_i64()?)))
            .collect()
    });
    let Some(levels) = levels else { return false };
    let Some(&(_, row)) = levels.first() else {
        return false;
    };
    let inner = levels[1..].iter().try_fold(1i64, |reach, &(n, s)| {
        (n >= 1 && s > 0).then(|| reach + (n - 1) * s)
    });
    let c = consumer.get("operand").unwrap_or(consumer);
    inner.is_some_and(|reach| reach <= row)
        && c["kind"] == "full"
        && c["pitch"].as_i64() == Some(row)
        && c["offset"].as_i64() == Some(0)
}

fn shifted_rows_call(gen: &str, a: &Value, first: i64, count: i64, m: &Machine) -> R<Value> {
    let mut local = a.clone();
    match gen {
        "prepared.rows_kernel" => {
            let Some(made) = prepared_row_made(a)? else {
                return fail("API.OP.001", "a made operand that does not lead with rows");
            };
            let rows = int(a, "R")?;
            let out = operand(arg(a, "out")?)?;
            let ops = operands(arg(a, "operands")?)?;
            local["R"] = count.into();
            for (i, o) in ops.iter().enumerate() {
                if !made.contains(&i) || composed_read(a, i) {
                    operand_field(
                        &mut local["operands"][i],
                        "offset",
                        vector::split::shifted(o, first).offset,
                    );
                }
            }
            operand_field(
                &mut local["out"],
                "offset",
                vector::split::shifted(&out, first).offset,
            );
            for i in made {
                if composed_read(a, i) {
                    continue;
                }
                let args = &mut local["made"][i.to_string()][0]["made_by"]["args"];
                let row_stride = args["walk"][0][1].as_i64().unwrap_or(0);
                let at = args["at"].as_i64().unwrap_or(0);
                let total = args["m"].as_i64().unwrap_or(0);
                args["walk"][0][0] = count.into();
                args["dwalk"][0][0] = count.into();
                args["at"] = (at + first * row_stride).into();
                args["m"] = (total / rows * count).into();
            }
        }
        "vector.rows_kernel" | "reductions.wide_fold_kernel" => {
            let signature = call(gen, a, m)?;
            let out = operand(arg(a, "out")?)?;
            if out.kind == Kind::Row {
                let port = signature
                    .graph
                    .ports
                    .iter()
                    .find(|p| p.name == "out")
                    .ok_or_else(|| ktpu_machine::Refusal::new("DSL.KERN.020", "row output"))?;
                operand_field(
                    &mut local["out"],
                    "rows",
                    port.spec.shape.last().copied().unwrap_or(0),
                );
            }
            local["R"] = count.into();
            row_operands_at(&mut local, "operands", first)?;
            operand_field(
                &mut local["out"],
                "offset",
                vector::split::shifted(&out, first).offset,
            );
        }
        "kernel.chain" => {
            for key in ["first", "second"] {
                let part = arg(a, key)?;
                local[key]["args"] =
                    shifted_rows_call(&text(part, "gen")?, arg(part, "args")?, first, count, m)?;
            }
        }
        "fused.kernel" => {
            if let Some(steps) = local["steps"].as_array_mut() {
                for step in steps {
                    step["R"] = count.into();
                    row_operands_at(step, "operands", first)?;
                    row_operands_at(step, "out_operands", first)?;
                }
            }
        }
        _ => return fail("API.OP.001", "not an independent row composite"),
    }
    Ok(local)
}

fn composite_shape(gen: &str, a: &Value, m: &Machine) -> R<vector::split::Shape> {
    let hw = Hw::of(m);
    let Some(rows) = row_domain(gen, a, m)? else {
        return fail("API.OP.001", "row composite needs one independent domain");
    };
    let mut shape = vector::split::Shape {
        rows,
        quantum: hw.band,
        reads: vec![],
        writes: vec![],
        tokens: false,
    };
    match gen {
        "reductions.wide_fold_kernel" => {
            let ops = operands(arg(a, "operands")?)?;
            let out = operand(arg(a, "out")?)?;
            let w = int(a, "W")?;
            shape.quantum = hw.stat_rows();
            shape.reads = ops
                .iter()
                .enumerate()
                .map(|(i, o)| row_tensor(format!("in{i}"), *o, w))
                .collect();
            shape.writes.push(row_tensor("out".into(), out, hw.kblock));
        }
        "vector.rows_kernel" => {
            let ops = operands(arg(a, "operands")?)?;
            let out = operand(arg(a, "out")?)?;
            let w = int(a, "W")?;
            if vector::holds_stat(&ops, out.kind) {
                shape.quantum = hw.stat_rows();
            }
            shape.reads = ops
                .iter()
                .enumerate()
                .map(|(i, o)| row_tensor(format!("in{i}"), *o, w))
                .collect();
            let mut tensor = row_tensor("out".into(), out, w);
            let k = call(gen, a, m)?;
            if let Some(p) = k.graph.ports.iter().find(|p| p.name == "out") {
                if out.kind == Kind::Row && p.spec.shape.len() == 2 {
                    tensor.planes = (0..p.spec.shape[0]).map(|i| i * p.spec.shape[1]).collect();
                }
            }
            shape.writes.push(tensor);
        }
        "prepared.rows_kernel" => {
            let Some(made) = prepared_row_made(a)? else {
                return fail("API.OP.001", "a made operand that does not lead with rows");
            };
            let ops = operands(arg(a, "operands")?)?;
            let out = operand(arg(a, "out")?)?;
            let w = int(a, "W")?;
            if vector::holds_stat(&ops, out.kind) {
                shape.quantum = hw.stat_rows();
            }
            let k = call(gen, a, m)?;
            for (i, o) in ops.iter().enumerate() {
                if !made.contains(&i) {
                    shape.reads.push(row_tensor(format!("in{i}"), *o, w));
                    continue;
                }
                // A made operand's source is read whole by the relayout.
                let port = k
                    .graph
                    .ports
                    .iter()
                    .find(|p| p.name == format!("x{i}"))
                    .ok_or_else(|| {
                        ktpu_machine::Refusal::new("DSL.KERN.020", "a made operand's source port")
                    })?;
                let elements = port.spec.shape.iter().product::<i64>();
                shape.reads.push(row_tensor(
                    port.name.clone(),
                    Operand {
                        dtype: g::Dtype::F16,
                        ..Operand::new(Kind::Col)
                    },
                    elements,
                ));
            }
            shape.writes.push(row_tensor("out".into(), out, w));
        }
        "matmul.mm_kernel" => {
            let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
            let extras = operands(arg(a, "extras")?)?;
            let out = operand(arg(a, "out")?)?;
            shape.quantum = if vector::holds_stat(&extras, out.kind) {
                vector::split::common_quantum(mm.quantum, hw.stat_rows())?
            } else {
                mm.quantum
            };
            shape
                .reads
                .push(row_tensor("a".into(), Operand::full(mm.k, mm.m), mm.k));
            shape.reads.push(row_tensor(
                "b".into(),
                Operand {
                    dtype: g::Dtype::U8,
                    ..Operand::new(Kind::Col)
                },
                mm.n * mm.kp,
            ));
            shape.reads.extend(
                extras
                    .iter()
                    .enumerate()
                    .map(|(i, o)| row_tensor(format!("in{}", i + 1), *o, mm.n)),
            );
            shape.writes.push(row_tensor("out".into(), out, mm.n));
        }
        "kernel.chain" => {
            let first = arg(a, "first")?;
            let second = arg(a, "second")?;
            let mut left = composite_shape(&text(first, "gen")?, arg(first, "args")?, m)?;
            let mut right = composite_shape(&text(second, "gen")?, arg(second, "args")?, m)?;
            let from = text(a, "from")?;
            let to = text(a, "to")?;
            if !boolean(a, "keep")? {
                left.writes.retain(|t| t.name != to);
            }
            right.reads.retain(|t| t.name != from);
            let k = call(gen, a, m)?;
            for tensor in left.reads.iter_mut().chain(&mut left.writes) {
                tensor.name = k.graph.consts["chain"]["first_ports"][&tensor.name]
                    .as_str()
                    .ok_or_else(|| {
                        ktpu_machine::Refusal::new("DSL.KERN.020", "chain first port mapping")
                    })?
                    .to_string();
            }
            for tensor in right.reads.iter_mut().chain(&mut right.writes) {
                tensor.name = k.graph.consts["chain"]["ports"][&tensor.name]
                    .as_str()
                    .ok_or_else(|| {
                        ktpu_machine::Refusal::new("DSL.KERN.020", "chain port mapping")
                    })?
                    .to_string();
            }
            shape.quantum = vector::split::common_quantum(left.quantum, right.quantum)?;
            shape.reads = left.reads;
            shape.reads.extend(right.reads);
            shape.writes = left.writes;
            shape.writes.extend(right.writes);
        }
        "fused.kernel" => {
            let package = fused_package(a)?;
            for step in package.steps {
                if let fused::Step::Rows {
                    w,
                    operands,
                    ins,
                    out_operands,
                    outs,
                    ..
                } = step
                {
                    if operands
                        .iter()
                        .chain(&out_operands)
                        .any(|o| o.kind == Kind::Row)
                    {
                        shape.quantum = hw.stat_rows();
                    }
                    for (name, o) in ins.into_iter().zip(operands) {
                        if package.ports.iter().any(|(p, _)| p.name == name) {
                            shape.reads.push(row_tensor(name, o, w));
                        }
                    }
                    for (name, o) in outs.into_iter().zip(out_operands) {
                        if package
                            .ports
                            .iter()
                            .any(|(p, written)| p.name == name && *written)
                        {
                            shape.writes.push(row_tensor(name, o, w));
                        }
                    }
                }
            }
        }
        _ => return fail("API.OP.001", "not an independent row composite"),
    }
    Ok(shape)
}

fn row_tensor(name: String, operand: Operand, width: i64) -> vector::split::Tensor {
    vector::split::Tensor {
        name,
        operand,
        width,
        planes: vec![0],
    }
}

fn same_row_read(producer: &Operand, consumer: &Operand) -> bool {
    producer.dtype == consumer.dtype
        && producer.offset == consumer.offset
        && producer.logical_strides() == consumer.logical_strides()
}

/// Scratch dependencies must remain within one row owner's domain.
fn row_local(gen: &str, a: &Value, m: &Machine) -> R<bool> {
    match gen {
        "vector.rows_kernel" | "reductions.wide_fold_kernel" => Ok(true),
        "prepared.rows_kernel" => Ok(prepared_row_made(a)?.is_some()),
        "matmul.mm_kernel" => Ok(row_product(a, m)?.is_some()),
        "kernel.chain" => {
            let first = arg(a, "first")?;
            let second = arg(a, "second")?;
            let fg = text(first, "gen")?;
            let sg = text(second, "gen")?;
            let fa = arg(first, "args")?;
            let sa = arg(second, "args")?;
            if !row_local(&fg, fa, m)? || !row_local(&sg, sa, m)? {
                return Ok(false);
            }
            let left = composite_shape(&fg, fa, m)?;
            let right = composite_shape(&sg, sa, m)?;
            let from = text(a, "from")?;
            let to = text(a, "to")?;
            Ok(right.reads.iter().filter(|r| r.name == from).all(|r| {
                left.writes.iter().any(|w| {
                    w.name == to
                        && same_row_read(&w.operand, &r.operand)
                        && (r.operand.kind == Kind::Row || r.width <= w.width)
                })
            }))
        }
        "fused.kernel" => {
            let package = fused_package(a)?;
            let mut written = BTreeMap::new();
            for step in package.steps {
                let fused::Step::Rows {
                    operands,
                    ins,
                    out_operands,
                    outs,
                    made,
                    ..
                } = step
                else {
                    return Ok(false);
                };
                for (i, (name, read)) in ins.iter().zip(&operands).enumerate() {
                    if let Some(producer) = written.get(name) {
                        if made.contains_key(&i) || !same_row_read(producer, read) {
                            return Ok(false);
                        }
                    }
                }
                written.extend(outs.into_iter().zip(out_operands));
            }
            Ok(true)
        }
        _ => Ok(false),
    }
}

/// Compose already distributed stages over shared global allocations. Cross-node
/// hazards remain graph accesses for L2's release/acquire lowering.
fn row_sequence(
    mut kernel: Kernel,
    plans: Vec<crate::split::PlannedKernel>,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    use crate::split::{
        choose_split, validate_ownership, CostEstimate, CostFeatures, LegalityWitness, LocalTiming,
        OutputLayout, PlannedKernel, ShardRole, ShardTiles, SplitAxis, SplitOption,
    };
    let p = request.nodes.ids().len();
    let mut active = vec![false; p];
    let mut shards: Vec<ShardTiles> = (0..p)
        .map(|logical_node| ShardTiles {
            logical_node,
            tile_ranges: vec![],
            logical_origin: vec![],
            valid_extent: vec![],
            padded_extent: vec![],
            read_regions: vec![],
            write_regions: vec![],
            scratch_regions: vec![],
            local_schedule: "row-stage-sequence".into(),
            role: ShardRole::Empty,
        })
        .collect();
    let mut output = OutputLayout {
        form: "producer".into(),
        tensors: kernel
            .graph
            .ports
            .iter()
            .filter(|p| p.role == g::Role::Out)
            .map(|p| (p.name.clone(), p.spec.clone()))
            .collect(),
        ..OutputLayout::default()
    };
    let external = |name: &str| output.tensors.iter().any(|(n, _)| n == name);
    let mut ownership = Vec::new();
    let mut events = Vec::new();
    let mut conversions = 0;
    for plan in &plans {
        for rank in 0..p {
            if plan
                .split
                .shards
                .iter()
                .any(|s| s.logical_node == rank && s.role != ShardRole::Empty)
            {
                active[rank] = true;
            }
        }
        for share in &plan.split.shards {
            let dst = &mut shards[share.logical_node];
            dst.read_regions.extend(share.read_regions.clone());
            dst.write_regions.extend(share.write_regions.clone());
            dst.scratch_regions.extend(share.scratch_regions.clone());
        }
        for (_, region) in &plan.split.output.ownership {
            if external(&region.allocation) {
                // Retain earlier destination bytes outside the overwrite.
                let mut retained = Vec::new();
                for (owner, old) in ownership.drain(..) {
                    let old: crate::split::Region = old;
                    if old.allocation != region.allocation
                        || old.bytes.end <= region.bytes.start
                        || region.bytes.end <= old.bytes.start
                    {
                        retained.push((owner, old));
                        continue;
                    }
                    if old.bytes.start < region.bytes.start {
                        retained.push((
                            owner,
                            crate::split::Region {
                                bytes: old.bytes.start..region.bytes.start,
                                ..old.clone()
                            },
                        ));
                    }
                    if region.bytes.end < old.bytes.end {
                        retained.push((
                            owner,
                            crate::split::Region {
                                bytes: region.bytes.end..old.bytes.end,
                                ..old
                            },
                        ));
                    }
                }
                ownership = retained;
            }
        }
        ownership.extend(
            plan.split
                .output
                .ownership
                .iter()
                .filter(|(_, r)| external(&r.allocation))
                .cloned(),
        );
        events.extend(plan.split.events.clone());
        conversions += plan.split.conversions;
    }
    output.valid_region = ownership.iter().map(|(_, r)| r.clone()).collect();
    output.ownership = ownership;
    validate_ownership(&output)?;
    for (rank, share) in shards.iter_mut().enumerate() {
        if active[rank] {
            share.role = ShardRole::Compute;
        }
    }
    if request.nodes.ids() != [0] {
        kernel.graph.name = crate::pyrepr::name(
            "row_sequence",
            vec![
                crate::pyrepr::Py::str(&kernel.graph.name),
                crate::pyrepr::Py::ints(request.nodes.ids()),
            ],
        );
    }
    let node_cycles = crate::split::cost::graph_nodes(&kernel.graph, m)?;
    if node_cycles
        .keys()
        .any(|node| !request.nodes.ids().contains(node))
    {
        return fail(
            "DSL.KERN.020",
            "composed row schedule escaped the node grant",
        );
    }
    let cycles: Vec<_> = request
        .nodes
        .ids()
        .iter()
        .map(|node| node_cycles.get(node).copied().unwrap_or(0))
        .collect();
    let total_cycles = cycles.iter().copied().max().unwrap_or(0) + m.vc.launch_front as i64;
    if p == 1 {
        let public = |name: &str| kernel.graph.ports.iter().any(|p| p.name == name);
        shards[0].read_regions.retain(|r| public(&r.allocation));
        shards[0].write_regions.retain(|r| public(&r.allocation));
        shards[0].scratch_regions = kernel
            .graph
            .scratch
            .iter()
            .map(|s| {
                let elements = s.spec.shape.iter().product::<i64>();
                let bytes = match s.spec.dtype {
                    g::Dtype::Mx7a | g::Dtype::Mx7b => m.mx7_bytes(elements),
                    dtype => elements * Hw::item(dtype),
                };
                crate::split::Region {
                    allocation: s.name.clone(),
                    bytes: 0..bytes as u64,
                }
            })
            .collect();
    }
    let option = SplitOption {
        id: "rows".into(),
        axis: SplitAxis::Rows,
        p,
        active_nodes: active.iter().filter(|&&a| a).count(),
        shards,
        output,
        events,
        conversions,
        cost: CostEstimate::default(),
        legality: LegalityWitness {
            numerics: request.numerics.clone(),
            clauses: vec![
                "stage-local row ownership; shared intermediates ordered by L2 accesses".into(),
            ],
        },
    };
    let features = CostFeatures {
        share_cycles: cycles
            .into_iter()
            .zip(active)
            .filter_map(|(c, a)| a.then_some(c))
            .collect(),
        dispatch_intercept: m.vc.launch_front as i64,
    };
    struct SequenceTiming(i64);
    impl crate::split::CostModel for SequenceTiming {
        fn estimate(&self, option: &SplitOption, features: &CostFeatures) -> CostEstimate {
            let mut cost = crate::split::CostModel::estimate(&LocalTiming, option, features);
            cost.makespan = self.0;
            cost.provenance = "ktpu-l2 complete row-stage sequence".into();
            cost
        }
    }
    let timing = SequenceTiming(total_cycles);
    let split = choose_split(vec![(option, features)], &timing, request.force.as_deref())?;
    Ok(PlannedKernel {
        kernel,
        l1: None,
        split,
        request: request.clone(),
    })
}

fn rename_regions(plan: &mut crate::split::PlannedKernel, names: &BTreeMap<String, String>) {
    let rename = |region: &mut crate::split::Region| {
        if let Some(name) = names.get(&region.allocation) {
            region.allocation = name.clone();
        }
    };
    for (_, region) in &mut plan.split.output.ownership {
        rename(region);
    }
    for region in &mut plan.split.output.valid_region {
        rename(region);
    }
    for share in &mut plan.split.shards {
        for region in share
            .read_regions
            .iter_mut()
            .chain(&mut share.write_regions)
            .chain(&mut share.scratch_regions)
        {
            rename(region);
        }
    }
}

/// Sequence members may have dependent row stages and overlapping writes.
/// Preserve their order and merge their actual final output ownership.
fn split_sequence(
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    let descriptors = arg(a, "members")?.as_array().ok_or_else(|| {
        ktpu_machine::Refusal::new("API.OP.001", "sequence members must be an array")
    })?;
    let mut local = request.clone();
    local.force = None;
    let mut plans = descriptors
        .iter()
        .map(|descriptor| {
            chain::sequence::deferred(descriptor)?;
            call_split_with_timing(
                &text(descriptor, "gen")?,
                arg(descriptor, "args")?,
                &local,
                m,
                true,
            )
        })
        .collect::<R<Vec<_>>>()?;
    let mut kernel = chain::sequence::kernel(plans.iter().map(|p| p.kernel.clone()).collect())?;
    for (index, plan) in plans.iter_mut().enumerate() {
        rename_regions(plan, &chain::sequence::scope(&kernel, index)?);
    }
    if request.nodes.ids().len() == 1 {
        kernel = chain::placed(call("kernel.sequence", a, m)?, request.nodes.ids()[0])?;
    } else {
        let (pooled, aliases) = chain::sequence::finish_mapped(kernel, a, m)?;
        kernel = pooled;
        for plan in &mut plans {
            rename_regions(plan, &aliases);
        }
    }
    let mut result = row_sequence(kernel, plans, request, m)?;
    chain::sequence::scratch_regions(&result.kernel, &request.nodes, &mut result.split.shards, m)?;
    Ok(result)
}

fn split_row_chain(
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    let mut stage_request = request.clone();
    stage_request.force = None;
    let part = |key: &str| {
        let part = arg(a, key)?;
        call_split_with_timing(
            &text(part, "gen")?,
            arg(part, "args")?,
            &stage_request,
            m,
            true,
        )
    };
    let mut first = part("first")?;
    let mut second = part("second")?;
    let from = text(a, "from")?;
    let to = text(a, "to")?;
    let keep = boolean(a, "keep")?;
    if let Some(binding) = chain::link::Binding::of(
        &first.kernel,
        &second.kernel,
        &from,
        &to,
        chain::link::View::read(a)?,
        m,
    )? {
        second.kernel = binding.apply(second.kernel)?;
        second.l1 = None;
        for share in &mut second.split.shards {
            binding.regions(&mut share.read_regions)?;
        }
    }
    let mut kernel = chain::kernel(
        first.kernel.clone(),
        second.kernel.clone(),
        &from,
        &to,
        keep,
    )?;
    let mut names = BTreeMap::from([(from, to.clone())]);
    if let Some(ports) = kernel.graph.consts["chain"]["ports"].as_object() {
        for (old, new) in ports {
            if let Some(new) = new.as_str() {
                names.insert(old.clone(), new.to_string());
            }
        }
    }
    let scratch_prefix = kernel.graph.scratch.len() - second.kernel.graph.scratch.len();
    for (original, composed) in second
        .kernel
        .graph
        .scratch
        .iter()
        .zip(&kernel.graph.scratch[scratch_prefix..])
    {
        names.insert(original.name.clone(), composed.name.clone());
    }
    rename_regions(&mut second, &names);
    if !keep && request.nodes.ids().len() > 1 {
        if let Some(link) = kernel.graph.scratch.iter_mut().find(|s| s.name == to) {
            link.spec.space = g::BufSpace::Dram;
        }
    }
    let (mut kernel, mapping) = chain::finish(kernel, a, m, request.nodes.ids().len() == 1)?;
    rename_regions(&mut first, &mapping);
    rename_regions(&mut second, &mapping);
    if request.nodes.ids().len() == 1 {
        kernel = chain::placed(call("kernel.chain", a, m)?, request.nodes.ids()[0])?;
    } else {
        kernel = chain::ordered(kernel, a, m)?;
    }
    row_sequence(kernel, vec![first, second], request, m)
}

/// Row/flat stages retain the package's named shared intermediate allocations.
/// Staging copies are emitted once, outside all compute shares.
fn split_row_package(
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    let signature = call("fused.kernel", a, m)?;
    let mut ports = arg(a, "ports")?.as_array().cloned().unwrap_or_default();
    let scratch = arg(a, "scratch")?.as_array().cloned().unwrap_or_default();
    for buffer in &scratch {
        let mut buffer = buffer.clone();
        buffer["written"] = false.into();
        ports.push(buffer);
    }
    let mut stage_request = request.clone();
    stage_request.force = None;
    let mut plans = Vec::new();
    let stages = arg(a, "staged")?.as_array().cloned().unwrap_or_default();
    let steps = arg(a, "steps")?.as_array().cloned().unwrap_or_default();
    let copy = |stage: &Value, read: bool| -> R<crate::split::PlannedKernel> {
        let mut stage = stage.clone();
        stage["read"] = read.into();
        stage["written"] = (!read).into();
        let mut package = a.clone();
        package["ports"] = serde_json::json!(ports);
        package["scratch"] = serde_json::json!([]);
        package["staged"] = serde_json::json!([stage]);
        package["steps"] = serde_json::json!([]);
        let destination = text(&stage, if read { "scratch" } else { "port" })?;
        if let Some(list) = package["ports"].as_array_mut() {
            for port in list {
                port["written"] = (text(port, "name")? == destination).into();
            }
        }
        let mut plan = call_split_with_timing("fused.kernel", &package, &stage_request, m, true)?;
        let source = text(&stage, if read { "port" } else { "scratch" })?;
        let bytes = int(&stage, "words")? * Hw::of(m).word_bytes;
        let mut written = bytes;
        if read {
            if let Some((_, spec)) = plan.kernel.given.iter().find(|(n, _)| n == &destination) {
                written = spec.shape.iter().product::<i64>() * Hw::item(spec.dtype);
            }
        }
        let dst = crate::split::Region {
            allocation: destination,
            bytes: 0..written as u64,
        };
        let src = crate::split::Region {
            allocation: source,
            bytes: 0..bytes as u64,
        };
        plan.split.shards[0].read_regions.push(src);
        plan.split.shards[0].write_regions.push(dst.clone());
        plan.split.output.valid_region = vec![dst.clone()];
        plan.split.output.ownership = vec![(0, dst)];
        Ok(plan)
    };
    for stage in &stages {
        if boolean(stage, "read")? {
            plans.push(copy(stage, true)?);
        }
    }
    for step in steps {
        let mut package = a.clone();
        package["ports"] = serde_json::json!(ports);
        package["scratch"] = serde_json::json!([]);
        package["staged"] = serde_json::json!([]);
        package["steps"] = serde_json::json!([step]);
        let outs = if text(&step, "kind")? == "prefix" {
            vec![text(&step, "dst")?]
        } else {
            names(arg(&step, "outs")?)
        };
        if let Some(list) = package["ports"].as_array_mut() {
            for port in list {
                port["written"] = outs.contains(&text(port, "name")?).into();
            }
        }
        if text(&step, "kind")? == "flat" {
            plans.push(split_flat_package(&package, &stage_request, m)?);
        } else {
            plans.push(call_split_with_timing(
                "fused.kernel",
                &package,
                &stage_request,
                m,
                true,
            )?);
        }
    }
    for stage in &stages {
        if boolean(stage, "written")? {
            plans.push(copy(stage, false)?);
        }
    }
    let mut kernel = chain::shares(&signature, plans.iter().map(|p| p.kernel.clone()).collect())?;
    for (index, plan) in plans.iter_mut().enumerate() {
        let names = chain::sequence::scope(&kernel, index)?;
        rename_regions(plan, &names);
    }
    for buffer in scratch {
        let buffer = named(&buffer)?;
        kernel.graph.scratch.push(g::Scratch {
            name: buffer.name,
            spec: crate::trace::buffer::spec(
                &buffer.shape,
                buffer.dtype,
                vec![],
                g::BufSpace::Dram,
            )?,
        });
    }
    if request.nodes.ids() == [0] {
        kernel = signature;
    }
    row_sequence(kernel, plans, request, m)
}

fn split_flat_package(
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    let package = fused_package(a)?;
    let Some(fused::Step::Flat {
        total,
        dtypes,
        out_dtype,
        ins,
        lanes,
        outs,
        kept,
        ..
    }) = package.steps.first()
    else {
        return fail("API.OP.001", "a flat stage needs its row domain");
    };
    let hw = Hw::of(m);
    let mut shape = vector::split::Shape {
        rows: *total,
        quantum: 1,
        reads: vec![],
        writes: vec![],
        tokens: true,
    };
    for (i, (name, &dtype)) in ins.iter().zip(dtypes).enumerate() {
        let o = if let Some((_, lane)) = lanes.iter().find(|(j, _)| *j == i) {
            Operand {
                dtype,
                offset: *lane,
                ..Operand::new(Kind::Scalar)
            }
        } else {
            Operand {
                dtype,
                ..Operand::full(hw.vlmax, 0)
            }
        };
        shape.reads.push(row_tensor(name.clone(), o, hw.vlmax));
    }
    for (name, &keep) in outs.iter().zip(kept) {
        if keep {
            shape.writes.push(row_tensor(
                name.clone(),
                Operand {
                    dtype: *out_dtype,
                    ..Operand::full(hw.vlmax, 0)
                },
                hw.vlmax,
            ));
        }
    }
    let signature = call("fused.kernel", a, m)?;
    vector::split::kernel(&shape, signature, request, m, |first, count| {
        fused::flat_range_kernel(&package, first, count, &a.to_string(), m)
    })
}

fn row_split(
    gen: &str,
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<Option<crate::split::PlannedKernel>> {
    let hw = Hw::of(m);
    if gen == "vector.flat_kernel"
        || gen == "convert.cast_kernel" && request.force.as_deref() != Some("p1")
    {
        let cast = gen == "convert.cast_kernel";
        let e = if cast {
            expr::load(0)
        } else {
            expr::from_table(arg(a, "e")?)?
        };
        let dtypes = if cast {
            vec![dtype(arg(a, "src")?)?]
        } else {
            dtypes(arg(a, "dtypes")?)?
        };
        let scalar = if cast {
            vec![]
        } else {
            walk(a.get("scalars").unwrap_or(&Value::Null))?
        };
        let scalar: Vec<_> = scalar.into_iter().map(|(i, l)| (i as usize, l)).collect();
        let n = int(a, "n")?;
        let out = if cast {
            dtype(arg(a, "dst")?)?
        } else {
            a.get("out_dtype").map_or(Ok(g::Dtype::F16), dtype)?
        };
        let signature = call(gen, a, m)?;
        let class = a.get("shape_class").map(class_of).transpose()?;
        let schedule = class_schedule(a);
        // A flat work item is one complete VL row. The last item's physical
        // extent is only the caller's whole words, not an implicit VL tail.
        let mut shape = vector::split::Shape {
            rows: crate::gen::cdiv(n, hw.vlmax),
            quantum: 1,
            reads: vec![],
            writes: vec![row_tensor(
                "out".into(),
                Operand {
                    dtype: out,
                    ..Operand::full(hw.vlmax, 0)
                },
                hw.vlmax,
            )],
            tokens: true,
        };
        for (i, &dtype) in dtypes.iter().enumerate() {
            let o = match scalar.iter().rev().find(|(j, _)| *j == i) {
                Some((_, lane)) => Operand {
                    dtype,
                    offset: *lane,
                    ..Operand::new(Kind::Scalar)
                },
                None => Operand {
                    dtype,
                    ..Operand::full(hw.vlmax, 0)
                },
            };
            shape.reads.push(row_tensor(format!("in{i}"), o, hw.vlmax));
        }
        let mut options = vector::split::split_options(&shape, request, m)?;
        if cast {
            for option in &mut options {
                option.id = "destination-tiles".into();
                option.axis = crate::split::SplitAxis::DestinationTiles;
                option.legality.clauses = vec![
                    "complete local vector trips; global element origins; disjoint write lines"
                        .into(),
                ];
            }
        }
        let local = |first: i64, rows: i64| {
            let first = first * hw.vlmax;
            let count = (rows * hw.vlmax).min(n - first);
            if let Some(class) = &class {
                let ops: Vec<_> = shape
                    .reads
                    .iter()
                    .map(|t| vector::split::shifted(&t.operand, first / hw.vlmax))
                    .collect();
                let output = vector::split::shifted(&shape.writes[0].operand, first / hw.vlmax);
                Ok(classes::rows::kernel(
                    &classes::Rows {
                        e: &e,
                        operands: &ops,
                        r: rows,
                        w: hw.vlmax,
                        out: &output,
                        bands: None,
                        class,
                        schedule: &schedule,
                    },
                    true,
                    m,
                )?
                .kernel)
            } else {
                vector::flat_kernel_at(&e, &dtypes, count, out, &scalar, first, n, m)
            }
        };
        let mut got = vector::split::kernel_options(options, signature, request, m, local)?;
        let bytes = if class.is_some() {
            shape.rows * hw.vlmax * Hw::item(out)
        } else {
            crate::gen::cdiv(n * Hw::item(out), hw.word_bytes) * hw.word_bytes
        };
        for region in &mut got.split.output.valid_region {
            region.bytes.end = region.bytes.end.min(bytes as u64);
            region.bytes.start = region.bytes.start.min(region.bytes.end);
        }
        for (_, region) in &mut got.split.output.ownership {
            region.bytes.end = region.bytes.end.min(bytes as u64);
            region.bytes.start = region.bytes.start.min(region.bytes.end);
        }
        for shard in &mut got.split.shards {
            for region in &mut shard.write_regions {
                region.bytes.end = region.bytes.end.min(bytes as u64);
                region.bytes.start = region.bytes.start.min(region.bytes.end);
            }
        }
        crate::split::validate_ownership(&got.split.output)?;
        return Ok(Some(got));
    }
    let product_chain = gen == "kernel.chain" && has_product(gen, a)?;
    if product_chain && request.force.as_deref() != Some("row-local") {
        return Ok(Some(split_row_chain(a, request, m)?));
    }
    if matches!(gen, "vector.rows_kernel" | "reductions.wide_fold_kernel")
        || (matches!(
            gen,
            "fused.kernel" | "kernel.chain" | "prepared.rows_kernel"
        ) && row_domain(gen, a, m)?.is_some()
            && row_local(gen, a, m)?)
    {
        let mut shape = composite_shape(gen, a, m)?;
        let signature = call(gen, a, m)?;
        for tensor in &mut shape.writes {
            if tensor.operand.kind == Kind::Row {
                if let Some(port) = signature.graph.ports.iter().find(|p| p.name == tensor.name) {
                    if port.spec.shape.len() == 2 {
                        tensor.planes = (0..port.spec.shape[0])
                            .map(|i| i * port.spec.shape[1])
                            .collect();
                    }
                }
            }
        }
        let mut local_request = request.clone();
        if product_chain {
            local_request.force = Some("rows".into());
        }
        let mut planned =
            vector::split::kernel(&shape, signature, &local_request, m, |first, count| {
                row_kernel_at(gen, a, first, count, m)
            })?;
        if product_chain {
            planned.split.id = "row-local".into();
            planned.request = request.clone();
        }
        return Ok(Some(planned));
    }
    if gen == "kernel.chain" {
        return Ok(Some(split_row_chain(a, request, m)?));
    }
    if gen == "kernel.sequence" && request.force.as_deref() != Some("p1") {
        return Ok(Some(split_sequence(a, request, m)?));
    }
    if gen == "fused.kernel" {
        let package = fused_package(a)?;
        if a.get("group_ends").is_some() {
            return fused::groups::split(&package, a, request, m);
        }
        if package.staged.is_empty()
            && package.scratch.is_empty()
            && fused::planes::independent(&package)
        {
            return Ok(Some(fused::planes::split(
                &package,
                &a.to_string(),
                request,
                m,
            )?));
        }
        if package.staged.is_empty()
            && package.scratch.is_empty()
            && matches!(package.steps.as_slice(), [fused::Step::Prefix { .. }])
        {
            if let fused::Step::Prefix { src, dst, boundary } = &package.steps[0] {
                boundary.validate(m)?;
                if src == dst && !boundary.independent_alias() {
                    return Ok(None);
                }
            }
            return Ok(Some(fused::prefix_split(
                &package,
                &a.to_string(),
                request,
                m,
            )?));
        }
        if !package.steps.is_empty()
            && package.steps.iter().all(|s| {
                matches!(
                    s,
                    fused::Step::Rows { .. }
                        | fused::Step::Flat { .. }
                        | fused::Step::Prefix { .. }
                )
            })
        {
            return Ok(Some(split_row_package(a, request, m)?));
        }
    }
    Ok(None)
}

/// Plan one logical invocation on an explicit sysnode grant.
/// Generators without distributed options expose their one-share implementation.
pub fn call_split(
    gen: &str,
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
) -> R<crate::split::PlannedKernel> {
    call_split_with_timing(gen, a, request, m, false)
}

/// Sequence stages need local timing even when their only option is unsplit.
pub fn call_split_with_timing(
    gen: &str,
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
    price_unsplit: bool,
) -> R<crate::split::PlannedKernel> {
    ktpu_graph::with_cycle_cache(|| split_with_timing(gen, a, request, m, price_unsplit))
}

fn split_with_timing(
    gen: &str,
    a: &Value,
    request: &crate::split::SplitRequest,
    m: &Machine,
    price_unsplit: bool,
) -> R<crate::split::PlannedKernel> {
    use crate::split::{
        CostEstimate, LegalityWitness, OutputLayout, PlannedKernel, ShardRole, ShardTiles,
        SplitAxis, SplitOption,
    };
    let nodes = crate::split::NodeSet::new(request.nodes.ids().to_vec(), m)?;
    if (!request.consumer.accepted_forms.is_empty()
        && !request
            .consumer
            .accepted_forms
            .iter()
            .any(|form| form == "producer"))
        || !request.consumer.scale_groups.is_empty()
    {
        return fail(
            "DSL.KERN.020",
            "p1 has no option satisfying the requested output form or scale groups",
        );
    }
    if (!request.numerics.precision.is_empty() && request.numerics.precision != "generator")
        || (!request.numerics.reduction_order.is_empty()
            && gen != "matmul.mm_kernel"
            && gen != "mm")
    {
        return fail(
            "DSL.KERN.020",
            "p1 preserves generator numerics; no requested precision or reduction-order option",
        );
    }
    if gen == "convert.prefix_kernel" && request.force.as_deref() != Some("p1") {
        return convert::prefix::Prefix::read(a, m)?.split(request, m);
    }
    if gen == "convert.rows_kernel" && request.force.as_deref() != Some("p1") {
        return convert::rows::Boundary::read(a, m)?.split(request, m);
    }
    if gen == "groupnorm.kernel" {
        let spec = serde_json::from_value(arg(a, "spec")?.clone()).map_err(|e| {
            ktpu_machine::Refusal::new("API.OP.001", format!("GroupNorm descriptor: {e}"))
        })?;
        return crate::gen::groupnorm::kernel(&spec, request, m);
    }
    if gen == "conv.kernel" {
        let spec = serde_json::from_value(arg(a, "spec")?.clone()).map_err(|e| {
            ktpu_machine::Refusal::new("API.OP.001", format!("convolution descriptor: {e}"))
        })?;
        return crate::gen::conv::kernel(&spec, arg(a, "product")?, request, m);
    }
    if gen == "conv.patch_kernel" {
        let spec = serde_json::from_value(arg(a, "spec")?.clone()).map_err(|e| {
            ktpu_machine::Refusal::new("API.OP.001", format!("convolution descriptor: {e}"))
        })?;
        return crate::gen::conv::patch_split(&spec, request, m);
    }
    if matches!(
        gen,
        "attention.flash" | "attention.flashd" | "attention.srflash"
    ) {
        let args = a.as_object().ok_or_else(|| {
            ktpu_machine::Refusal::new(
                "API.OP.001",
                "attention template expects an argument object",
            )
        })?;
        let default_heads = if gen == "attention.srflash" { 2 } else { 10 };
        let heads = args.get("heads").map_or(Some(default_heads), Value::as_i64);
        let queries = args.get("queries").map_or(Some(1024), Value::as_i64);
        let (Some(heads), Some(queries)) = (heads, queries) else {
            return fail("API.OP.001", "flash heads and queries must be integers");
        };
        if args
            .keys()
            .any(|key| !matches!(key.as_str(), "heads" | "queries" | "contract"))
        {
            return fail("API.OP.001", "unknown attention template argument");
        }
        let contract = args
            .get("contract")
            .map(|value| {
                value.as_str().ok_or_else(|| {
                    ktpu_machine::Refusal::new("API.OP.001", "attention contract must be a string")
                })
            })
            .transpose()?;
        use crate::gen::attention::motif::AttentionMotif;
        let motif = match gen {
            "attention.flashd" => AttentionMotif::Flashd,
            "attention.srflash" => AttentionMotif::Srflash,
            _ => AttentionMotif::Flash,
        };
        return crate::gen::attention::flash_split::plan_motif(
            heads, queries, motif, contract, request, m,
        );
    }
    if (gen == "matmul.mm_kernel" || gen == "mm")
        && (nodes.ids().len() > 1 || request.force.as_deref().is_some_and(|id| id != "p1"))
    {
        let class = a.get("shape_class").map(class_of).transpose()?;
        let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
        let e = expr::from_table(arg(a, "e")?)?;
        let extras = operands(arg(a, "extras")?)?;
        let out = operand(arg(a, "out")?)?;
        let knobs = matmul::unrecorded()
            .read(made_by(arg(a, "mm")?, "matmul.plan")?, matmul::Group::Plan)?
            .read(a, matmul::Group::Kernel)?;
        let made = if class.is_some() {
            BTreeMap::new()
        } else {
            made_reads(a, m)?
        };
        let demands = if class.is_some() { vec![] } else { demands(a)? };
        return matmul::split::kernel_with_policy(
            &matmul::split::Shape {
                class: class.as_ref(),
                mm: &mm,
                e: &e,
                extras: &extras,
                out: &out,
                knobs: &knobs,
                made: &made,
                demands: &demands,
            },
            request,
            m,
            price_unsplit,
            chain::quant::Backend::read(a)?,
        );
    }
    if gen == "transpose.transpose_kernel" && request.force.as_deref() != Some("p1") {
        let class = a.get("shape_class").map(class_of).transpose()?;
        return transpose::destination::kernel(
            int(a, "nb")?,
            int(a, "A")?,
            int(a, "B")?,
            class.as_ref(),
            request,
            m,
        );
    }
    if gen == "relayout.relayout_kernel" && request.force.as_deref() != Some("p1") {
        if let Ok(plan) = relayout_plan(made_by(arg(a, "pl")?, "relayout.plan")?, m)? {
            if let Some(words) = relayout::split::independent_words(&plan) {
                let mut single = request.clone();
                single.force = Some("p1".into());
                let original = call_split_with_timing(gen, a, &single, m, true)?;
                return relayout::split::kernel(&plan, words, original, request, m);
            }
        }
    }
    if gen == "transpose.reverse_kernel" && request.force.as_deref() != Some("p1") {
        return transpose::reverse_split(
            int(a, "src_bytes")?,
            &walk(arg(a, "levels")?)?,
            int(a, "at")?,
            int(a, "n")?,
            request,
            m,
        );
    }
    if gen == "matmul.quant_walk_kernel" && request.force.as_deref() != Some("p1") {
        let rows = int(a, "rows")?;
        let cols = int(a, "cols")?;
        let batches = a.get("batches").map_or(Ok(1), |_| int(a, "batches"))?;
        return matmul::destination::kernel(
            &Operand {
                kind: Kind::Full,
                dtype: g::Dtype::F16,
                offset: 0,
                pitch: cols,
                rows: batches * rows,
            },
            rows,
            cols,
            int(a, "rp")?,
            int(a, "cp")?,
            quant_side(a)?,
            int(a, "g")?,
            int(a, "nk")?,
            batches,
            a.get("finite")
                .map_or(Ok(false), |_| boolean(a, "finite"))?,
            &quant_schedule(a)?,
            false,
            Some((
                int(a, "src_bytes")?,
                &walk(arg(a, "levels")?)?,
                int(a, "at")?,
            )),
            None,
            request,
            m,
        );
    }
    let quant_read = if gen == "matmul.quant_kernel"
        && a.get("shape_class").is_none()
        && request.force.as_deref() != Some("p1")
    {
        Some(read_of(a, 0, a.get("prepared").unwrap_or(&Value::Null), m)?)
    } else {
        None
    };
    if quant_read.as_ref().is_some_and(|read| match read {
        prepared::Read::Composed(_) => true,
        prepared::Read::Materialized(steps) => steps.is_empty(),
    }) {
        let staged = match a.get("pad_strategy").map(Value::as_str) {
            None | Some(Some("native")) => false,
            Some(Some("staged")) => true,
            Some(_) => return fail("API.OP.001", format!("a pad strategy in {a}")),
        };
        let mapped = match &quant_read {
            Some(prepared::Read::Composed(steps)) => Some(steps.as_slice()),
            _ => None,
        };
        return matmul::destination::kernel(
            &operand(arg(a, "src")?)?,
            int(a, "rows")?,
            int(a, "cols")?,
            int(a, "rp")?,
            int(a, "cp")?,
            quant_side(a)?,
            int(a, "g")?,
            int(a, "nk")?,
            a.get("batches").map_or(Ok(1), |_| int(a, "batches"))?,
            a.get("finite")
                .map_or(Ok(false), |_| boolean(a, "finite"))?,
            &quant_schedule(a)?,
            staged,
            None,
            mapped,
            request,
            m,
        );
    }
    if request.force.as_deref() != Some("p1") {
        let class = a.get("shape_class").map(class_of).transpose()?;
        let planned = match gen {
            "convert.copy_kernel" if class.is_some() => {
                let hw = Hw::of(m);
                let bytes = int(a, "nbytes")?;
                let count = crate::gen::cdiv(bytes, hw.word_bytes);
                let levels = [(count, hw.word_bytes)];
                convert::destination::walk(
                    bytes,
                    &levels,
                    0,
                    bytes,
                    &levels,
                    0,
                    class.as_ref(),
                    request,
                    m,
                )?
            }
            "convert.walk_kernel" => convert::destination::walk(
                int(a, "src_bytes")?,
                &walk(arg(a, "levels")?)?,
                int(a, "at")?,
                int(a, "dst_bytes")?,
                &walk(arg(a, "dlevels")?)?,
                int(a, "dat")?,
                class.as_ref(),
                request,
                m,
            )?,
            "convert.fill_kernel" => convert::destination::fill(
                int(a, "dst_bytes")?,
                &walk(arg(a, "dlevels")?)?,
                int(a, "dat")?,
                int(a, "value")?,
                int(a, "ew")?,
                request,
                m,
            )?,
            _ => None,
        };
        if let Some(planned) = planned {
            return Ok(planned);
        }
    }
    if gen == "convert.copy_kernel"
        && a.get("shape_class").is_none()
        && request.force.as_deref() != Some("p1")
    {
        return convert::copy_split(int(a, "nbytes")?, request, m);
    }
    if let Some(planned) = row_split(gen, a, request, m)? {
        return Ok(planned);
    }
    if !request.numerics.reduction_order.is_empty() {
        return fail("DSL.KERN.020", "p1 preserves the generator reduction order");
    }
    let generator = if gen == "mm" { "matmul.mm_kernel" } else { gen };
    let kernel = if nodes.ids()[0] == 0 {
        call(generator, a, m)?
    } else {
        crate::kernel::on_node(nodes.ids()[0], || call(generator, a, m))?
    };
    let tensors = kernel
        .graph
        .ports
        .iter()
        .filter(|p| p.role == ktpu_graph::Role::Out)
        .map(|p| (p.name.clone(), p.spec.clone()))
        .collect();
    let mut option = SplitOption {
        id: "p1".into(),
        axis: SplitAxis::Unsplit,
        p: 1,
        active_nodes: 1,
        shards: nodes
            .ids()
            .iter()
            .enumerate()
            .map(|(logical_node, _)| ShardTiles {
                logical_node,
                tile_ranges: vec![],
                logical_origin: vec![],
                valid_extent: vec![],
                padded_extent: vec![],
                read_regions: vec![],
                write_regions: vec![],
                scratch_regions: vec![],
                local_schedule: gen.into(),
                role: if logical_node == 0 {
                    ShardRole::Compute
                } else {
                    ShardRole::Empty
                },
            })
            .collect(),
        output: OutputLayout {
            form: "producer".into(),
            tensors,
            ..OutputLayout::default()
        },
        events: vec![],
        conversions: 0,
        cost: CostEstimate::default(),
        legality: LegalityWitness {
            numerics: crate::split::NumericContract {
                precision: "generator".into(),
                reduction_order: vec![],
            },
            clauses: vec!["single owner; physical regions resolved at binding".into()],
        },
    };
    let local_reason = match gen {
        "lanes.relayout_kernel" | "lanes.class_kernel" => Some(
            "indexed lane classes share destination lines; table ports select write locations and padded classes repeat destinations",
        ),
        "relayout.relayout_kernel" => Some(
            "complete local relayout; any indexed destinations and ordered merge layers retain one owner",
        ),
        "matmul.quant_kernel" if a.get("shape_class").is_some() => Some(
            "classed packing preserves the complete node-loop entry schedule and finite-status reduction",
        ),
        "matmul.quant_kernel" => Some(
            "materialized prepared reads retain their ordered relayout stages and shared scratch before packing",
        ),
        "convert.walk_kernel" | "convert.fill_kernel" => Some(
            "the complete destination walk retains ordered writes and partial-line ownership; this option does not reorder its descriptor stream",
        ),
        "convert.copy_kernel" | "convert.cast_kernel" => Some(
            "explicit P=1 preserves the complete local conversion schedule",
        ),
        _ => None,
    };
    if let Some(reason) = local_reason {
        convert::local::certify(&mut option, &kernel, reason)?;
    }
    if request.force.as_deref().is_some_and(|id| id != option.id) {
        return fail(
            "DSL.KERN.020",
            local_reason.map_or_else(
                || "no matching legal split option".to_string(),
                |reason| format!("this destination schedule exposes p1: {reason}"),
            ),
        );
    }
    let split = if price_unsplit {
        let features = crate::split::CostFeatures::of_shares(std::slice::from_ref(&kernel), m)?;
        crate::split::cost::price_option(option, &features, &crate::split::LocalTiming)?
    } else {
        option
    };
    Ok(PlannedKernel {
        l1: None,
        kernel,
        split,
        request: request.clone(),
    })
}

/// The kernel of one recorded generator call, built for `m`.
pub fn call(gen: &str, a: &Value, m: &Machine) -> R<Kernel> {
    ktpu_graph::with_cycle_cache(|| construct(gen, a, m))
}

fn construct(gen: &str, a: &Value, m: &Machine) -> R<Kernel> {
    if a.get("shape_class").is_some() || gen == "lanes.class_kernel" {
        let kernel = classed(gen, a, m)?.kernel;
        return if gen == "matmul.mm_kernel" {
            chain::quant::kernel(kernel, chain::quant::Backend::read(a)?, m)
        } else {
            Ok(kernel)
        };
    }
    let e = |k: &str| expr::from_table(arg(a, k)?);
    Ok(match gen {
        "groupnorm.kernel" => {
            let spec = serde_json::from_value(arg(a, "spec")?.clone()).map_err(|e| {
                ktpu_machine::Refusal::new("API.OP.001", format!("GroupNorm descriptor: {e}"))
            })?;
            let request = crate::split::SplitRequest {
                nodes: crate::split::NodeSet::new(vec![0], m)?,
                consumer: crate::split::OutputRequirement::default(),
                dispatch: crate::split::DispatchProfile::Common,
                numerics: crate::split::NumericContract::default(),
                input_forms: vec![],
                force: Some("domains".into()),
            };
            crate::gen::groupnorm::kernel(&spec, &request, m)?.kernel
        }
        "conv.unfold_kernel" => {
            let spec: crate::gen::conv::ConvSpec = serde_json::from_value(arg(a, "spec")?.clone())
                .map_err(|e| {
                    ktpu_machine::Refusal::new("API.OP.001", format!("convolution descriptor: {e}"))
                })?;
            let space = match a
                .get("scratch_space")
                .and_then(Value::as_str)
                .unwrap_or("dram")
            {
                "stage" => ktpu_graph::BufSpace::Stage,
                "dram" => ktpu_graph::BufSpace::Dram,
                _ => return fail("API.OP.001", "bulk patch scratch must be stage or dram"),
            };
            crate::gen::conv::unfold(
                &spec,
                int(a, "first_batch")?,
                int(a, "batches")?,
                int(a, "capacity")?,
                space,
                m,
            )?
        }
        "conv.patch_kernel" => {
            let spec = serde_json::from_value(arg(a, "spec")?.clone()).map_err(|e| {
                ktpu_machine::Refusal::new("API.OP.001", format!("convolution descriptor: {e}"))
            })?;
            crate::gen::conv::patches(&spec, m)?
        }
        "groupnorm.merge_kernel" => {
            crate::gen::groupnorm::merge_kernel(int(a, "na")?, int(a, "nb")?, m)?
        }
        "reductions.wide_fold_kernel" => {
            let (out, r, w) = (operand(arg(a, "out")?)?, int(a, "R")?, int(a, "W")?);
            let ops = operands(arg(a, "operands")?)?;
            reductions::wide_fold_kernel(&e("e")?, &ops, r, w, &out, m)?.0
        }
        "reductions.column_tree_kernel" => {
            let (out, r, w) = (operand(arg(a, "out")?)?, int(a, "R")?, int(a, "W")?);
            let ops = operands(arg(a, "operands")?)?;
            reductions::column_tree_kernel(&e("e")?, &ops, r, w, &out, &made_reads(a, m)?, m)?
        }
        "vector.rows_kernel" => {
            let bands = a.get("bands").and_then(Value::as_i64);
            let (out, r, w) = (operand(arg(a, "out")?)?, int(a, "R")?, int(a, "W")?);
            vector::rows_kernel(
                &e("e")?,
                &operands(arg(a, "operands")?)?,
                r,
                w,
                &out,
                bands,
                m,
            )?
            .0
        }
        "vector.flat_kernel" => {
            let dtypes = arg(a, "dtypes")?
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or_default()
                .iter()
                .map(dtype)
                .collect::<R<Vec<_>>>()?;
            let scalars = walk(a.get("scalars").unwrap_or(&Value::Null))?;
            let scalars: Vec<(usize, i64)> =
                scalars.into_iter().map(|(i, l)| (i as usize, l)).collect();
            let out = a.get("out_dtype").map_or(Ok(g::Dtype::F16), dtype)?;
            vector::flat_kernel(&e("e")?, &dtypes, int(a, "n")?, out, &scalars, m)?
        }
        "matmul.quant_walk_kernel" => matmul::quant_walk_kernel(
            int(a, "src_bytes")?,
            &walk(arg(a, "levels")?)?,
            int(a, "at")?,
            int(a, "rows")?,
            int(a, "cols")?,
            int(a, "rp")?,
            int(a, "cp")?,
            quant_side(a)?,
            int(a, "g")?,
            int(a, "nk")?,
            a.get("batches").map_or(Ok(1), |_| int(a, "batches"))?,
            m,
            a.get("finite")
                .map_or(Ok(false), |_| boolean(a, "finite"))?,
            &quant_schedule(a)?,
        )?,
        "matmul.quant_kernel" => {
            let side = quant_side(a)?;
            let schedule = quant_schedule(a)?;
            let staged = match a.get("pad_strategy").map(Value::as_str) {
                None | Some(Some("native")) => false,
                Some(Some("staged")) => true,
                Some(_) => return fail("API.OP.001", format!("a pad strategy in {a}")),
            };
            matmul::quant_kernel_scheduled(
                &operand(arg(a, "src")?)?,
                int(a, "rows")?,
                int(a, "cols")?,
                int(a, "rp")?,
                int(a, "cp")?,
                side,
                int(a, "g")?,
                int(a, "nk")?,
                a.get("batches").map_or(Ok(1), |_| int(a, "batches"))?,
                m,
                a.get("finite")
                    .map_or(Ok(false), |_| boolean(a, "finite"))?,
                &schedule,
                &read_of(a, 0, a.get("prepared").unwrap_or(&Value::Null), m)?,
                staged,
            )?
        }
        "matmul.mm_kernel" => {
            let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
            let extras = operands(arg(a, "extras")?)?;
            let knobs = matmul::unrecorded().read(a, matmul::Group::Kernel)?;
            let kernel = matmul::mm_kernel_made(
                &mm,
                &e("e")?,
                &extras,
                &operand(arg(a, "out")?)?,
                &knobs,
                m,
                &made_reads(a, m)?,
                &demands(a)?,
            )?;
            chain::quant::kernel(kernel, chain::quant::Backend::read(a)?, m)?
        }
        "fused.kernel" => {
            let package = fused_package(a)?;
            fused::groups::boundaries(a, package.steps.len())?;
            if a.get("quant_backend").is_some() || a.get("pipeline_lookahead").is_some() {
                fused::scheduled(&package, a, &a.to_string(), m)?
            } else {
                fused::kernel(&package, &a.to_string(), m)?
            }
        }
        "kernel.sequence" => {
            let descriptors = arg(a, "members")?.as_array().ok_or_else(|| {
                ktpu_machine::Refusal::new("API.OP.001", "sequence members must be an array")
            })?;
            let members = descriptors
                .iter()
                .map(|descriptor| {
                    chain::sequence::deferred(descriptor)?;
                    call(&text(descriptor, "gen")?, arg(descriptor, "args")?, m)
                })
                .collect::<R<Vec<_>>>()?;
            let kernel = chain::sequence::kernel(members)?;
            chain::sequence::finish(kernel, a, m)?
        }
        "kernel.chain" => {
            let part = |k: &str| -> R<Kernel> {
                let p = arg(a, k)?;
                let gen = arg(p, "gen")?.as_str().unwrap_or_default().to_string();
                call(&gen, arg(p, "args")?, m)
            };
            let text = |k: &str| arg(a, k).map(|v| v.as_str().unwrap_or_default().to_string());
            let mut kernel = chain::scheduled(
                part("first")?,
                part("second")?,
                &text("from")?,
                &text("to")?,
                boolean(a, "keep")?,
                wave_consumer(a, m)?.as_ref(),
                chain::link::View::read(a)?,
                m,
            )?;
            chain::input_boundary(&mut kernel, a, m)?;
            chain::crop_boundary(&mut kernel, a, m)?;
            let (kernel, _) = chain::finish(kernel, a, m, true)?;
            chain::pooled_ordered(kernel, a, m)?
        }
        "convert.rows_kernel" => convert::rows::Boundary::read(a, m)?.kernel(m)?,
        "convert.prefix_kernel" => convert::prefix::Prefix::read(a, m)?.kernel(m)?,
        "convert.copy_kernel" => convert::copy_kernel(int(a, "nbytes")?, m)?,
        "convert.walk_kernel" => convert::walk_kernel(
            int(a, "src_bytes")?,
            &walk(arg(a, "levels")?)?,
            int(a, "at")?,
            int(a, "dst_bytes")?,
            &walk(arg(a, "dlevels")?)?,
            int(a, "dat")?,
            m,
        )?,
        "convert.fill_kernel" => convert::fill_kernel(
            int(a, "dst_bytes")?,
            &walk(arg(a, "dlevels")?)?,
            int(a, "dat")?,
            int(a, "value")?,
            int(a, "ew")?,
            m,
        )?,
        "convert.cast_kernel" => convert::cast_kernel(
            int(a, "n")?,
            dtype(arg(a, "src")?)?,
            dtype(arg(a, "dst")?)?,
            m,
        )?,
        "transpose.transpose_kernel" => {
            transpose::transpose_kernel(int(a, "nb")?, int(a, "A")?, int(a, "B")?, m)?
        }
        "transpose.reverse_kernel" => transpose::reverse_kernel(
            int(a, "src_bytes")?,
            &walk(arg(a, "levels")?)?,
            int(a, "at")?,
            int(a, "n")?,
            m,
        )?,
        "lanes.relayout_kernel" => {
            let pl = lanes_plan(made_by(arg(a, "pl")?, "lanes.plan")?, m)?;
            lanes::relayout_kernel(&pl, m)?
        }
        "relayout.relayout_kernel" => {
            match relayout_plan(made_by(arg(a, "pl")?, "relayout.plan")?, m)? {
                Ok(pl) => relayout::relayout_kernel(&pl, m)?,
                Err(relayout::Unplanned(why)) => {
                    return fail(
                        "API.OP.001",
                        format!("a relayout.plan call no form takes: {why}"),
                    )
                }
            }
        }
        "prepared.rows_kernel" => {
            let (out, r, w) = (operand(arg(a, "out")?)?, int(a, "R")?, int(a, "W")?);
            let made = made_reads(a, m)?;
            let mut alias = BTreeMap::new();
            for (i, j) in indexed(a.get("alias").unwrap_or(&Value::Null))? {
                match j.as_u64() {
                    Some(j) => alias.insert(i, j as usize),
                    None => return fail("API.OP.001", format!("an alias operand {j}")),
                };
            }
            prepared::rows_kernel(
                &e("e")?,
                &operands(arg(a, "operands")?)?,
                r,
                w,
                &out,
                &made,
                &alias,
                m,
            )?
        }
        other => return fail("API.OP.001", format!("no kernel generator {other:?}")),
    })
}
