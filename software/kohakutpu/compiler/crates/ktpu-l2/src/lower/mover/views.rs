//! Views as word walks: a view's words, an index list, a copy's two walks,
//! and a gather's or scatter's rows (L2.MOV.002, .015).

use super::*;

/// `(buffer, byte offset, levels)` of a view as 32-byte words.
pub fn words(
    body: &Body,
    st: &Stmt,
    t: &Term,
    env: &Env,
    tg: &Target,
) -> Result<(String, i64, Levels)> {
    let v = value(body, st, &as_view(t), env)?;
    let name = view_name(&v);
    let buf = buffer(body, st, &name, "buffer ")?;
    let walked = M::axes_of(&v, &buf.shape).and_then(|ax| M::rowmajor_walk(buf, &ax));
    let (off, mut dims, item) = walked.map_err(|e| fail(body, st, "L2.MOV.002", e))?;
    let word = tg.word_bytes;
    if dims.last().map(|d| d.1 != item).unwrap_or(true) {
        dims.push((1, item));
    }
    let (n, s) = *dims.last().unwrap();
    if s != item || (n * item) % word != 0 || off % word != 0 {
        return Err(fail(
            body,
            st,
            "L2.MOV.015",
            format!("{name}: the innermost axis is whole 32-byte words"),
        ));
    }
    let mut levels: Levels = dims[..dims.len() - 1].to_vec();
    levels.push((n * item / word, word));
    Ok((name, off, levels))
}

pub(super) fn index_list(
    body: &Body,
    st: &Stmt,
    t: &Term,
    env: &Env,
    tg: &Target,
) -> Result<(String, i64, i64)> {
    let v = value(body, st, &as_view(t), env)?;
    let name = view_name(&v);
    let buf = buffer(body, st, &name, "index buffer ")?;
    if matches!(&v, Term::View(_, s) if s.is_empty()) {
        return Ok((name, 0, buf.nbytes(tg) / 4));
    }
    let (n, off, levels) = words(body, st, &v, env, tg)?;
    let run = merge(&squeeze(&levels));
    if run.len() != 1 || run[0].1 != tg.word_bytes {
        return Err(fail(
            body,
            st,
            "L2.MOV.015",
            format!("{name}: an index list is one contiguous run of words"),
        ));
    }
    Ok((n, off, run[0].0 * tg.word_bytes / 4))
}

type Walk = (String, i64, Levels);

/// `(destination walk, source walk)` of a `copy`, the source's levels in the
/// order the destination's steps take them.
pub fn copy_walks(body: &Body, st: &Stmt, env: &Env, tg: &Target) -> Result<(Walk, Walk)> {
    let pos = st.positional();
    if pos.len() != 3 || !pos[1].is_arrow("<-") {
        return Err(fail(body, st, "L2.MOV.015", "copy DST <- SRC ..."));
    }
    let (dst, src) = (pos[0], pos[2]);
    let word = tg.word_bytes;
    let (dn, mut doff, mut dl) = words(body, st, dst, env, tg)?;
    let (sn, mut soff, mut sl) = words(body, st, src, env, tg)?;
    if let Some(w) = st.kw("walk") {
        if !matches!(src, Term::Name(_)) || st.kw("perm").is_some() {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                "copy DST <- BUFFER walk=((COUNT, STRIDE), ...) [at=BYTES]",
            ));
        }
        sl = walk_levels(body, st, w, env)?;
        soff = match st.kw("at") {
            Some(a) => const_of(body, st, a, env)?,
            None => 0,
        };
        if soff % word != 0 || sl.iter().any(|(_, s)| s % word != 0) {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                "a walk steps whole 32-byte words",
            ));
        }
    }
    if let Some(w) = st.kw("dwalk") {
        if !matches!(dst, Term::Name(_)) {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                "copy BUFFER <- SRC dwalk=((COUNT, STRIDE), ...) [dat=BYTES]",
            ));
        }
        dl = walk_levels(body, st, w, env)?;
        doff = match st.kw("dat") {
            Some(a) => const_of(body, st, a, env)?,
            None => 0,
        };
        if doff % word != 0 || dl.iter().any(|(_, s)| s % word != 0) {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                "a walk steps whole 32-byte words",
            ));
        }
    }
    if let Some(p) = st.kw("perm") {
        let order: Option<Vec<i64>> = match tuple(p) {
            Some(items) => items.iter().map(|i| i.as_int().map(|v| v as i64)).collect(),
            None => None,
        };
        let ok = order
            .as_ref()
            .map(|o| {
                let mut s = o.clone();
                s.sort();
                s == (0..sl.len() as i64).collect::<Vec<_>>()
            })
            .unwrap_or(false);
        if !ok {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!("perm= orders all {} word axes of the source", sl.len()),
            ));
        }
        sl = order.unwrap().iter().map(|&i| sl[i as usize]).collect();
    }
    if count(&dl) != count(&sl) {
        return Err(fail(
            body,
            st,
            "L2.MOV.015",
            format!("copy of {} words into {}", count(&sl), count(&dl)),
        ));
    }
    Ok(((dn, doff, dl), (sn, soff, sl)))
}

/// `(buffer, byte offset, rows, words a row, levels)` of a gather's
/// destination or a scatter's source.
pub fn rows_walk(
    body: &Body,
    st: &Stmt,
    env: &Env,
    tg: &Target,
) -> Result<(String, i64, i64, i64, Levels)> {
    let pos: Vec<&Term> = st
        .positional()
        .into_iter()
        .filter(|p| p.as_name() != Some("fp"))
        .collect();
    if pos.len() != 3 || !pos[1].is_arrow("<-") {
        return Err(fail(
            body,
            st,
            "L2.MOV.015",
            format!("{} DST <- SRC ...", st.op),
        ));
    }
    let side = if st.op == "gather" { pos[0] } else { pos[2] };
    let (name, off, rl) = words(body, st, side, env, tg)?;
    let n = if rl.len() > 1 { rl[0].0 } else { 1 };
    Ok((name, off, n, count(&rl) / n, rl))
}
