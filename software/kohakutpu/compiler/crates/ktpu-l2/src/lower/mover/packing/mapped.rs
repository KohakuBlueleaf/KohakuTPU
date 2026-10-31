//! Pack logical walk tiles directly from original source words via a VC
//! register prologue. No converted FP16 intermediate is written to DRAM.

use std::collections::BTreeMap;

use super::*;
use crate::lower::readmap::{self, ReadMap, Shape};

fn format_walk(dims: &[(i64, i64)]) -> String {
    if let [(s, n)] = dims {
        format!("{s}, {n}")
    } else {
        dims.iter()
            .map(|(s, n)| format!("({s}, {n})"))
            .collect::<Vec<_>>()
            .join(", ")
    }
}

/// Fetch `shape`'s words to `slot`, the descriptor stepping between transfers
/// and on to `increment`, at most a DMA queue's worth of fills unsynced.
fn fill(
    shape: &Shape,
    descriptor: usize,
    slot: usize,
    increment: i64,
    steady: bool,
    t: &Target,
    lines: &mut Vec<String>,
) {
    if shape.words.is_empty() {
        return;
    }
    let (_, _, transfers) = shape.descriptor_transfers(t, t.read.pack_gather_words);
    for (i, &(at, offset)) in transfers.iter().enumerate() {
        if i > 0
            && i % t.dma_queue_depth == 0
            && !on("l2.readmap/no-pack-throttle")
            && !on("l2.fill/no-throttle")
        {
            lines.push("    vsync fe on=fill".into());
        }
        let next =
            transfers
                .get(i + 1)
                .map(|x| x.1)
                .unwrap_or(if steady { increment } else { offset });
        lines.extend([
            format!("    ainc a{descriptor} += {}", next - offset),
            format!("    vfill a{descriptor} -> l1[{}] rel", slot + at),
        ]);
    }
}

#[derive(Clone, Copy)]
struct Options {
    classifier_overlap: bool,
    pipeline: bool,
    zero_classifier: bool,
    lift: bool,
}

