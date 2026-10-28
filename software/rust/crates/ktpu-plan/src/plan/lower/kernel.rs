//! `lower.plan`: the kernel ast's envelope, store and value as a `const`,
//! `copy`, `matmul` or `vector` plan.

use super::*;

/// `_order`: the store's ranges, outermost first, one dense walk.
fn order(g: &Graph, coef: &[(Id, i64)], ranges: &[Id]) -> Result<Vec<Id>, Refusal> {
    if coef.iter().any(|(r, _)| !ranges.contains(r)) {
        return refuse(
            "TG.PLAN.012",
            "the store reads a range outside its loop nest",
        );
    }
    if ranges
        .iter()
        .any(|r| !coef.iter().any(|(x, _)| x == r) && extent(g, *r) > 1)
    {
        return refuse(
            "TG.PLAN.012",
            "a loop range the store does not index (a broadcast store)",
        );
    }
    let mut order: Vec<(Id, i64)> = coef.to_vec();
    order.sort_by_key(|(_, c)| -c);
    let mut want = 1;
    for (r, c) in order.iter().rev() {
        if *c != want {
            return refuse(
                "TG.PLAN.012",
                "the store is not one dense row-major walk of its loop nest",
            );
        }
        want *= extent(g, *r);
    }
    let mut out: Vec<Id> = order.into_iter().map(|(r, _)| r).collect();
    if mutant::on("tg.lower/order") {
        out.reverse();
    }
    Ok(out)
}

fn kernel_name(g: &Graph, ast: Id) -> String {
    if let Arg::Other(text) = g.arg(ast) {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(text) {
            if let Some(n) = v["fields"]["name"].as_str() {
                return n.to_string();
            }
        }
    }
    "kernel".into()
}

pub(super) fn param_slot(g: &Graph, p: Id) -> (i64, String) {
    match g.arg(p) {
        Arg::Param { slot, dtype, .. } => (*slot, dtype.clone()),
        _ => (-1, g.dtype(p).to_string()),
    }
}

/// `lower.plan`.
pub fn plan(uops: &Uops) -> Result<Lowered, Refusal> {
    let (mut g, ast) = Graph::read(uops);
    let Some((_, env_ranges)) = envelope(&g, ast) else {
        return refuse(
            "TG.PLAN.011",
            format!("not one stored loop nest; it holds {}", summarise(&g, ast)),
        );
    };
    let (ast, digits) = unfold(&mut g, ast);
    let (ast, digits) = if mutant::on("tg.lower/no_unfold") {
        (ast, digits)
    } else {
        domain::normalise(&mut g, ast, digits)
    };
    let top = g.src(ast)[0];
    let store = if g.op(top) == "END" {
        g.src(top)[0]
    } else {
        top
    };
    let ranges: Vec<Id> = env_ranges
        .iter()
        .flat_map(|r| {
            digits
                .iter()
                .find(|(x, _)| x == r)
                .map(|(_, d)| d.clone())
                .unwrap_or(vec![*r])
        })
        .collect();
    let (idx, value) = (g.src(store)[0], g.src(store)[1]);
    let out_param = param_of(&g, idx);
    if g.op(idx) != "INDEX" || out_param.is_none() {
        return refuse("TG.PLAN.012", "the store is not into a buffer");
    }
    let oa = if g.src(idx).len() > 1 {
        affine(&g, g.src(idx)[1])
    } else {
        Some((Vec::new(), 0))
    };
    let Some(oa) = oa.filter(|o| o.1 == 0) else {
        return refuse(
            "TG.PLAN.012",
            "the store's index is not one dense walk of the loop nest",
        );
    };
    let out_ranges = order(&g, &oa.0, &ranges)?;
    let (out_slot, out_dtype) = param_slot(&g, out_param.unwrap());
    let shape: Vec<i64> = out_ranges.iter().map(|r| extent(&g, *r)).collect();
    let mut base = Plan {
        kind: String::new(),
        name: kernel_name(&g, ast),
        out_slot: Slot::Param(out_slot),
        out_dtype,
        out_shape: shape.clone(),
        out_ranges: out_ranges.clone(),
        expr: None,
        accesses: Vec::new(),
        reduce_ranges: Vec::new(),
        reductions: Vec::new(),
        value: None,
        a: None,
        b: None,
        mnk: None,
        batch: Vec::new(),
        out_strides: None,
    };
    let mut imemo = HashMap::new();
    if fold::index_only(&g, value, &mut imemo) {
        let mut memo = HashMap::new();
        let v = fold::evaluate(&g, value, &out_ranges, &mut memo)
            .map_err(|e| Refusal::new("TG.PLAN.004", e.0))?;
        let dims: Vec<usize> = shape.iter().map(|d| *d as usize).collect();
        let ty = Ty::of(&base.out_dtype).ok_or_else(|| {
            Refusal::new(
                "TG.PLAN.004",
                format!("no numpy type for {}", base.out_dtype),
            )
        })?;
        base.kind = "const".into();
        base.value = Some(v.broadcast(&dims).cast(ty));
        return Ok(Lowered {
            graph: g,
            plan: base,
        });
    }
    let mut reader = Reader {
        out_ranges: out_ranges.clone(),
        accesses: Vec::new(),
        reduce_ranges: Vec::new(),
        reductions: Vec::new(),
        memo: HashMap::new(),
        imemo,
    };
    let e = reader.read(&mut g, value)?;
    base.accesses = reader.accesses.clone();
    base.expr = Some(e.clone());
    base.reduce_ranges = reader.reduce_ranges.clone();
    base.reductions = reader.reductions.clone();
    let sets: Vec<&Vec<Id>> =
        reader
            .reductions
            .iter()
            .map(|(_, rs)| rs)
            .fold(Vec::new(), |mut acc, rs| {
                if !acc.contains(&rs) {
                    acc.push(rs);
                }
                acc
            });
    if sets.len() <= 1 && !mutant::on("tg.lower/no_matmul") {
        if let Some(mm) = matmul(&mut g, &base, value, &mut reader)? {
            return Ok(Lowered { graph: g, plan: mm });
        }
    }
    if let Expr::Load(i) = e.as_ref() {
        if reader.reduce_ranges.is_empty() {
            let acc = &reader.accesses[*i];
            let same = acc.coef.len() == oa.0.len() && acc.coef.iter().all(|x| oa.0.contains(x));
            if acc.slot != Slot::Table && same && acc.off == 0 && acc.dtype == base.out_dtype {
                base.kind = "copy".into();
                return Ok(Lowered {
                    graph: g,
                    plan: base,
                });
            }
        }
    }
    base.kind = "vector".into();
    Ok(Lowered {
        graph: g,
        plan: base,
    })
}
