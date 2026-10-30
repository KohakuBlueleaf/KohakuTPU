//! Node-scope mover ops to L1 `move` statements (L2.MOV.001-.017).
//!
//! A view is a walk of 32-byte words, outermost level first; the
//! destination's walk is the iteration order (a scatter's, its source's; a
//! transform's steps once per converted entry).

mod packing;
pub mod quant_status;
mod views;
mod walk;
mod xform;

pub use packing::{quant_backend, vector_quant, Finite};

use ktpu_l1::text::{Env, Stmt, Term};

use super::memory as M;
use super::sync::{mem, Access};
use crate::ir::repr::repr;
use crate::ir::{value, Body, Buffer, Op, Result};
use crate::mutant::on;
use crate::target::{Target, MOVE_WORDS};

use views::index_list;
pub use views::{copy_walks, rows_walk, words};
use walk::{count, span_of, walk_levels, walk_text};
pub use walk::{cut, merge, run_words, squeeze};
pub use xform::py_tuple;
use xform::{convert, gt4_walks};

pub const OPS: [&str; 7] = ["copy", "fill", "gen", "gather", "scatter", "quant", "gt4"];

/// The node-scope ops (L2.MOV.001).
pub fn ops() -> Vec<&'static str> {
    OPS.iter()
        .copied()
        .filter(|o| *o != "fill" || !on("l2.mover/no-fill"))
        .collect()
}

fn const_of(body: &Body, st: &Stmt, t: &Term, env: &Env) -> Result<i64> {
    if on("l2.mover/const-zero") {
        return Ok(0);
    }
    crate::ir::const_of(body, st, t, env)
}

fn tuple(t: &Term) -> Option<&Vec<Term>> {
    match t {
        Term::Tuple(items) if !on("l2.mover/tuples-unread") => Some(items),
        _ => None,
    }
}
pub const UNIT: &str = "mover";

/// `[(count, stride)]`, outermost first.
pub type Levels = Vec<(i64, i64)>;

fn fail(body: &Body, st: &Stmt, clause: &'static str, msg: impl Into<String>) -> ktpu_l1::Error {
    body.reader.fail(st, clause, msg)
}

fn as_view(t: &Term) -> Term {
    match t {
        Term::Name(n) => Term::View(n.clone(), vec![]),
        other => other.clone(),
    }
}

fn view_name(t: &Term) -> String {
    match t {
        Term::View(n, _) | Term::Name(n) => n.clone(),
        other => repr(other),
    }
}

fn buffer<'a>(body: &'a Body, st: &Stmt, name: &str, what: &str) -> Result<&'a Buffer> {
    body.buffers
        .get(name)
        .ok_or_else(|| fail(body, st, "L2.MOV.015", format!("no {what}{name}")))
}

fn mem_of(write: bool, sp: (String, i64, i64)) -> Access {
    mem(UNIT, write, &sp.0, sp.1, sp.2)
}

fn int_kw(body: &Body, st: &Stmt, t: &Term) -> Result<i128> {
    match t {
        Term::Int(v, _) => Ok(*v),
        _ => Err(fail(
            body,
            st,
            "L2.MOV.015",
            format!("wanted a constant, got {}", repr(t)),
        )),
    }
}

