//! `tile_transpose`: f16 `[nb, A, B]` to `[nb, B, A]` a 32 x 32 tile at a time
//! on the vector cores, one read and one write of memory.
//!
//! A tile is 8 bands of 4 rows of 2 words. A GT4 unpack of band i puts rows and
//! columns at (register i, chunk c >> 2, lane 4 (r & 3) + (c & 3)); two masked
//! lane swaps exchange r & 3 and c & 3 inside each chunk; 64 chunk moves
//! exchange register and chunk; a GT4 pack lays the bands back as rows.

use super::*;
use crate::lower::sync::Stage;
use crate::target::Target;
use ktpu_l1::text::Term;

/// Rows and columns a tile.
const TILE: i64 = 32;
/// L1 words: the tile read, the tile written, the two mask rows.
const OUT: i64 = 64;
const MASKS: i64 = 128;
const L1_WORDS: i64 = 144;
/// Registers: the bands read, the bands written, the two masks.
const BANDS: i64 = 8;
const MASK_REGS: [i64; 2] = [16, 17];

/// The image of `trips` tiles, each a step of `src_step` and `dst_step` bytes.
fn image(name: &str, src_step: i64, dst_step: i64) -> String {
    let mut lines = vec![
        format!("image {name}(trips)"),
        format!("    ainc a0 += {src_step}"),
        format!("    ainc a1 += {dst_step}"),
        format!("    vfill a2 -> l1[{MASKS}]"),
        "    vsync unpack on=fill".into(),
    ];
    // A vsel keeps its a operand where c is set: the masks' complements.
    for (i, v) in MASK_REGS.iter().enumerate() {
        lines.push(format!(
            "    vunpk v{v} <- l1[{}] f16",
            MASKS + 8 * i as i64
        ));
        lines.push(format!("    vsel v{v}, k0, k1, v{v}"));
    }
    lines.extend(["    seti s0 = trips".into(), "    loop s0".into()]);
    let body = |lines: &mut Vec<String>, s: String| lines.push(format!("        {s}"));
    body(&mut lines, "vsync fill on=unpack".into());
    body(&mut lines, "vfill a0 -> l1[0] rel".into());
    body(&mut lines, "vsync unpack on=fill".into());
    for i in 0..BANDS {
        body(
            &mut lines,
            format!("vunpk v{i} <- l1[{}] gt4 walk=(2, 1)", 8 * i),
        );
    }
    for (k, v) in [5, 10].iter().zip(MASK_REGS) {
        for i in 0..BANDS {
            body(&mut lines, format!("vsel v{i}, v{i}, xor(v{i}, {k}), v{v}"));
        }
    }
    for j in 0..BANDS {
        for i in 0..BANDS {
            let to = BANDS + j;
            body(&mut lines, format!("vmov v{to}[{i}:], v{i}[{j}:] vl=16"));
        }
    }
    body(&mut lines, "vsync pack on=drain".into());
    for j in 0..BANDS {
        body(
            &mut lines,
            format!(
                "vpack v{} -> l1[{}] gt4 walk=(2, 1)",
                BANDS + j,
                OUT + 8 * j
            ),
        );
    }
    body(&mut lines, "vsync drain on=pack".into());
    body(&mut lines, format!("vdrain a1 <- l1[{OUT}] rel"));
    lines.extend(["    vsync fe on=drain".into(), "    halt".into()]);
    lines.join("\n")
}

fn whole<'a>(
    body: &'a Body,
    st: &Stmt,
    t: &Term,
    env: &Env,
    what: &str,
) -> Result<(String, i64, &'a Buffer)> {
    let v = value(body, st, &as_view(t), env)?;
    let name = view_name(&v);
    let b = buffer(body, st, &name, what)?;
    let ax = M::axes_of(&v, &b.shape).map_err(|e| fail(body, st, "L2.MOV.020", e))?;
    let off = M::rowmajor_walk(b, &ax)
        .map_err(|e| fail(body, st, "L2.MOV.020", e))?
        .0;
    Ok((name, off, b))
}