fn image(
    name: &str,
    job: &Job,
    dtype: &str,
    checked: bool,
    t: &Target,
    options: Options,
) -> String {
    let Job {
        shapes,
        shared: shared_source,
        copies,
        stride: strides,
        steady,
        pipelined: overlap,
        ..
    } = job;
    let (strides, steady, overlap) = (*strides, *steady, *overlap);
    let n = shapes.len();
    let args = (0..n)
        .flat_map(|i| {
            if shared_source[i] {
                vec![format!("dst{i}")]
            } else {
                vec![format!("src{i}"), format!("dst{i}")]
            }
        })
        .chain(std::iter::once("trips".into()))
        .collect::<Vec<_>>();
    let mut lines = vec![format!("image {name}({})", args.join(", "))];
    let mut slot_of = vec![0usize; n];
    let mut distinct = 0usize;
    for (i, s) in shapes.iter().enumerate() {
        if i > 0 && !shared_source[i] {
            distinct += 1;
        }
        slot_of[i] = (distinct % 2) * 128;
        if !shared_source[i] {
            let (dims, _, _) = s.descriptor_transfers(t, t.read.pack_gather_words);
            lines.push(format!(
                "    desc a{i} = src{i} walk=({})",
                format_walk(&dims)
            ));
        }
        lines.push(format!("    desc a{} = dst{i} walk=(32, 4)", n + i));
    }
    if checked {
        lines.extend(["    seti s2 = 65536.0".into(), "    seti s3 = 16.0".into()]);
    }
    if options.lift {
        lines.push(image::lift_seti());
    }
    let pack = finish::Finish {
        dtype,
        checked,
        classifier_overlap: options.classifier_overlap,
        pipeline: options.pipeline,
        zero_classifier: options.zero_classifier,
        lift: options.lift,
    };
    for i in 0..n {
        lines.push(format!("    ainc a{} += {}", n + i, strides.1));
    }
    let iteration = |prefetch: bool| {
        let mut code = Vec::new();
        for (i, shape) in shapes.iter().enumerate() {
            if !shared_source[i] {
                if !on("l2.readmap/no-fill-wait") {
                    code.push("    vsync fe on=fill".into());
                }
                shape.unpack(slot_of[i], &mut code);
                if overlap {
                    if let Some(next_i) = (i + 1..n).find(|&k| !shared_source[k]) {
                        fill(
                            &shapes[next_i],
                            next_i,
                            slot_of[next_i],
                            strides.0,
                            steady,
                            t,
                            &mut code,
                        );
                    }
                }
            }
            shape.rearrange(4 * i, pack.pipeline, &mut code);
            if !shared_source[i] && !overlap {
                if let Some(next_i) = (i + 1..n).find(|&k| !shared_source[k]) {
                    fill(
                        &shapes[next_i],
                        next_i,
                        slot_of[next_i],
                        strides.0,
                        steady,
                        t,
                        &mut code,
                    );
                }
            }
        }
        if prefetch {
            fill(&shapes[0], 0, 0, strides.0, steady, t, &mut code);
        }
        if overlap && !on("l2.readmap/no-output-reuse-wait") {
            code.push("    vsync pack on=drain".into());
        }
        pack.batch(0, 4, n as i64, 256, "", "    ", &mut code);
        if pack.pipeline {
            code.push("    vsync drain on=pack".into());
        }
        for (i, offsets) in copies.iter().enumerate() {
            for (j, &offset) in offsets.iter().enumerate() {
                if on("l2.readmap/drop-fanout") && j > 0 {
                    continue;
                }
                let next =
                    offsets
                        .get(j + 1)
                        .copied()
                        .unwrap_or(if steady { strides.1 } else { offset });
                code.extend([
                    format!("    ainc a{} += {}", n + i, next - offset),
                    format!("    vdrain a{} <- l1[{}] rel", n + i, 256 + i * 4),
                ]);
            }
        }
        code
    };
    if overlap {
        fill(&shapes[0], 0, 0, strides.0, steady, t, &mut lines);
        if steady {
            lines.extend(["    seti s0 = trips - 1".into(), "    loop s0".into()]);
            lines.extend(iteration(true).iter().map(|line| format!("    {line}")));
        }
        lines.extend(iteration(false));
    } else {
        lines.extend(["    seti s0 = trips".into(), "    loop s0".into()]);
        let mut code = Vec::new();
        fill(&shapes[0], 0, 0, strides.0, steady, t, &mut code);
        code.extend(iteration(false));
        code.push("    vsync fe on=drain".into());
        lines.extend(code.iter().map(|line| format!("    {line}")));
    }
    lines.push("    vsync fe on=drain".into());
    if checked {
        lines.extend([
            "    vsync fe on=math".into(),
            "    getstk s14".into(),
            "    vmax v31, v31, s14".into(),
            "    vsync fe on=math".into(),
        ]);
    }
    lines.push("    halt".into());
    lines.join("\n")
}

#[derive(Clone)]
struct SuperEntry {
    anchor: i64,
    shapes: Vec<Shape>,
    copies: Vec<Vec<i64>>,
    dests: Vec<i64>,
}

/// One run of one pack image.
struct Job {
    shapes: Vec<Shape>,
    shared: Vec<bool>,
    copies: Vec<Vec<i64>>,
    stride: (i64, i64),
    steady: bool,
    pipelined: bool,
    arguments: Vec<String>,
}

type JobKey = (Vec<Shape>, Vec<bool>, (i64, i64), bool, Vec<Vec<i64>>);

impl Job {
    fn key(&self) -> JobKey {
        (
            self.shapes.clone(),
            self.shared.clone(),
            self.stride,
            self.steady,
            self.copies.clone(),
        )
    }
}

/// What a composed pack runs: every core's jobs, its operands and options.
struct Schedule {
    sn: String,
    dn: String,
    source_bytes: i64,
    dest_bytes: i64,
    dtype: &'static str,
    streams: Vec<Vec<Job>>,
    options: Options,
}

/// `read_shape` and `read_batch`: the logical window a pack covers.
struct Window {
    pitch: i64,
    row0: i64,
    col0: i64,
    rows: i64,
    cols: i64,
    batches: i64,
    padded_rows: i64,
}

