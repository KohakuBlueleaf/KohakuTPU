//! Fixed node loops over packed GEMM tiles and K chunks.

use ktpu_l1::text::{Env, Term};

use crate::ir::{const_of, value, Body, Buffer, Op, Result};
use crate::lower::memory;
use crate::lower::sync::{mem, Stage};
use crate::target::Target;

pub fn lower(body: &Body, op: &Op, env: &Env, target: &Target, tag: usize) -> Result<Stage> {
    let st = &op.st;
    let fail = |message: &str| body.reader.fail(st, "L2.MG.016", message);
    let get = |term: &Term| -> Result<(String, &Buffer)> {
        let term = value(body, st, term, env)?;
        let name = match &term {
            Term::Name(name) => name,
            Term::View(name, slices) if slices.is_empty() => name,
            _ => return Err(fail("gemm_tiles takes whole buffers")),
        };
        Ok((
            name.clone(),
            body.buffers
                .get(name)
                .ok_or_else(|| fail("unknown gemm_tiles buffer"))?,
        ))
    };
    let args = st.positional();
    if args.len() != 3 {
        return Err(fail("gemm_tiles DST <- LHS rhs=RHS"));
    }
    let ((dn, dst), (an, a)) = (get(args[0])?, get(args[2])?);
    let (bn, b) = get(st.kw("rhs").ok_or_else(|| fail("gemm_tiles needs rhs"))?)?;
    let integer = |key: &str| {
        const_of(
            body,
            st,
            st.kw(key)
                .ok_or_else(|| fail("missing gemm_tiles dimension"))?,
            env,
        )
    };
    let (bt, rows, cols, cores, shared) = (
        integer("batches")?,
        integer("rows")?,
        integer("cols")?,
        integer("clusters")?,
        integer("shared")?,
    );
    if [bt, rows, cols, cores].iter().any(|v| *v <= 0) || cores as usize > target.mg.len() {
        return Err(fail("invalid gemm_tiles dimensions or cluster count"));
    }
    let (gm, gn) = dst
        .tile()
        .ok_or_else(|| fail("gemm_tiles destination needs tile layout"))?;
    let nk = a.get("nk");
    if a.dtype != "mx7a"
        || b.dtype != "mx7b"
        || dst.dtype != "f16"
        || gm <= 0
        || gn <= 0
        || nk <= 0
        || b.get("nk") != nk
        || a.get("gm") != gm
        || b.get("gn") != gn
        || a.shape.len() != 2
        || b.shape.len() != 2
        || dst.shape.len() != 2
    {
        return Err(fail("gemm_tiles buffer packing differs from its tile"));
    }
    let (tm, tn, kc) = (
        gm * target.mg_lanes,
        gn * target.mg_lanes,
        nk * target.kblock,
    );
    let k = a.shape[1];
    if rows % tm != 0
        || cols % tn != 0
        || k <= 0
        || k % kc != 0
        || gm * nk > target.bank
        || gn * nk > target.bank
        || gm * gn > target.tiles
    {
        return Err(fail(
            "gemm_tiles requires whole packed tiles/chunks within bank capacities",
        ));
    }
    let ntm = rows / tm;
    let ntn = cols / tn;
    let chunks = k / kc;
    let product = |x: i64, y: i64| {
        x.checked_mul(y)
            .ok_or_else(|| fail("gemm_tiles dimension overflow"))
    };
    let tiles_batch = product(ntm, ntn)?;
    let tiles = product(bt, tiles_batch)?;
    if a.shape != [product(bt, rows)?, k]
        || b.shape
            != [
                if shared != 0 {
                    cols
                } else {
                    product(bt, cols)?
                },
                k,
            ]
        || dst.shape != [product(tiles, tm)?, tn]
    {
        return Err(fail("gemm_tiles buffer shapes do not match counts"));
    }
    let (ab, bb, db) = (
        gm * nk * memory::entry(target),
        gn * nk * memory::entry(target),
        gm * gn * target.word_bytes,
    );
    let mut lines = Vec::new();
    let mut units = Vec::new();
    let mut accesses = Vec::new();
    for core in 0..cores {
        let unit = format!("mg[{core}]");
        let tile_label = format!("tiles_{tag}_{core}");
        let k_label = format!("chunks_{tag}_{core}");
        let end = format!("done_{tag}_{core}");
        lines.extend([
            format!("r0 = set {core}"),
            "r8 = set 0".into(),
            "r9 = set 0".into(),
            "r13 = set 0".into(),
            format!("label {tile_label}"),
            format!("goto {end} if r0 >= {tiles}"),
            format!("r1 = divu r0, {tiles_batch}"),
            format!("r2 = divu r0, {ntn}"),
            format!("r3 = remu r0, {ntn}"),
            "r4 = set 0".into(),
            "r5 = set 0".into(),
            format!("label {k_label}"),
            format!("r6 = mul r2, {}", chunks * ab),
            format!("r10 = mul r4, {ab}"),
            "r6 = add r6, r10".into(),
        ]);
        if shared != 0 {
            lines.push("r7 = set r3".into());
        } else {
            lines.extend([format!("r7 = mul r1, {ntn}"), "r7 = add r7, r3".into()]);
        }
        lines.extend([
            format!("r7 = mul r7, {}", chunks * bb),
            format!("r11 = mul r4, {bb}"),
            "r7 = add r7, r11".into(),
            "r10 = sub r6, r8".into(),
            "r11 = sub r7, r9".into(),
            "r8 = set r6".into(),
            "r9 = set r7".into(),
            format!("send {unit} advance=1"),
            format!("    fill A0 <- {} + r10 n={}", an, gm * nk),
            format!("    fill B0 <- {} + r11 n={}", bn, gn * nk),
            format!("    gemm {gm}x{gn}x{nk} a=A0 b=B0 acc=r5"),
            format!("    post n=0 to={unit}"),
            format!("wait {unit}"),
            "r5 = set 1".into(),
            "r4 = add r4, 1".into(),
            format!("goto {k_label} if r4 < {chunks}"),
            format!("r12 = mul r0, {db}"),
            "r14 = sub r12, r13".into(),
            "r13 = set r12".into(),
            format!("send {unit} advance=1"),
            format!("    drain {} + r14 n={}", dn, gm * gn),
            format!("wait {unit}"),
            format!("r0 = add r0, {cores}"),
            format!("goto {tile_label}"),
            format!("label {end}"),
        ]);
        units.push(unit.clone());
        for (write, name, buffer) in [(false, &an, a), (false, &bn, b), (true, &dn, dst)] {
            accesses.push(mem(&unit, write, name, 0, buffer.nbytes(target)));
        }
    }
    if crate::mutant::on("l2.cluster/no-idle-post") {
        lines.retain(|line| !line.trim_start().starts_with("post n=0"));
    }
    Ok(Stage::new(lines, units, accesses))
}