/// The statement as images on every core, its tiles split along the longer of
/// A and B, whose walk the descriptors step.
pub fn tile_transpose(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    images: &mut Vec<String>,
) -> Result<Stage> {
    let st = &op.st;
    let err = |msg: &str| fail(body, st, "L2.MOV.020", msg);
    let p = st.positional();
    let int = |key: &str| -> Result<i64> {
        let t = st.kw(key).ok_or_else(|| err("tile_transpose nb= a= b="))?;
        const_of(body, st, t, env)
    };
    let (nb, a, b) = (int("nb")?, int("a")?, int("b")?);
    let mask = st.kw("mask").ok_or_else(|| err("tile_transpose mask="))?;
    let (dn, doff, db) = whole(body, st, p[0], env, "destination ")?;
    let (sn, soff, sb) = whole(body, st, p[2], env, "source ")?;
    let (mn, moff, _) = whole(body, st, mask, env, "mask ")?;
    if a % TILE != 0 || b % TILE != 0 || nb < 1 || a < 1 || b < 1 {
        return Err(err(
            "tile_transpose takes whole tiles of 32 rows and columns",
        ));
    }
    if tg.vc.is_empty()
        || tg.chunks != BANDS
        || tg.lanes != 16
        || tg.word_bytes != 32
        || tg.descriptors < 3
        || tg.l1_words < L1_WORDS
        || i64::from(tg.vregs) <= MASK_REGS[1]
    {
        return Err(err(
            "tile_transpose needs eight-chunk cores, three descriptors and 144 L1 words",
        ));
    }
    let pitch = |key: &str, default| -> Result<i64> {
        st.kw(key)
            .map_or(Ok(default), |term| const_of(body, st, term, env))
    };
    let (sp, dp) = (
        pitch("source_pitch", 2 * b)?,
        pitch("destination_pitch", 2 * a)?,
    );
    if sp < 2 * b || dp < 2 * a || sp % tg.word_bytes != 0 || dp % tg.word_bytes != 0 {
        return Err(err("tile_transpose: pitches cover whole-word rows"));
    }
    let source_end = soff + (nb * a - 1) * sp + 2 * b;
    let destination_end = doff + (nb * b - 1) * dp + 2 * a;
    if soff < 0
        || doff < 0
        || source_end > sb.shape.iter().product::<i64>() * sb.item()
        || destination_end > db.shape.iter().product::<i64>() * db.item()
    {
        return Err(err("tile_transpose: rectangle exceeds its allocation"));
    }
    if sp >= tg.step() || dp >= tg.step() {
        return Err(err("tile_transpose: a row past a descriptor's stride"));
    }
    let (ra, rb) = (a / TILE, b / TILE);
    // Tiles of x step along the row or down the columns; y's the other way.
    let (inner, outer, src_step, dst_step) = if rb >= ra {
        (rb, ra, 2 * tg.word_bytes, TILE * dp)
    } else {
        (ra, rb, TILE * sp, 2 * tg.word_bytes)
    };
    let stepping = src_step < tg.step() && dst_step < tg.step();
    let tag = images.len();
    let name = format!("{}.tile_transpose_{tag}", body.name);
    images.push(if stepping {
        image(&name, src_step, dst_step)
    } else {
        image(&name, 0, 0)
    });
    let cores = (tg.vc.len() as i64).min(inner);
    let units: Vec<String> = (0..cores).map(|v| format!("vc[{v}]")).collect();
    let walk = |pitch: i64| format!("walk=((32, 2), ({pitch}, {TILE}))");
    let mut lines = Vec::new();
    for n in 0..nb {
        for o in 0..outer {
            for v in 0..cores {
                let (first, end) = (inner * v / cores, inner * (v + 1) / cores);
                let run = if stepping { end - first } else { 1 };
                for k in (first..end).step_by(run as usize) {
                    let (ri, ci) = if rb >= ra { (o, k) } else { (k, o) };
                    let s = soff + n * a * sp + ri * TILE * sp + ci * 2 * tg.word_bytes;
                    let d = doff + n * b * dp + ci * TILE * dp + ri * 2 * tg.word_bytes;
                    lines.extend([
                        format!("send vc[{v}]"),
                        format!("    desc a0 = {sn} + {s} {}", walk(sp)),
                        format!("    desc a1 = {dn} + {d} {}", walk(dp)),
                        format!("    desc a2 = {mn} + {moff} walk=(32, 16)"),
                        format!("    run {name}({run})"),
                    ]);
                }
            }
            lines.extend(units.iter().map(|u| format!("wait {u}")));
        }
    }
    // A tile overwrites what a later tile reads.
    if sn == dn && soff < destination_end && doff < source_end {
        return Err(err("tile_transpose: source and destination overlap"));
    }
    let mut accesses = Vec::new();
    for unit in &units {
        for row in 0..nb * a {
            let at = soff + row * sp;
            accesses.push(Access::new(unit, false, ("mem", &sn), at, at + 2 * b));
        }
        for row in 0..nb * b {
            let at = doff + row * dp;
            accesses.push(Access::new(unit, true, ("mem", &dn), at, at + 2 * a));
        }
        accesses.extend([
            Access::new(unit, false, ("mem", &mn), moff, moff + 16 * tg.word_bytes),
            Access::new(unit, true, (unit, "l1"), 0, L1_WORDS),
        ]);
    }
    Ok(Stage::new(lines, units, accesses))
}