impl Window {
    fn read(body: &Body, op: &Op, env: &Env, elements: usize) -> Result<Window> {
        let st = &op.st;
        let err = |s: &str| fail(body, st, "L2.READ.001", s.to_owned());
        let Some(Term::Tuple(dims)) = st.kw("read_shape") else {
            return Err(err(
                "read_shape is (pitch, first row, first column, rows, columns)",
            ));
        };
        let [pitch, row0, col0, rows, cols] = dims.as_slice() else {
            return Err(err(
                "read_shape is (pitch, first row, first column, rows, columns)",
            ));
        };
        let pitch = const_of(body, st, pitch, env)?;
        let row0 = const_of(body, st, row0, env)?;
        let col0 = const_of(body, st, col0, env)?;
        let rows = const_of(body, st, rows, env)?;
        let cols = const_of(body, st, cols, env)?;
        let (batches, padded_rows) = if let Some(Term::Tuple(dims)) = st.kw("read_batch") {
            let [b, rp] = dims.as_slice() else {
                return Err(err("read_batch is (batches, padded rows)"));
            };
            (const_of(body, st, b, env)?, const_of(body, st, rp, env)?)
        } else {
            (1, rows)
        };
        if batches < 1 || padded_rows < rows || padded_rows % 4 != 0 {
            return Err(err(
                "read_batch has positive batch count and whole padded four-row groups",
            ));
        }
        if pitch <= 0
            || rows <= 0
            || cols <= 0
            || row0 < 0
            || col0 < 0
            || col0 + cols > pitch
            || ((row0 + batches * rows - 1) * pitch + col0 + cols) as usize > elements
        {
            return Err(err("read_shape exceeds the logical mapped operand"));
        }
        Ok(Window {
            pitch,
            row0,
            col0,
            rows,
            cols,
            batches,
            padded_rows,
        })
    }

    /// The logical positions of the 4x32 entry at `origin` (a value offset).
    fn entry(&self, origin: i64) -> Vec<i64> {
        let r = origin / self.pitch;
        let k = origin % self.pitch;
        (0..4)
            .flat_map(|i| {
                (0..32).map(move |j| {
                    let batch = (r + i) / self.padded_rows;
                    let local = (r + i) % self.padded_rows;
                    let batch_row = if on("l2.readmap/batch-base") {
                        0
                    } else {
                        batch * self.rows
                    };
                    if batch < self.batches && local < self.rows && k + j < self.cols {
                        (self.row0 + batch_row + local) * self.pitch + k + self.col0 + j
                    } else {
                        -1
                    }
                })
            })
            .collect()
    }
}

/// The flag of a `0` or `1` keyword, off when absent.
fn flag(body: &Body, op: &Op, env: &Env, key: &str) -> Result<bool> {
    match op.st.kw(key) {
        None => Ok(false),
        Some(k) => match const_of(body, &op.st, k, env)? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(fail(
                body,
                &op.st,
                "L2.READ.001",
                format!("{key} is 0 or 1"),
            )),
        },
    }
}

