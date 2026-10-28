//! A generator call as the planner goldens record it
//! (`golden/plan_dump.py`): `{"gen": "<module>.<fn>", "args": {python
//! keyword: value}}`. An `Operand` is its field dict, an expression its node
//! table ([`expr::from_table`]), an `Mm` / lanes plan `{"made_by": CALL}`
//! (the `matmul.plan` / `lanes.plan` call, made again here), tuples lists.
//! `cores` / `clusters` are the machine's (RUL.016); a recorded value is
//! not read. An argument left out takes the Python generator's default
//! (`shared` True, `quantum` 4, `batches` 1, `out_dtype` f16, `scalars` and
//! `bands` none).

use crate::expr;
use crate::gen::vector::{Kind, Operand};
use crate::gen::{convert, lanes, matmul, transpose, vector};
use crate::kernel::Kernel;
use crate::trace::{fail, R};
use ktpu_graph as g;
use ktpu_machine::Machine;
use serde_json::Value;

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
        m,
    )
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

/// The kernel of one recorded generator call, built for `m`.
pub fn call(gen: &str, a: &Value, m: &Machine) -> R<Kernel> {
    let e = |k: &str| expr::from_table(arg(a, k)?);
    Ok(match gen {
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
            )?
        }
        "matmul.mm_kernel" => {
            let mm = mm_plan(made_by(arg(a, "mm")?, "matmul.plan")?, m)?;
            let extras = operands(arg(a, "extras")?)?;
            matmul::mm_kernel(&mm, &e("e")?, &extras, &operand(arg(a, "out")?)?, m)?
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
        other => return fail("API.OP.001", format!("no kernel generator {other:?}")),
    })
}
