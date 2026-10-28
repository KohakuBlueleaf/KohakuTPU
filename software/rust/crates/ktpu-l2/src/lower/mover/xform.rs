//! Conversions: `quant` (f16 to mx7a / mx7b entries) and `gt4` (f16 to a
//! tiled f16 layout), by view or by walks (L2.MOV.010, .017).

use super::*;

pub(super) fn gt4_walks(
    body: &Body,
    op: &Op,
    dst: &Term,
    src: &Term,
    env: &Env,
    tg: &Target,
) -> Result<(Vec<String>, Vec<Access>)> {
    let st = &op.st;
    let word = tg.word_bytes;
    let (Term::Name(dn), Term::Name(sn), Some(dw)) = (dst, src, st.kw("dwalk")) else {
        return Err(fail(
            body,
            st,
            "L2.MOV.017",
            "gt4 BUFFER <- BUFFER walk=((COUNT, STRIDE), ..) dwalk=(..) [at= dat=]",
        ));
    };
    let sl = walk_levels(body, st, st.kw("walk").unwrap(), env)?;
    let dl = walk_levels(body, st, dw, env)?;
    let soff = match st.kw("at") {
        Some(a) => const_of(body, st, a, env)?,
        None => 0,
    };
    let doff = match st.kw("dat") {
        Some(a) => const_of(body, st, a, env)?,
        None => 0,
    };
    let counts = |l: &[(i64, i64)]| l.iter().map(|x| x.0).collect::<Vec<_>>();
    if sl.last().unwrap().0 != 4
        || dl.len() + 1 != sl.len()
        || counts(&dl) != counts(&sl[..sl.len() - 1])
    {
        return Err(fail(
            body,
            st,
            "L2.MOV.017",
            "gt4 walks: the source's innermost level is the 4 words of a group, the rest match the destination's",
        ));
    }
    if soff % word != 0 || doff % word != 0 || sl.iter().chain(&dl).any(|(_, s)| s % word != 0) {
        return Err(fail(
            body,
            st,
            "L2.MOV.015",
            "a walk steps whole 32-byte words",
        ));
    }
    let inner = *sl.last().unwrap();
    if let Some(tag) = st.kw("class_tag") {
        if dl.len() > 8 && !on("l2.mover/unchecked-class-depth") {
            return Err(fail(
                body,
                st,
                "L2.MOV.017",
                "class GT4 supports at most eight entry axes",
            ));
        }
        let line = format!(
            "    gt4 w({sn} + {soff} + r10, ({}, {})) -> w({dn} + {doff} + r11, (1, 0))",
            inner.0, inner.1
        );
        return Ok((
            class_walk_loop(
                &sl[..sl.len() - 1],
                &dl,
                line,
                const_of(body, st, tag, env)? as usize,
            ),
            vec![
                mem_of(false, span_of(sn, soff, &sl, word)),
                mem_of(true, span_of(dn, doff, &dl, 4 * word)),
            ],
        ));
    }
    let lines = cut(
        &[sl[..sl.len() - 1].to_vec(), dl.clone()],
        MOVE_WORDS / inner.0,
        Some(&[vec![inner], vec![]]),
        0,
        tg,
    )
    .into_iter()
    .map(|(o, l)| {
        let mut s_lv = l[0].clone();
        s_lv.push(inner);
        format!(
            "gt4 {} -> {}",
            walk_text(sn, soff + o[0], &s_lv),
            walk_text(dn, doff + o[1], &l[1])
        )
    })
    .collect();
    Ok((
        lines,
        vec![
            mem_of(false, span_of(sn, soff, &sl, word)),
            mem_of(true, span_of(dn, doff, &dl, 4 * word)),
        ],
    ))
}