/// A class walk repeats bounded inner moves using node registers.
pub fn class_copy_loop(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    tag: usize,
) -> Result<(Vec<String>, Vec<Access>)> {
    let st = &op.st;
    let ((dn, doff, dl), (sn, soff, sl)) = copy_walks(body, st, env, tg)?;
    if !on("l2.mover/word-streams") {
        let mut bulk = op.clone();
        bulk.st
            .args
            .retain(|arg| !matches!(arg, Term::Assign(k, _) if k.as_name() == Some("classed")));
        let (copies, accesses) = lower_op(body, &bulk, env, tg)?;
        let mut lines = vec!["move".into()];
        lines.extend(copies.iter().map(|copy| format!("    {copy}")));
        return Ok((lines, accesses));
    }
    if st.kw("node_loop").and_then(Term::as_int) == Some(2) {
        if sl.is_empty()
            || dl.is_empty()
            || sl.iter().chain(&dl).any(|x| x.0 <= 0)
            || count(&sl) != count(&dl)
        {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                "linear class copy requires equal positive word counts",
            ));
        }
        let mut lines = vec![
            "r0 = set 0".into(),
            "r8 = set 0".into(),
            "r9 = set 0".into(),
            format!("label linear_move_{tag}"),
        ];
        for (levels, result, old, delta) in [(&sl, 6, 8, 10), (&dl, 7, 9, 11)] {
            lines.push(format!("r{result} = set 0"));
            for axis in 0..levels.len() {
                let divisor = count(&levels[axis + 1..]);
                lines.extend([
                    format!("r1 = divu r0, {divisor}"),
                    format!("r1 = remu r1, {}", levels[axis].0),
                    format!("r1 = mul r1, {}", levels[axis].1),
                    format!("r{result} = add r{result}, r1"),
                ]);
            }
            lines.extend([
                format!("r{delta} = sub r{result}, r{old}"),
                format!("r{old} = set r{result}"),
            ]);
        }
        lines.extend([
            "move advance=1".into(),
            format!("    copy w({sn} + {soff} + r10, (1, 0)) -> w({dn} + {doff} + r11, (1, 0))"),
            "r0 = add r0, 1".into(),
            format!("goto linear_move_{tag} if r0 < {}", count(&sl)),
        ]);
        return Ok((
            lines,
            vec![
                mem_of(false, span_of(&sn, soff, &sl, tg.word_bytes)),
                mem_of(true, span_of(&dn, doff, &dl, tg.word_bytes)),
            ],
        ));
    }
    if sl.is_empty()
        || sl.len() != dl.len()
        || sl.len() > 8
        || sl.iter().zip(&dl).any(|(s, d)| s.0 != d.0 || s.0 <= 0)
        || sl.last().unwrap().0 > MOVE_WORDS
    {
        return Err(fail(
            body,
            st,
            "L2.MOV.015",
            "class loop needs matched positive walk axes and a bounded inner move",
        ));
    }
    let loops = sl.len() - 1;
    let (sc, ss) = sl[loops];
    let (dc, ds) = dl[loops];
    let inner = format!(
        "    copy w({sn} + {soff} + r10, ({sc}, {ss})) -> w({dn} + {doff} + r11, ({dc}, {ds}))"
    );
    Ok((
        class_walk_loop(&sl[..loops], &dl[..loops], inner, tag),
        vec![
            mem_of(false, span_of(&sn, soff, &sl, tg.word_bytes)),
            mem_of(true, span_of(&dn, doff, &dl, tg.word_bytes)),
        ],
    ))
}

pub(super) fn class_walk_loop(
    sl: &[(i64, i64)],
    dl: &[(i64, i64)],
    inner: String,
    tag: usize,
) -> Vec<String> {
    let mut lines = Vec::new();
    let loops = sl.len();
    lines.push("r10 = set 0".into());
    lines.push("r11 = set 0".into());
    for axis in 0..loops {
        lines.push(format!("r{axis} = set 0"));
        lines.push(format!("label class_move_{tag}_{axis}"));
    }
    lines.push("move advance=1".into());
    lines.push(inner);
    for axis in (0..loops).rev() {
        lines.push(format!("r{axis} = add r{axis}, 1"));
        lines.push(format!("r10 = set {}", sl[axis].1));
        lines.push(format!("r11 = set {}", dl[axis].1));
        for inner in axis + 1..loops {
            lines.push(format!("r12 = sub r{inner}, 1"));
            lines.push(format!("r13 = mul r12, {}", sl[inner].1));
            lines.push("r10 = sub r10, r13".into());
            lines.push(format!("r13 = mul r12, {}", dl[inner].1));
            lines.push("r11 = sub r11, r13".into());
        }
        lines.push(format!(
            "goto class_move_{tag}_{axis} if r{axis} < {}",
            sl[axis].0
        ));
    }
    lines
}

pub fn class_convert_loop(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    tag: usize,
) -> Result<(Vec<String>, Vec<Access>)> {
    let mut with_tag = op.clone();
    with_tag.st.args.push(Term::Assign(
        Box::new(Term::name("class_tag")),
        Box::new(Term::int(tag as i128)),
    ));
    lower_op(body, &with_tag, env, tg)
}

/// `(L1 mover lines, accesses)` of one node-scope op.
pub fn lower(body: &Body, op: &Op, env: &Env, tg: &Target) -> Result<(Vec<String>, Vec<Access>)> {
    let (mut lines, acc) = lower_op(body, op, env, tg)?;
    if on("l2.mover/pitch-doubled") {
        for ln in lines.iter_mut() {
            if let Some(i) = ln.find("pitch=") {
                let s = i + 6;
                let e = s + ln[s..].bytes().take_while(u8::is_ascii_digit).count();
                let v: i64 = ln[s..e].parse().unwrap_or(0);
                *ln = format!("{}{}{}", &ln[..s], 2 * v, &ln[e..]);
            }
        }
    }
    Ok((lines, acc))
}

