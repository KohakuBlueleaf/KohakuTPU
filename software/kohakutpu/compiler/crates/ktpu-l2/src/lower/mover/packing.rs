//! Finite FP16 packing on a vector core, retaining the mover entry map.

use super::quant_status::Status;
use super::*;
use crate::lower::sync::Stage;
use ktpu_machine::packing::STATUS_RECORD;
use ktpu_machine::PackingBackend;

mod finish;
mod image;
mod mapped;
mod padding;

pub(super) use image::{clear, status};
pub use mapped::fits as composed_fits;

/// Select a permitted backend. An op override never widens board admission.
/// A destination of K pairs (`nk` >= 2) takes vector_mx7 (L2.MOV.018, RUL.099),
/// and so does a read map or in-place padding where the board allows it.
pub fn quant_backend(body: &Body, op: &Op, tg: &Target) -> Result<bool> {
    let policy = &tg.machine.conversion.fp16_mx7;
    let paired = op
        .st
        .positional()
        .first()
        .and_then(|d| body.buffers.get(&view_name(&as_view(d))))
        .is_some_and(|b| b.get("nk") >= 2)
        && !on("l2.packing/mover-paired")
        && !crate::mutant::before_rul099();
    let vector_only = ["read_map", "pad_cols", "pad"]
        .iter()
        .any(|k| op.st.kw(k).is_some());
    let backend = match op.st.kw("backend") {
        None if paired => PackingBackend::VectorMx7,
        None if vector_only && policy.allowed_backends.contains(&PackingBackend::VectorMx7) => {
            PackingBackend::VectorMx7
        }
        None => policy.preferred_backend,
        Some(Term::Name(name)) if name == "mover_quant" => PackingBackend::MoverQuant,
        Some(Term::Name(name)) if name == "vector_mx7" => PackingBackend::VectorMx7,
        Some(value) => {
            return Err(fail(
                body,
                &op.st,
                "L2.MOV.010",
                format!("unknown packing backend {}", ktpu_l1::text::fmt(value)),
            ))
        }
    };
    if op.st.kw("read_map").is_some() && backend != PackingBackend::VectorMx7 {
        return Err(fail(
            body,
            &op.st,
            "L2.READ.001",
            "logical read-map packing requires backend=vector_mx7",
        ));
    }
    if (op.st.kw("pad_cols").is_some() || op.st.kw("pad").is_some())
        && backend != PackingBackend::VectorMx7
    {
        return Err(fail(
            body,
            &op.st,
            "L2.MOV.010",
            "in-place padding requires vector_mx7",
        ));
    }
    if !policy.allowed_backends.contains(&backend) && !on("l2.packing/ignore-policy") {
        return Err(fail(
            body,
            &op.st,
            "L2.MOV.010",
            "conversion.fp16_mx7 excludes the requested backend",
        ));
    }
    if paired && backend != PackingBackend::VectorMx7 {
        return Err(fail(
            body,
            &op.st,
            "L2.MOV.018",
            "quant into an nk >= 2 destination needs vector_mx7 (RUL.099)",
        ));
    }
    Ok(backend == PackingBackend::VectorMx7)
}

/// Where a checked quant's finite status goes.
#[derive(Clone, Debug)]
pub enum Finite {
    /// Its own buffer, checked behind a barrier at the quant.
    Inline(String),
    /// The caller's region, checked by a later `quant_check`.
    Deferred(Status),
}