pub(super) fn convert(
    body: &Body,
    op: &Op,
    dst: &Term,
    src: &Term,
    env: &Env,
    tg: &Target,
) -> Result<(Vec<String>, Vec<Access>)> {
    let st = &op.st;
    let dv = value(body, st, &as_view(dst), env)?;
    let sv = value(body, st, &as_view(src), env)?;
    let (dname, sname) = (view_name(&dv), view_name(&sv));
    let dbuf = body.buffers.get(&dname);
    let sbuf = body.buffers.get(&sname);
    let (Some(dbuf), Some(sbuf)) = (dbuf, sbuf) else {
        let missing = if dbuf.is_none() { &dname } else { &sname };
        return Err(fail(body, st, "L2.MOV.015", format!("no buffer {missing}")));
    };
    let vfail = |e: String| fail(body, st, "L2.MOV.010", e);
    let dax = M::axes_of(&dv, &dbuf.shape).map_err(vfail)?;
    let sax = M::axes_of(&sv, &sbuf.shape).map_err(vfail)?;
    let (ds, ss) = (M::shape_of(&dax), M::shape_of(&sax));
    if ds != ss || ss.len() != 2 {
        return Err(fail(
            body,
            st,
            "L2.MOV.010",
            format!(
                "{}: the views are {} and {}",
                op.op,
                py_tuple(&ds),
                py_tuple(&ss)
            ),
        ));
    }
    if sbuf.dtype != "f16" {
        return Err(vfail(format!(
            "{} reads a row-major f16 buffer, not {}",
            op.op, sbuf.dtype
        )));
    }
    let soff = M::rowmajor_walk(sbuf, &sax).map_err(vfail)?.0;
    let pitch = sbuf.shape.last().unwrap() * sbuf.item();
    let ((dbase, slv, dlv), flag) = if op.op == "quant" {
        quant(dbuf, &dax, pitch, tg)
    } else {
        gt4(dbuf, &dax, pitch, tg)
    }
    .map_err(vfail)?;
    let per_entry = count(&slv) / count(&dlv);
    let mut k = slv.len();
    while count(&slv[k..]) < per_entry {
        k -= 1;
    }
    if count(&slv[k..]) != per_entry {
        return Err(fail(
            body,
            st,
            "L2.MOV.010",
            format!("{} entry walk {:?} / {:?}", op.op, slv, dlv),
        ));
    }
    let tail = slv[k..].to_vec();
    if let Some(tag) = st.kw("class_tag") {
        if k != dlv.len()
            || k > 8
            || slv[..k]
                .iter()
                .zip(&dlv)
                .any(|(a, b)| a.0 != b.0 || a.0 <= 0)
        {
            return Err(vfail(
                "class transform requires matched positive entry axes".into(),
            ));
        }
        let axes = tail
            .iter()
            .map(|(n, s)| format!(", ({n}, {s})"))
            .collect::<String>();
        let inner = format!(
            "    {} w({sname} + {soff} + r10{axes}) -> w({dname} + {dbase} + r11, (1, 0)){flag}",
            op.op
        );
        let per = if op.op == "gt4" {
            4 * tg.word_bytes
        } else {
            M::entry(tg)
        };
        return Ok((
            class_walk_loop(
                &slv[..k],
                &dlv,
                inner,
                const_of(body, st, tag, env)? as usize,
            ),
            vec![
                mem_of(false, span_of(&sname, soff, &slv, tg.word_bytes)),
                mem_of(true, span_of(&dname, dbase, &dlv, per)),
            ],
        ));
    }
    let lines = cut(
        &[slv[..k].to_vec(), dlv.clone()],
        MOVE_WORDS / per_entry,
        Some(&[tail.clone(), vec![]]),
        0,
        tg,
    )
    .into_iter()
    .map(|(o, l)| {
        let mut s_lv = l[0].clone();
        s_lv.extend(tail.iter().copied());
        format!(
            "{} {} -> {}{flag}",
            op.op,
            walk_text(&sname, soff + o[0], &s_lv),
            walk_text(&dname, dbase + o[1], &l[1])
        )
    })
    .collect();
    let word = tg.word_bytes;
    let per = if op.op == "gt4" {
        4 * word
    } else {
        M::entry(tg)
    };
    Ok((
        lines,
        vec![
            mem_of(false, span_of(&sname, soff, &slv, word)),
            mem_of(true, span_of(&dname, dbase, &dlv, per)),
        ],
    ))
}

