//! A plan's JSON form: its ranges, reads and array digests.

use super::*;

fn rng(g: &Graph, r: Id) -> program::Range {
    let (id, axis) = range_id(g, r);
    program::Range {
        range: id,
        extent: extent(g, r),
        axis,
    }
}

fn acc_json(g: &Graph, a: &Access) -> program::Access {
    program::Access {
        slot: match &a.slot {
            Slot::Param(s) => serde_json::json!(s),
            Slot::Table => serde_json::Value::Null,
            Slot::Temp(k) => serde_json::json!(k),
        },
        dtype: a.dtype.clone(),
        coef: a.coef.iter().map(|(r, c)| (rng(g, *r), *c)).collect(),
        off: a.off,
        table: a.table.as_ref().map(Table::digest),
        gate: a.gate,
    }
}

/// An array's digest as numpy holds it.
pub fn arr_digest(a: &Arr) -> Array {
    let mut h = Sha256::new();
    let n = a.len();
    for i in 0..n {
        match a.ty {
            Ty::Bool | Ty::I8 | Ty::U8 => h.update([a.ints[i] as u8]),
            Ty::I16 | Ty::U16 => h.update((a.ints[i] as u16).to_le_bytes()),
            Ty::I32 | Ty::U32 => h.update((a.ints[i] as u32).to_le_bytes()),
            Ty::I64 | Ty::U64 => h.update((a.ints[i] as u64).to_le_bytes()),
            Ty::F16 => h.update(fold::f16_bits(a.floats[i]).to_le_bytes()),
            Ty::F32 => h.update((a.floats[i] as f32).to_le_bytes()),
            Ty::F64 => h.update(a.floats[i].to_le_bytes()),
        }
    }
    let dtype = match a.ty {
        Ty::Bool => "bool",
        Ty::I8 => "int8",
        Ty::I16 => "int16",
        Ty::I32 => "int32",
        Ty::I64 => "int64",
        Ty::U8 => "uint8",
        Ty::U16 => "uint16",
        Ty::U32 => "uint32",
        Ty::U64 => "uint64",
        Ty::F16 => "float16",
        Ty::F32 => "float32",
        Ty::F64 => "float64",
    };
    Array {
        dtype: dtype.into(),
        shape: a.shape.iter().map(|d| *d as i64).collect(),
        sha256: hex(&h.finalize()),
    }
}

/// The plan as the golden records it (`plan_dump.Recorder.plan_json`).
pub fn plan_json(g: &Graph, p: &Plan) -> program::Plan {
    let mut ids = HashMap::new();
    let table = p.expr.as_ref().map(|e| expr::table(e, &mut ids));
    let mut reductions: Vec<(usize, Vec<program::Range>)> = p
        .reductions
        .iter()
        .filter_map(|(e, rs)| {
            ids.get(&(Rc::as_ptr(e) as usize))
                .map(|i| (*i, rs.iter().map(|r| rng(g, *r)).collect()))
        })
        .collect();
    reductions.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| format!("{:?}", a.1).cmp(&format!("{:?}", b.1)))
    });
    program::Plan {
        kind: p.kind.clone(),
        name: p.name.clone(),
        out_slot: match p.out_slot {
            Slot::Param(n) => n,
            _ => -1,
        },
        out_dtype: p.out_dtype.clone(),
        out_shape: p.out_shape.clone(),
        out_ranges: p.out_ranges.iter().map(|r| rng(g, *r)).collect(),
        expr: table,
        accesses: p.accesses.iter().map(|a| acc_json(g, a)).collect(),
        reduce_ranges: p.reduce_ranges.iter().map(|r| rng(g, *r)).collect(),
        reductions,
        value: p.value.as_ref().map(arr_digest),
        a: p.a.as_ref().map(|a| acc_json(g, a)),
        b: p.b.as_ref().map(|a| acc_json(g, a)),
        out_strides: p.out_strides.clone(),
        mnk: p.mnk.as_ref().map(|(m, n, ks)| {
            (
                rng(g, *m),
                rng(g, *n),
                ks.iter().map(|k| rng(g, *k)).collect(),
            )
        }),
        batch: p
            .mnk
            .as_ref()
            .map(|_| p.batch.iter().map(|r| rng(g, *r)).collect()),
    }
}