/// Every unit's status image `name` run into `status`, and its check here or
/// its record pending a `quant_check`.
fn finite_status(
    status: &Finite,
    name: &str,
    units: &[String],
    lines: &mut Vec<String>,
    accesses: &mut Vec<Access>,
) {
    let at = match status {
        Finite::Inline(s) => s.clone(),
        Finite::Deferred(s) => s.address(),
    };
    for (v, unit) in units.iter().enumerate() {
        lines.extend([
            format!("send {unit}"),
            format!("    run {name}({at} + {})", v as i64 * STATUS_RECORD),
        ]);
    }
    match status {
        Finite::Inline(_) => {
            lines.push("barrier".into());
            for v in 0..units.len() as i64 {
                lines.extend([
                    format!("r14 = load {at} + {}", v * STATUS_RECORD),
                    "assert_zero r14 code=1".into(),
                ]);
            }
        }
        Finite::Deferred(s) => {
            for (v, unit) in units.iter().enumerate() {
                let lo = s.offset + v as i64 * STATUS_RECORD;
                accesses.push(Access::new(
                    unit,
                    true,
                    ("mem", &s.name),
                    lo,
                    lo + STATUS_RECORD,
                ));
            }
        }
    }
}

/// A serialized 16-entry L1 slot: gather, exact unpack, MX7 pack, drain.
pub fn vector_quant(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    images: &mut Vec<String>,
    status: Option<Finite>,
) -> Result<Stage> {
    if op.st.kw("read_map").is_some() {
        return mapped::quant(body, op, env, tg, images, status);
    }
    let st = &op.st;
    let err = |msg| fail(body, st, "L2.MOV.010", msg);
    let checked = status.is_some();
    let policy = &tg.machine.conversion.fp16_mx7;
    if checked && policy.version < 2 {
        return Err(err(
            "vector_mx7 requires a validated finite=1 input contract".into(),
        ));
    }
    let vc = &tg.machine.vc;
    if checked
        && (tg.machine.fw.status.get("KA_ST_CONTRACT") != Some(&0x24)
            || tg.vregs < 32
            || tg.sregs < 15
            || tg.descriptors < 3
            || vc.tau != 8)
    {
        return Err(err(
            "checked MX7 requires contract completion, 32 registers and EXP2D tau=8".into(),
        ));
    }
    let finite_batch = match st.kw("finite_batch") {
        Some(k) => const_of(body, st, k, env)?,
        None => 1,
    };
    if ![1, 2, 4].contains(&finite_batch)
        || (checked && vc.preds < finite_batch as u32)
        || i64::from(tg.vregs) <= 4 * (finite_batch - 1)
    {
        return Err(err(
            "finite_batch is 1, 2 or 4, bounded by available registers and predicates".into(),
        ));
    }
    let overlap = match st.kw("overlap") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("overlap is 0 or 1".into())),
        },
    };
    let classifier_overlap = match st.kw("classifier_overlap") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("classifier_overlap is 0 or 1".into())),
        },
    };
    let pipeline = match st.kw("pipeline") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("pipeline is 0 or 1".into())),
        },
    } && !on("l2.packing/serial-engines");
    let zero_classifier = match st.kw("zero_classifier") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("zero_classifier is 0 or 1".into())),
        },
    } && !on("l2.packing/compare-classifier");
    let stream_axes = match st.kw("stream_axes") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("stream_axes is 0 or 1".into())),
        },
    } && !on("l2.packing/ordered-axes");
    let fill_overlap = match st.kw("fill_overlap") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("fill_overlap is 0 or 1".into())),
        },
    } && !on("l2.packing/frontend-fill");
    let zero_reuse = match st.kw("zero_reuse") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("zero_reuse is 0 or 1".into())),
        },
    } && !on("l2.packing/repeated-zero");
    let stream_batch = match st.kw("stream_batch") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("stream_batch is 0 or 1".into())),
        },
    } && !on("l2.packing/short-batches");
    let classifier_banks = match st.kw("classifier_banks") {
        None => false,
        Some(k) => match const_of(body, st, k, env)? {
            0 => false,
            1 => true,
            _ => return Err(err("classifier_banks is 0 or 1".into())),
        },
    } && zero_classifier
        && !on("l2.packing/single-classifier-bank");
    if classifier_banks && tg.vregs < 24 {
        return Err(err("classifier_banks requires 24 vector registers".into()));
    }
    let slots = policy.vector_slots;
    let cores = tg.machine.quant_cores();
    if cores < 1 || cores > tg.vc.len() as i64 {
        return Err(err(
            "vector_cores must select 1..available VCs (0 means all)".into(),
        ));
    }
    if tg.vc.is_empty()
        || tg.ports.is_empty()
        || vc.has_mx == 0
        || tg.chunks != 8
        || tg.lanes != 16
        || tg.word_bytes != 32
        || tg.descriptors < 2
        || vc.descriptor_dims < 3
        || tg.l1_words < (192 * slots).max(if checked { 388 } else { 0 })
        || vc.walk_max < 128
        || !vc.pack_modes.iter().any(|s| s == "mx7")
        || tg.machine.mover.xform.quant_lanes != 4
        || tg.machine.mover.xform.quant_values != 32
        || M::entry(tg) != 128
    {
        return Err(err(
            "vector_mx7 requires a routable eight-chunk MX7 VC, two descriptors and 192 L1 words"
                .into(),
        ));
    }
    let p = st.positional();
    if p.len() != 3 {
        return Err(err("quant DST <- SRC".into()));
    }
    let dv = value(body, st, &as_view(p[0]), env)?;
    let sv = value(body, st, &as_view(p[2]), env)?;
    let (dn, sn) = (view_name(&dv), view_name(&sv));
    let db = buffer(body, st, &dn, "destination ")?;
    if !db.is_mx7() || db.flag("t") {
        return Err(err(
            "quant destination is non-transposed mx7a or mx7b".into()
        ));
    }
    let sb = buffer(body, st, &sn, "source ")?;
    let dax = M::axes_of(&dv, &db.shape).map_err(err)?;
    let sax = M::axes_of(&sv, &sb.shape).map_err(err)?;
    let ds = M::shape_of(&dax);
    let ss = M::shape_of(&sax);
    let half_columns = match st.kw("pad_cols") {
        None => false,
        Some(k) if const_of(body, st, k, env)? == 16 => true,
        Some(_) => return Err(err("pad_cols supports a 16-column word tail".into())),
    };
    let padded = st.kw("pad").is_some();
    let shape_ok = if padded {
        ds.len() == 2 && ss.len() == 2 && ss[0] <= ds[0] && ss[1] <= ds[1] && ss[1] % 16 == 0
    } else if half_columns {
        ds.len() == 2 && ss.len() == 2 && ds[0] == ss[0] && ds[1] == 32 && ss[1] == 16
    } else {
        ds == ss
    };
    if sb.dtype != "f16" || !shape_ok || sax.len() != 2 {
        return Err(err(
            "quant requires matching two-dimensional FP16 and MX7 views".into(),
        ));
    }
    let soff = M::rowmajor_walk(sb, &sax).map_err(err)?.0;
    let pitch = sb.shape.last().unwrap() * sb.item();
    if soff % 32 != 0 || pitch % 32 != 0 || pitch <= 0 {
        return Err(err("vector_mx7 gathers whole source words".into()));
    }
    if padded
        && (dax.len() != 2
            || dax[0].lo % 4 != 0
            || ds[0] % 4 != 0
            || dax[1].lo % 32 != 0
            || ds[1] % 32 != 0)
    {
        return Err(err(
            "padded quant destination covers complete 4x32 entries".into()
        ));
    }
    if let Some(Finite::Deferred(s)) = &status {
        let need = ktpu_machine::packing::quant_status_bytes(cores);
        if s.bytes < need {
            return Err(fail(
                body,
                st,
                "L2.MOV.019",
                format!(
                    "a quant status of {} bytes: its cores' records take {need}",
                    s.bytes
                ),
            ));
        }
    }
    let pieces = if padded {
        padding::pieces(db, &dax, &sax, soff, pitch)
    } else {
        vec![padding::Piece {
            dax,
            soff,
            rows: 4,
            words: if half_columns { 1 } else { 2 },
        }]
    };
    let schedule = Schedule {
        body,
        op,
        tg,
        checked,
        finite_batch,
        overlap,
        classifier_overlap,
        pipeline,
        zero_classifier,
        stream_axes,
        fill_overlap,
        zero_reuse,
        stream_batch,
        classifier_banks,
        slots,
        cores,
        pitch,
        sn: &sn,
        dn: &dn,
        db,
    };
    // One finite status a quant: every piece folds into its core's sticky
    // register, cleared before the first piece and read after the last.
    let units: Vec<String> = (0..cores).map(|v| format!("vc[{v}]")).collect();
    let mut result = Stage::new(Vec::new(), Vec::new(), Vec::new());
    if checked {
        result.units = units.clone();
        let name = format!("{}.mx7_clear_{}", body.name, images.len());
        images.push(image::clear(&name));
        for unit in &units {
            result
                .lines
                .extend([format!("send {unit}"), format!("    run {name}()")]);
        }
        // Enter a runtime walk with no completion debt from initialization.
        result
            .lines
            .extend(units.iter().map(|unit| format!("wait {unit}")));
    }
    let tag = images.len();
    for piece in pieces {
        let stage = schedule.emit(piece, images)?;
        result.lines.extend(stage.lines);
        result.accesses.extend(stage.accesses);
        for unit in stage.units {
            if !result.units.contains(&unit) {
                result.units.push(unit);
            }
        }
    }
    if let Some(status) = &status {
        let name = format!("{}.mx7_status_{tag}", body.name);
        images.push(image::status(&name));
        finite_status(
            status,
            &name,
            &units,
            &mut result.lines,
            &mut result.accesses,
        );
    }
    Ok(result)
}