fn lower_op(body: &Body, op: &Op, env: &Env, tg: &Target) -> Result<(Vec<String>, Vec<Access>)> {
    let st = &op.st;
    let pos: Vec<&Term> = st.positional();
    let kw = |k: &str| st.kw(k);
    let word = tg.word_bytes;
    if op.op == "fill" || op.op == "gen" {
        if pos.len() != 1 {
            let tail = if op.op == "fill" { "value=V" } else { "seed=S" };
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!("{} DST {tail}", op.op),
            ));
        }
        let (name, mut off, mut lv) = words(body, st, pos[0], env, tg)?;
        if let Some(w) = kw("walk") {
            if !matches!(pos[0], Term::Name(_)) {
                return Err(fail(
                    body,
                    st,
                    "L2.MOV.015",
                    format!("{} BUFFER walk=((COUNT, STRIDE), ...) [at=BYTES]", op.op),
                ));
            }
            lv = squeeze(&walk_levels(body, st, w, env)?);
            off = match kw("at") {
                Some(a) => const_of(body, st, a, env)?,
                None => 0,
            };
            if off % word != 0 || lv.iter().any(|(_, s)| s % word != 0) {
                return Err(fail(
                    body,
                    st,
                    "L2.MOV.015",
                    "a walk steps whole 32-byte words",
                ));
            }
        }
        let key = if op.op == "fill" { "value" } else { "seed" };
        let Some(kv) = kw(key) else {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!("{} needs {key}=", op.op),
            ));
        };
        let extra = match kw("ew") {
            Some(e) => format!(" ew={}", int_kw(body, st, e)?),
            None => String::new(),
        };
        let tail = format!("{key}={}{extra}", int_kw(body, st, kv)?);
        let lines = cut(&[lv.clone()], MOVE_WORDS, None, 0, tg)
            .into_iter()
            .map(|(o, l)| format!("{} {} {tail}", op.op, walk_text(&name, off + o[0], &l[0])))
            .collect();
        return Ok((lines, vec![mem_of(true, span_of(&name, off, &lv, word))]));
    }
    let fp = pos.iter().any(|p| p.as_name() == Some("fp"));
    let pos: Vec<&Term> = pos
        .into_iter()
        .filter(|p| p.as_name() != Some("fp"))
        .collect();
    if pos.len() != 3 || !pos[1].is_arrow("<-") {
        return Err(fail(
            body,
            st,
            "L2.MOV.015",
            format!("{} DST <- SRC ...", op.op),
        ));
    }
    let (dst, src) = (pos[0], pos[2]);
    if op.op == "gt4" && kw("walk").is_some() {
        return gt4_walks(body, op, dst, src, env, tg);
    }
    if op.op == "quant" || op.op == "gt4" {
        return convert(body, op, dst, src, env, tg);
    }
    if op.op == "copy" {
        let ((dn, doff, dl), (sn, soff, sl)) = copy_walks(body, st, env, tg)?;
        if st.kw("classed").is_some() && !on("l2.class/squeeze-walk") {
            if st.kw("walk").is_none()
                || st.kw("dwalk").is_none()
                || count(&sl) > MOVE_WORDS
                || sl.len() > tg.levels
                || dl.len() > tg.levels
            {
                return Err(fail(
                    body,
                    st,
                    "L2.MOV.015",
                    "class copy needs explicit walks fitting one mover command",
                ));
            }
            let literal = |name: &str, off: i64, levels: &Levels| {
                let levels = levels
                    .iter()
                    .map(|(n, s)| format!("({n}, {s})"))
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("w({name} + {off}, {levels})")
            };
            return Ok((
                vec![format!(
                    "copy {} -> {}",
                    literal(&sn, soff, &sl),
                    literal(&dn, doff, &dl)
                )],
                vec![
                    mem_of(false, span_of(&sn, soff, &sl, word)),
                    mem_of(true, span_of(&dn, doff, &dl, word)),
                ],
            ));
        }
        // Row compaction cuts own complete DRAM lines, including odd-word rows.
        let mut limit = MOVE_WORDS;
        let source = merge(&squeeze(&sl));
        let dest = merge(&squeeze(&dl));
        let line = i64::from(tg.machine.card.dram.line_bytes);
        if !on("l2.mover/unaligned-row-cut")
            && source.len() == 2
            && source[1].1 == word
            && dest.len() == 1
            && dest[0].1 == word
            && doff % line == 0
        {
            let row_words = source[1].0;
            let (mut a, mut b) = (row_words * word, line);
            while b != 0 {
                (a, b) = (b, a % b);
            }
            let quantum = row_words * (line / a);
            if quantum <= MOVE_WORDS {
                limit = MOVE_WORDS / quantum * quantum;
            }
        }
        let lines = cut(&[sl.clone(), dl.clone()], limit, None, 0, tg)
            .into_iter()
            .map(|(o, l)| {
                format!(
                    "copy {} -> {}",
                    walk_text(&sn, soff + o[0], &l[0]),
                    walk_text(&dn, doff + o[1], &l[1])
                )
            })
            .collect();
        return Ok((
            lines,
            vec![
                mem_of(false, span_of(&sn, soff, &sl, word)),
                mem_of(true, span_of(&dn, doff, &dl, word)),
            ],
        ));
    }
    if op.op == "gather" || op.op == "scatter" {
        let Some(ix) = kw("index") else {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!("{} DST <- SRC index=IDX [fp]", op.op),
            ));
        };
        let (idx_name, ioff, ilv) = index_list(body, st, ix, env, tg)?;
        let table = if op.op == "gather" { src } else { dst };
        let tv = value(body, st, &as_view(table), env)?;
        let tname = view_name(&tv);
        let tb = buffer(body, st, &tname, "buffer ")?;
        let pitch = if let Some(stride) = kw("table_stride") {
            i64::try_from(int_kw(body, st, stride)?).map_err(|_| {
                fail(
                    body,
                    st,
                    "L2.MOV.015",
                    "table_stride exceeds signed address range",
                )
            })?
        } else {
            tb.nbytes(tg) / tb.shape[0]
        };
        let (rn, roff, n, per, rl) = rows_walk(body, st, env, tg)?;
        if ilv < n {
            return Err(fail(
                body,
                st,
                "L2.MOV.015",
                format!(
                    "{}: {n} rows want {n} indices, {idx_name} holds {ilv}",
                    op.op
                ),
            ));
        }
        let ((_, rs), wlv) = if rl.len() > 1 {
            (rl[0], rl[1..].to_vec())
        } else {
            ((1, word), rl.clone())
        };
        let many = 8.max(tg.max_index.min(MOVE_WORDS / per) / 8 * 8);
        let mut lines = Vec::new();
        let mut c0 = 0;
        for (wo, wl) in cut(&[wlv], tg.row_words, None, 1, tg) {
            let (wo, wl) = (wo[0], &wl[0]);
            let k = count(wl);
            let mut r0 = 0;
            while r0 < n {
                let m = many.min(n - r0);
                let mut lv = vec![(m, rs)];
                lv.extend(wl.iter().copied());
                let walk = walk_text(&rn, roff + r0 * rs + wo, &lv);
                let table = format!("{tname} + {}", c0 * word);
                let text = if op.op == "gather" {
                    format!("gather {table} -> {walk}")
                } else {
                    format!("scatter {walk} -> {table}")
                };
                lines.push(format!(
                    "{text} index={idx_name} + {} n={m} pitch={pitch} words={k}{}",
                    ioff + 4 * r0,
                    if fp { " fp" } else { "" }
                ));
                r0 += many;
            }
            c0 += k;
        }
        let table_acc = mem(UNIT, op.op == "scatter", &tname, 0, tb.nbytes(tg));
        let rows_acc = mem_of(op.op == "gather", span_of(&rn, roff, &rl, word));
        return Ok((
            lines,
            vec![
                table_acc,
                rows_acc,
                mem(UNIT, false, &idx_name, ioff, ioff + 4 * ilv),
            ],
        ));
    }
    Err(fail(
        body,
        st,
        "L2.MOV.015",
        format!("no mover op '{}': {}", op.op, OPS.join(", ")),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tgt() -> Target {
        Target::of(&ktpu_machine::Machine::named("multimesh_v9t6").unwrap())
    }

    #[test]
    #[allow(non_snake_case)]
    fn L2_MOV_008_cut_words() {
        let t = tgt();
        // 2 x 16384 words, one level: two moves of 16384.
        let got = cut(&[vec![(32768, 32)]], MOVE_WORDS, None, 0, &t);
        assert_eq!(got.len(), 2);
        assert_eq!(got[1].0, vec![16384 * 32]);
        assert_eq!(got[1].1, vec![vec![(16384, 32)]]);
    }

    #[test]
    fn merge_and_squeeze() {
        assert_eq!(merge(&[(4, 128), (4, 32)]), vec![(16, 32)]);
        assert_eq!(squeeze(&[(1, 64), (4, 32)]), vec![(4, 32)]);
        assert_eq!(squeeze(&[(1, 64)]), vec![(1, 64)]);
    }
}