/// Python's `str(tuple)` of ints.
pub fn py_tuple(v: &[i64]) -> String {
    match v.len() {
        1 => format!("({},)", v[0]),
        _ => format!(
            "({})",
            v.iter()
                .map(|x| x.to_string())
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// `(first block, blocks, first unit in the block, units in each)`: a range
/// inside one block, or whole blocks.
fn blocks(lo: i64, size: i64, block: i64, what: &str) -> M::ViewResult<(i64, i64, i64, i64)> {
    let (b0, r0) = (lo / block, lo % block);
    if r0 + size <= block {
        return Ok((b0, 1, r0, size));
    }
    if r0 != 0 || size % block != 0 {
        return Err(format!(
            "{what}: [{lo}, {}) is inside one {block} or whole ones",
            lo + size
        ));
    }
    Ok((b0, size / block, 0, block))
}

type Conv = ((i64, Levels, Levels), &'static str);

pub(super) fn quant(buf: &Buffer, ax: &[M::Ax], pitch: i64, tg: &Target) -> M::ViewResult<Conv> {
    if !buf.is_mx7() || buf.flag("t") {
        return Err(format!(
            "quant writes an mx7a / mx7b buffer, not {}",
            buf.dtype
        ));
    }
    let g = buf.get(if buf.dtype == "mx7a" { "gm" } else { "gn" });
    let nk = buf.get("nk");
    let (base, two) = M::slab(buf, ax, tg)?;
    let ((r0, rows), (k0, cols)) = (two[0], two[1]);
    let xf = &tg.machine.mover.xform;
    // A quantiser entry: `l` rows of `v` FP16 values (`vb` bytes a row).
    let (l, v) = (xf.quant_lanes as i64, xf.quant_values as i64);
    let vb = 2 * v;
    if r0 % l != 0 || rows % l != 0 || k0 % v != 0 || cols % v != 0 {
        return Err(format!(
            "quant converts whole groups of {l} rows by {v} values"
        ));
    }
    let (t0, tn, g0, gs) = blocks(r0 / l, rows / l, g, "quant rows (groups)")?;
    let (c0, cn, b0, bs) = blocks(k0 / v, cols / v, nk, "quant K (blocks)")?;
    let chunks = buf.shape.last().unwrap() / (v * nk);
    let first = ((t0 * chunks + c0) * g + g0) * nk + b0;
    let (word, e) = (tg.word_bytes, M::entry(tg));
    let src = vec![
        (tn, l * g * pitch),
        (cn, nk * vb),
        (gs, l * pitch),
        (bs, vb),
        (l, pitch),
        (vb / word, word),
    ];
    let dst = vec![
        (tn, chunks * g * nk * e),
        (cn, g * nk * e),
        (gs, nk * e),
        (bs, e),
    ];
    let flag = if buf.dtype == "mx7b" { " bpack" } else { "" };
    Ok(((base + first * e, src, dst), flag))
}

fn gt4(buf: &Buffer, ax: &[M::Ax], pitch: i64, tg: &Target) -> M::ViewResult<Conv> {
    if buf.dtype != "f16" || !buf.has("tile") {
        return Err("gt4 writes an f16 buffer with a tile= layout".into());
    }
    let (gm, gn) = buf.tile().unwrap();
    let xf = &tg.machine.mover.xform;
    // A GT4 group: `r` rows of `c` columns, `q` sub-tiles of `r` columns
    // wide; `rb` bytes a sub-tile row.
    let (r, c) = (xf.gt4_rows as i64, xf.gt4_cols as i64);
    let (q, rb) = (c / r, xf.gt4_granule_bits as i64 / 8);
    if gn % q != 0 {
        return Err(format!(
            "gt4: a tile {gn} sub-tiles wide is not whole groups of {q}"
        ));
    }
    let (base, two) = M::slab(buf, ax, tg)?;
    let ((r0, rows), (c0, cols)) = (two[0], two[1]);
    if r0 % r != 0 || rows % r != 0 || c0 % c != 0 || cols % c != 0 {
        return Err(format!(
            "gt4 converts whole groups of {r} rows by {c} columns"
        ));
    }
    let (ti, tin, bi, bs) = blocks(r0 / r, rows / r, gm, "gt4 rows (bands)")?;
    let (tj, tjn, qi, qs) = blocks(c0 / c, cols / c, gn / q, "gt4 columns (groups)")?;
    let tn = buf.shape.last().unwrap() / (r * gn);
    let sub = M::subtile(tg);
    let first = M::subtile_address(buf, ti * gm + bi, tj * gn + q * qi, tg);
    let word = tg.word_bytes;
    let src = vec![
        (tin, r * gm * pitch),
        (bs, r * pitch),
        (tjn, rb * gn),
        (qs, word),
        (r, pitch),
    ];
    let dst = vec![
        (tin, tn * gm * gn * sub),
        (bs, gn * sub),
        (tjn, gm * gn * sub),
        (qs, q * sub),
    ];
    Ok(((base + first, src, dst), ""))
}