/// IMEM words a batch image leaves for what its L1 lowering adds.
const IMEM_MARGIN: usize = crate::lower::readmap::Knobs::DEFAULT.imem_margin;

/// Loop words a VSKIPZ guard skips: its 8-bit field, less what L1 adds.
const SKIP_ROOM: usize = 255 - 32;

/// The instruction lines of an image's text.
fn image_words(text: &str) -> usize {
    text.lines()
        .skip(1)
        .filter(|l| !l.trim().is_empty())
        .count()
}

#[derive(Clone, Copy)]
struct Schedule<'a> {
    body: &'a Body,
    op: &'a Op,
    tg: &'a Target,
    checked: bool,
    finite_batch: i64,
    overlap: bool,
    classifier_overlap: bool,
    pipeline: bool,
    zero_classifier: bool,
    stream_axes: bool,
    fill_overlap: bool,
    zero_reuse: bool,
    stream_batch: bool,
    classifier_banks: bool,
    slots: i64,
    cores: i64,
    pitch: i64,
    sn: &'a str,
    dn: &'a str,
    db: &'a Buffer,
}

impl Schedule<'_> {
    fn emit(&self, piece: padding::Piece, images: &mut Vec<String>) -> Result<Stage> {
        self.emit_across(piece, images, 16)
    }

    /// [`Self::emit`], a tile row spanning at most `widest` entries.
    fn emit_across(
        &self,
        piece: padding::Piece,
        images: &mut Vec<String>,
        widest: i64,
    ) -> Result<Stage> {
        let again = piece.clone();
        let Self {
            body,
            op,
            tg,
            checked,
            finite_batch,
            overlap,
            classifier_overlap,
            pipeline,
            zero_classifier,
            stream_axes,
            fill_overlap,
            zero_reuse,
            stream_batch,
            classifier_banks,
            slots,
            cores,
            pitch,
            sn,
            dn,
            db,
        } = *self;
        let st = &op.st;
        let err = |msg| fail(body, st, "L2.MOV.010", msg);
        let policy = &tg.machine.conversion.fp16_mx7;
        let padding::Piece {
            dax,
            soff,
            rows: valid_rows,
            words: row_words,
        } = piece;
        let ((dbase, mut sl, mut dl), flag) = xform::quant(db, &dax, pitch, tg).map_err(err)?;
        sl[4].0 = valid_rows;
        sl[5].0 = row_words;
        // Zero entries have no source walk and cannot raise the finite flag.
        let pitch = if valid_rows == 0 { 0 } else { pitch };
        let checked = checked && valid_rows != 0;
        let partial = valid_rows != 4 || row_words != 2;
        let split_rows = pitch >= tg.step();
        if split_rows && tg.descriptors < 6 {
            return Err(err("large-pitch MX7 gathers require six descriptors".into()));
        }
        // A tile row spans `across` entries of the axis whose entries continue
        // one another's source rows: each row is one burst, not 64-byte pieces.
        let limit = widest.min(if partial {
            4
        } else if checked && slots == 2 {
            7
        } else {
            16
        });
        let (across, inner_dst) = match (0..4)
            .filter(|&i| {
                !split_rows
                    && valid_rows != 0
                    && row_words == 2
                    && sl[i].0 > 1
                    && sl[i].1 == sl[5].1 * row_words
                    && dl[i].1 < tg.step()
            })
            .max_by_key(|&i| sl[i].0)
        {
            Some(i) => {
                let n = (1..=limit.min(sl[i].0))
                    .rev()
                    .find(|n| sl[i].0 % n == 0)
                    .unwrap();
                let inner = dl[i].1;
                sl[i] = (sl[i].0 / n, sl[i].1 * n);
                dl[i] = (dl[i].0 / n, dl[i].1 * n);
                (n, inner)
            }
            None => (1, 0),
        };
        if stream_axes && !split_rows {
            // Reordering disjoint output entries preserves the address-pair map.
            // Jointly contiguous axes can be collapsed before selecting a stream.
            for i in 0..4 {
                for j in 0..4 {
                    if i != j
                        && sl[i].0 > 1
                        && sl[j].0 > 1
                        && sl[i].1 == sl[j].0 * sl[j].1
                        && dl[i].1 == dl[j].0 * dl[j].1
                    {
                        sl[j].0 *= sl[i].0;
                        dl[j].0 *= dl[i].0;
                        sl[i].0 = 1;
                        dl[i].0 = 1;
                    }
                }
            }
            let axis = (0..4)
                .filter(|&i| sl[i].1 < tg.step() && dl[i].1 < tg.step())
                .max_by_key(|&i| sl[i].0)
                .unwrap_or(2);
            sl.swap(2, axis);
            dl.swap(2, axis);
        }
        let stream_batch = stream_batch && valid_rows != 0 && !split_rows;
        let source_stride = if valid_rows == 0 { 0 } else { sl[2].1 };
        let incremental = source_stride < tg.step() && dl[2].1 < tg.step();
        // Small partitions use a single batch instead of splitting 8 entries into
        // 7+1 and repeatedly replacing two images at every outer tile.
        let paired = checked && slots == 2 && sl[2].0 / cores >= 14;
        let cap = if stream_batch || (valid_rows == 0 && zero_reuse) {
            16
        } else if partial {
            4
        } else if paired {
            7
        } else {
            16
        };
        let dtype = if flag.is_empty() ^ on("l2.packing/wrong-layout") {
            "mx7a"
        } else {
            "mx7b"
        };
        // `n` stream groups a batch: `n * across` entries.
        let image_of = |name: &str, n: i64, slots: i64, steady: Option<bool>, odd: bool| {
            image::Batch {
                name,
                n: n * across,
                across,
                source_stride,
                stride: dl[2].1,
                incremental,
                dtype,
                slots,
                checked,
                split_rows,
                finite_batch,
                valid_rows,
                row_words,
                overlap,
                classifier_overlap,
                pipeline,
                zero_classifier,
                fill_overlap,
                zero_reuse,
                classifier_banks,
                steady,
                odd,
                lift: db.get("nk") >= 2
                    && !on("l2.packing/unit-scale-zero")
                    && !crate::mutant::before_rul099(),
            }
            .emit()
        };
        // The largest image a batch of `n` groups makes fits a core's IMEM.
        let room = tg.machine.vc.imem_words as usize - IMEM_MARGIN;
        let fits = |n: i64| image_words(&image_of("probe", n, slots, None, stream_batch)) <= room;
        // A loop VSKIPZ can skip keeps one image for every trip count; a
        // longer one is specialised on whether it runs.
        let guarded = |n: i64| {
            let words = |steady| image_words(&image_of("probe", n, 2, Some(steady), false));
            words(true) - words(false) <= SKIP_ROOM
        };
        let mut batch = if incremental {
            (cap / across)
                .max(1)
                .min((tg.step() - 1) / source_stride.max(1))
                .min((tg.step() - 1) / dl[2].1)
        } else {
            1 // Every entry is absolute when a gather stride cannot be represented.
        };
        while batch > 1 && !fits(batch) {
            batch -= 1;
        }
        if across > 1 && !fits(batch) {
            return self.emit_across(again, images, across - 1);
        }
        let tag = images.len();
        let cores = cores.min(sl[2].0);
        let units: Vec<String> = (0..cores).map(|v| format!("vc[{v}]")).collect();
        let mut lines = Vec::new();
        let mut inner = Vec::new();
        let mut emitted = std::collections::BTreeSet::new();
        for v in 0..cores {
            let first = sl[2].0 * v / cores;
            let end = sl[2].0 * (v + 1) / cores;
            let mut group = first;
            while group < end {
                let n = if stream_batch {
                    (1..=batch.min(end - group))
                        .rev()
                        .find(|n| (end - group) % n == 0)
                        .unwrap()
                } else {
                    batch.min(end - group)
                };
                let full = if incremental { (end - group) / n } else { 1 };
                let double = slots == 2 && full >= 2;
                let odd = stream_batch && double && full % 2 != 0;
                let consumed = if double && !odd { full / 2 * 2 } else { full };
                let trips = if double { full / 2 - 1 } else { full };
                let used_slots = if double { 2 } else { 1 };
                let steady = if stream_batch || (double && !guarded(n)) {
                    Some(trips != 0)
                } else {
                    None
                };
                let suffix = match (stream_batch, steady) {
                    (true, _) => format!("_s{}_o{}", i32::from(trips != 0), i32::from(odd)),
                    (false, Some(s)) => format!("_s{}", i32::from(s)),
                    (false, None) => String::new(),
                };
                let name = format!("{}.mx7_{tag}_{n}_{used_slots}{suffix}", body.name);
                if emitted.insert((n, used_slots, steady, odd)) {
                    images.push(image_of(&name, n, used_slots, steady, odd));
                }
                let s = soff + group * sl[2].1;
                let d = dbase + group * dl[2].1;
                let node_batches = if !incremental && policy.vector_looped {
                    (end - group) / n
                } else {
                    1
                };
                let (src_reg, dst_reg) = if node_batches > 1 {
                    inner.extend([
                        "r3 = set 0".into(),
                        format!("label mx7_batch_{tag}_{v}_{group}"),
                        format!("r8 = mul r3, {}", n * sl[2].1),
                        "r8 = add r8, r10".into(),
                        format!("r9 = mul r3, {}", n * dl[2].1),
                        "r9 = add r9, r11".into(),
                    ]);
                    ("r8", "r9")
                } else {
                    ("r10", "r11")
                };
                inner.push(format!("send vc[{v}]"));
                if valid_rows == 0 {
                    // A zero tile packs register zeros; no source descriptor or DMA.
                } else if split_rows {
                    for (row, desc) in [0, 3, 4, 5].iter().take(valid_rows as usize).enumerate() {
                        inner.push(format!(
                            "    desc a{desc} = {sn} + {} + {src_reg} walk=(32, {row_words})",
                            s + row as i64 * pitch
                        ));
                    }
                } else {
                    inner.push(format!(
                    "    desc a0 = {sn} + {s} + {src_reg} walk=((32, {}), ({pitch}, {valid_rows}), ({}, {n}))",
                    row_words * across,
                    if n == 1 { 0 } else { source_stride }
                ));
                }
                let row = if across == 1 {
                    String::new()
                } else {
                    format!("({inner_dst}, {across}), ")
                };
                inner.push(format!(
                    "    desc a1 = {dn} + {d} + {dst_reg} walk=((32, 4), {row}({}, {n}))",
                    dl[2].1
                ));
                inner.push(format!("    run {name}({trips})"));
                if node_batches > 1 {
                    inner.extend([
                        "r3 = add r3, 1".into(),
                        format!("goto mx7_batch_{tag}_{v}_{group} if r3 < {node_batches}"),
                    ]);
                }
                group += n * consumed * node_batches;
            }
        }
        // Retire each outer tile before issuing the next tile's descriptor/image
        // stream. This bounds dispatch completion debt independently of outer shape.
        // All selected VCs have already been issued; they still execute in parallel,
        // and each image retains its internal double-buffered DMA pipeline.
        if !on("l2.packing/no-tile-wait") {
            inner.extend(units.iter().map(|unit| format!("wait {unit}")));
        }
        let axes = [0usize, 1, 3];
        if policy.vector_looped {
            for reg in 0..axes.len() {
                lines.extend([format!("r{reg} = set 0"), format!("label mx7_{tag}_{reg}")]);
            }
            lines.extend(["r10 = set 0".into(), "r11 = set 0".into()]);
            for (reg, axis) in axes.iter().enumerate() {
                lines.extend([
                    format!("r12 = mul r{reg}, {}", sl[*axis].1),
                    "r10 = add r10, r12".into(),
                    format!("r12 = mul r{reg}, {}", dl[*axis].1),
                    "r11 = add r11, r12".into(),
                ]);
            }
            lines.extend(inner);
            for (reg, axis) in axes.iter().enumerate().rev() {
                lines.extend([
                    format!("r{reg} = add r{reg}, 1"),
                    format!("goto mx7_{tag}_{reg} if r{reg} < {}", sl[*axis].0),
                ]);
            }
        } else {
            for ti in 0..sl[0].0 {
                for ci in 0..sl[1].0 {
                    for bi in 0..sl[3].0 {
                        let src = if on("l2.packing/absolute-zero") {
                            0
                        } else {
                            ti * sl[0].1 + ci * sl[1].1 + bi * sl[3].1
                        };
                        let dst = ti * dl[0].1 + ci * dl[1].1 + bi * dl[3].1;
                        // A statically expanded walk must remain static through L1:
                        // scalar MOVs would turn each descriptor into LOAD/ALU/PATCH
                        // and prevent the existing image reuse / engine lowering.
                        lines.extend(inner.iter().map(|line| {
                            line.replace(" + r10", &format!(" + {src}"))
                                .replace(" + r11", &format!(" + {dst}"))
                        }));
                    }
                }
            }
        }
        let mut accesses = Vec::new();
        sl.push((across, sl[5].1 * row_words));
        dl.push((across, inner_dst));
        for unit in &units {
            for mut a in [
                mem_of(false, span_of(sn, soff, &sl, 32)),
                mem_of(true, span_of(dn, dbase, &dl, 128)),
            ] {
                if a.write || valid_rows != 0 {
                    a.unit = unit.clone();
                    accesses.push(a);
                }
            }
            accesses.push(Access::new(
                unit,
                true,
                (unit, "l1"),
                0,
                if checked { 388 } else { 192 * slots },
            ));
        }
        Ok(Stage::new(lines, units, accesses))
    }
}