/// Group equal tiles: one pack, drained to every destination, and images that
/// share one source fetch. `None`: a lane program exceeds the machine's IMEM.
fn schedule(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    checked: bool,
) -> Result<Option<Schedule>> {
    let st = &op.st;
    let err = |s: &str| fail(body, st, "L2.READ.001", s.to_owned());
    let knobs = &tg.read;
    let map = ReadMap::read(body, op, env)?;
    let window = Window::read(body, op, env, map.elements.len())?;
    let args = st.positional();
    let dv = value(body, st, &as_view(args[0]), env)?;
    let sv = value(body, st, &as_view(args[2]), env)?;
    let dn = view_name(&dv);
    let sn = view_name(&sv);
    let db = buffer(body, st, &dn, "destination ")?;
    let sb = buffer(body, st, &sn, "source ")?;
    let dax = M::axes_of(&dv, &db.shape).map_err(|s| err(&s))?;
    let ((base, sl, dl), dest_flag) =
        xform::quant(db, &dax, 2 * window.pitch, tg).map_err(|s| err(&s))?;
    if sb.dtype != "f16"
        || sb.shape.iter().product::<i64>() < map.elements.iter().copied().max().unwrap_or(-1) + 1
    {
        return Err(err("read_map source does not cover original FP16 words"));
    }
    let mut equal = BTreeMap::<(Shape, i64), Vec<i64>>::new();
    for a in 0..sl[0].0 {
        for b in 0..sl[1].0 {
            for c in 0..sl[2].0 {
                for d in 0..sl[3].0 {
                    let origin = (a * sl[0].1 + b * sl[1].1 + c * sl[2].1 + d * sl[3].1) / 2;
                    let tile = map.tile(&window.entry(origin), knobs);
                    equal
                        .entry((tile.shape, tile.anchor))
                        .or_default()
                        .push(base + a * dl[0].1 + b * dl[1].1 + c * dl[2].1 + d * dl[3].1);
                }
            }
        }
    }
    let limit = tg.step();
    let reach = limit / 2;
    let imem = tg.machine.vc.imem_words as usize;
    let mut by_anchor_words = BTreeMap::<(i64, Vec<i64>), Vec<(Shape, Vec<i64>, i64)>>::new();
    for ((shape, anchor), mut destinations) in equal {
        destinations.sort();
        let max_fanout = if shape.words.is_empty() {
            knobs.fanout
        } else {
            imem / 32
        };
        let mut first = 0;
        while first < destinations.len() {
            let dest = destinations[first];
            let mut end = first + 1;
            while end < destinations.len()
                && end - first < max_fanout
                && destinations[end] - dest < reach
            {
                end += 1;
            }
            let copies = destinations[first..end]
                .iter()
                .map(|x| x - dest)
                .collect::<Vec<_>>();
            by_anchor_words
                .entry((anchor, shape.words.clone()))
                .or_default()
                .push((shape.clone(), copies, dest));
            first = end;
        }
    }
    let quant_cores = tg.machine.quant_cores() as usize;
    let image_entries = knobs.image_entries.min(tg.machine.vc.preds as usize);
    let total_items: usize = by_anchor_words.values().map(Vec::len).sum();
    let max_chunk = (total_items / quant_cores).clamp(1, image_entries);
    let mut buckets =
        BTreeMap::<(Vec<Shape>, Vec<Vec<i64>>), BTreeMap<(i64, Vec<i64>), SuperEntry>>::new();
    for ((anchor, _), mut items) in by_anchor_words {
        items.sort();
        let mut start = 0;
        while start < items.len() {
            let mut end = start + 1;
            let mut seg_sum = items[start].0.segments.len();
            let mut copy_sum = items[start].1.len();
            while end < items.len()
                && end - start < max_chunk
                && seg_sum + items[end].0.segments.len() <= knobs.image_segments
                && copy_sum + items[end].1.len() <= knobs.image_copies
            {
                seg_sum += items[end].0.segments.len();
                copy_sum += items[end].1.len();
                end += 1;
            }
            let chunk = &items[start..end];
            let shapes = chunk.iter().map(|x| x.0.clone()).collect::<Vec<_>>();
            let copies = chunk.iter().map(|x| x.1.clone()).collect::<Vec<_>>();
            let dests = chunk.iter().map(|x| x.2).collect::<Vec<_>>();
            buckets
                .entry((shapes.clone(), copies.clone()))
                .or_default()
                .insert(
                    (anchor, dests.clone()),
                    SuperEntry {
                        anchor,
                        shapes,
                        copies,
                        dests,
                    },
                );
            start = end;
        }
    }
    let mut entries = Vec::<SuperEntry>::new();
    let mut cuts = std::collections::BTreeSet::<usize>::new();
    for ((_, copies), mut bucket) in buckets {
        while let Some((first, _)) = bucket
            .first_key_value()
            .map(|(k, v)| (k.clone(), v.clone()))
        {
            let mut best = (1i64, (0i64, 0i64));
            for (next, _) in bucket.iter().skip(1).take(knobs.run_probe) {
                let d_src = next.0 - first.0;
                let d_dst = next.1[0] - first.1[0];
                if next.1.iter().zip(&first.1).any(|(a, b)| a - b != d_dst) {
                    continue;
                }
                if d_src.abs() >= limit
                    || d_dst.abs() >= limit
                    || copies
                        .iter()
                        .any(|c| (d_dst - c.last().copied().unwrap_or(0)).abs() >= limit)
                {
                    continue;
                }
                let mut n = 2i64;
                while n < knobs.run_limit {
                    let probe_key = (
                        first.0 + d_src * n,
                        first.1.iter().map(|d| d + d_dst * n).collect::<Vec<_>>(),
                    );
                    if !bucket.contains_key(&probe_key) {
                        break;
                    }
                    n += 1;
                }
                if n > best.0 {
                    best = (n, (d_src, d_dst));
                }
            }
            for i in 0..best.0 {
                let key = (
                    first.0 + best.1 .0 * i,
                    first
                        .1
                        .iter()
                        .map(|d| d + best.1 .1 * i)
                        .collect::<Vec<_>>(),
                );
                entries.push(bucket.remove(&key).unwrap());
            }
            cuts.insert(entries.len());
        }
    }
    let cores = tg.machine.quant_cores().min(entries.len() as i64).max(1) as usize;
    let overlap = flag(body, op, env, "overlap")?;
    let options = Options {
        classifier_overlap: flag(body, op, env, "classifier_overlap")?,
        pipeline: flag(body, op, env, "pipeline")?,
        zero_classifier: flag(body, op, env, "zero_classifier")?,
        lift: db.get("nk") >= 2
            && !on("l2.packing/unit-scale-zero")
            && !crate::mutant::before_rul099(),
    };
    let dtype = if dest_flag.is_empty() { "mx7a" } else { "mx7b" };
    let mut streams = Vec::new();
    for v in 0..cores {
        let mut stream = Vec::new();
        let start = entries.len() * v / cores;
        let end = entries.len() * (v + 1) / cores;
        let mut pos = start;
        while pos < end {
            let cut_end = end.min(*cuts.range(pos + 1..).next().unwrap());
            let entry0 = &entries[pos];
            let m_len = entry0.shapes.len();
            let probe_shared = (0..m_len).map(|m| m > 0).collect::<Vec<_>>();
            let probe = image(
                "mapped_probe",
                &Job {
                    shapes: entry0.shapes.clone(),
                    shared: probe_shared,
                    copies: entry0.copies.clone(),
                    stride: (0, 0),
                    steady: false,
                    pipelined: false,
                    arguments: Vec::new(),
                },
                dtype,
                checked,
                tg,
                options,
            );
            let module =
                crate::lower::vector::encode::read_image(&probe, "mapped_probe", tg.vregs)?;
            let words = crate::lower::vector::encode::words_of(
                &module,
                "mapped_probe",
                m_len + 2,
                &tg.machine,
            )?;
            let room = imem.saturating_sub(knobs.imem_margin) / words;
            if room == 0 {
                return Ok(None);
            }
            let arity = (image_entries / m_len)
                .min(tg.descriptors / (1 + m_len))
                .min(cut_end - pos)
                .min(room)
                .max(1);
            let repeats = ((cut_end - pos) / arity).min(knobs.image_trips);
            let pair = (0..arity)
                .map(|j| &entries[pos + j * repeats])
                .collect::<Vec<_>>();
            let stride = if repeats > 1 {
                (
                    entries[pos + 1].anchor - entries[pos].anchor,
                    entries[pos + 1].dests[0] - entries[pos].dests[0],
                )
            } else {
                (0, 0)
            };
            stream.push(Job {
                shapes: pair.iter().flat_map(|e| e.shapes.iter().cloned()).collect(),
                shared: (0..arity).flat_map(|_| (0..m_len).map(|m| m > 0)).collect(),
                copies: pair.iter().flat_map(|e| e.copies.iter().cloned()).collect(),
                stride,
                steady: repeats > 1,
                pipelined: overlap && words * arity * 2 + knobs.imem_margin <= imem,
                arguments: pair
                    .iter()
                    .flat_map(|e| {
                        std::iter::once(format!("{sn} + {}", e.anchor))
                            .chain(e.dests.iter().map(|&d| format!("{dn} + {d}")))
                    })
                    .chain(std::iter::once(repeats.to_string()))
                    .collect(),
            });
            pos += pair.len() * repeats;
        }
        streams.push(stream);
    }
    Ok(Some(Schedule {
        source_bytes: sb.shape.iter().product::<i64>() * 2,
        dest_bytes: db.nbytes(tg),
        sn,
        dn,
        dtype,
        streams,
        options,
    }))
}

