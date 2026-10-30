//! A generator call as the planner goldens record it
//! (`golden/plan_dump.py`): `{"gen": "<module>.<fn>", "args": {python
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
use crate::gen::{convert, lanes, matmul, prepared, reductions, relayout, transpose, vector};
use crate::kernel::Kernel;
use crate::trace::{fail, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::Value;
use std::collections::BTreeMap;

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

/// A `matmul.mm_kernel` call's kernel knobs as its kernel takes them
/// (`matmul::effective`), and whether that is the kernel of none.
pub fn mm_resolved(a: &Value, m: &Machine) -> R<Value> {
    let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
    let e = expr::from_table(arg(a, "e")?)?;
    let knobs = matmul::unrecorded().read(a, matmul::Group::Kernel)?;
    let got = matmul::effective(&mm, &e, &operand(arg(a, "out")?)?, &knobs, m);
    let plain = got.args(matmul::Group::Kernel) == matmul::unrecorded().args(matmul::Group::Kernel);
    let knobs: serde_json::Map<String, Value> = got
        .args(matmul::Group::Kernel)
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect();
    Ok(serde_json::json!({"knobs": knobs, "plain": plain}))
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

/// `made`: operand index -> the `relayout.plan` calls making it (none when
/// left out).
fn made_plans(a: &Value, m: &Machine) -> R<BTreeMap<usize, Vec<relayout::Plan>>> {
    let mut made = BTreeMap::new();
    for (i, plans) in indexed(a.get("made").unwrap_or(&Value::Null))? {
        made.insert(i, plan_list(plans, m)?);
    }
    Ok(made)
}

/// The kernel of one recorded generator call, built for `m`.
pub fn call(gen: &str, a: &Value, m: &Machine) -> R<Kernel> {
    let e = |k: &str| expr::from_table(arg(a, k)?);
    Ok(match gen {
        "reductions.wide_fold_kernel" => {
            let (out, r, w) = (operand(arg(a, "out")?)?, int(a, "R")?, int(a, "W")?);
            let ops = operands(arg(a, "operands")?)?;
            reductions::wide_fold_kernel(&e("e")?, &ops, r, w, &out, m)?.0
        }
        "reductions.column_tree_kernel" => {
            let (out, r, w) = (operand(arg(a, "out")?)?, int(a, "R")?, int(a, "W")?);
            let ops = operands(arg(a, "operands")?)?;
            reductions::column_tree_kernel(&e("e")?, &ops, r, w, &out, &made_plans(a, m)?, m)?
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
        "matmul.quant_kernel" => {
            let side = match arg(a, "side")?.as_str() {
                Some("a") => matmul::Side::A,
                Some("b") => matmul::Side::B,
                _ => return fail("API.FORM.006", format!("a packing side in {a}")),
            };
            let schedule = matmul::QUANT_SCHEDULE
                .iter()
                .map(|&(key, default)| {
                    let value = a.get("schedule").and_then(|s| s.get(key));
                    match value {
                        None => Ok((key, default)),
                        Some(v) => match v.as_i64() {
                            Some(v) => Ok((key, v)),
                            None => {
                                fail("API.OP.001", format!("quant schedule {key} is an integer"))
                            }
                        },
                    }
                })
                .collect::<R<Vec<_>>>()?;
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
                &plan_list(a.get("prepared").unwrap_or(&Value::Null), m)?,
                staged,
            )?
        }
        "matmul.mm_kernel" => {
            let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
            let extras = operands(arg(a, "extras")?)?;
            let knobs = matmul::unrecorded().read(a, matmul::Group::Kernel)?;
            matmul::mm_kernel_made(
                &mm,
                &e("e")?,
                &extras,
                &operand(arg(a, "out")?)?,
                &knobs,
                m,
                &made_plans(a, m)?,
            )?
        }
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
            let made = made_plans(a, m)?;
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