/// The machine holds this composed pack: its layout, and every lane program in
/// its IMEM. A pack that does not fit reads materialized.
pub fn fits(body: &Body, op: &Op, env: &Env, tg: &Target, checked: bool) -> Result<bool> {
    Ok(readmap::fits_layout(tg) && schedule(body, op, env, tg, checked)?.is_some())
}

pub(super) fn quant(
    body: &Body,
    op: &Op,
    env: &Env,
    tg: &Target,
    images: &mut Vec<String>,
    status: Option<Finite>,
) -> Result<Stage> {
    let st = &op.st;
    let err = |s: &str| fail(body, st, "L2.READ.002", s.to_owned());
    if !readmap::fits_layout(tg) {
        return Err(err(
            "this machine reads materialized: its VC holds no composed layout",
        ));
    }
    let checked = status.is_some();
    let Some(plan) = schedule(body, op, env, tg, checked)? else {
        return Err(err(
            "this read is materialized: a tile's lane program exceeds IMEM",
        ));
    };
    let cores = plan.streams.len().max(1);
    let units = (0..cores).map(|i| format!("vc[{i}]")).collect::<Vec<_>>();
    if let Some(Finite::Deferred(s)) = &status {
        let need = STATUS_RECORD * cores as i64;
        if s.bytes < need {
            return Err(fail(
                body,
                st,
                "L2.MOV.019",
                format!(
                    "a quant status of {} bytes: its pieces' records take {need}",
                    s.bytes
                ),
            ));
        }
    }
    let tag = images.len();
    let mut lines = Vec::new();
    if checked {
        let name = format!("{}.read_clear_{tag}", body.name);
        images.push(image::clear(&name));
        for unit in &units {
            lines.extend([format!("send {unit}"), format!("    run {name}()")]);
        }
        lines.extend(units.iter().map(|unit| format!("wait {unit}")));
    }
    let mut reused = BTreeMap::<JobKey, String>::new();
    let mut waves = Vec::new();
    for (v, stream) in plan.streams.iter().enumerate() {
        let mut wave = Vec::new();
        for job in stream {
            let key = job.key();
            let name = if let Some(name) = reused.get(&key) {
                name.clone()
            } else {
                let name = format!("{}.read_pack_{tag}_{}", body.name, reused.len());
                images.push(image(&name, job, plan.dtype, checked, tg, plan.options));
                reused.insert(key, name.clone());
                name
            };
            wave.push(vec![
                format!("send vc[{v}]"),
                format!("    run {name}({})", job.arguments.join(", ")),
            ]);
        }
        waves.push(wave);
    }
    for wave in 0..waves.iter().map(Vec::len).max().unwrap_or(0) {
        for stream in &waves {
            if let Some(job) = stream.get(wave) {
                lines.extend(job.clone());
            }
        }
        lines.extend(units.iter().map(|unit| format!("wait {unit}")));
    }
    let mut accesses = Vec::new();
    if let Some(status) = &status {
        let name = format!("{}.read_status_{tag}", body.name);
        images.push(image::status(&name));
        finite_status(status, &name, &units, &mut lines, &mut accesses);
    }
    for unit in &units {
        for (write, name, size) in [
            (false, plan.sn.as_str(), plan.source_bytes),
            (true, plan.dn.as_str(), plan.dest_bytes),
        ] {
            let mut a = mem_of(write, span_of(name, 0, &[(size / 32, 32)], 32));
            a.unit = unit.clone();
            accesses.push(a);
        }
        accesses.push(Access::new(unit, true, (unit, "l1"), 0, readmap::L1_WORDS));
    }
    Ok(Stage::new(lines, units, accesses))
}
