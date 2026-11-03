//! The product through `@stage` slots (`Knobs::ring_depth`): a wave of row
//! groups at a time, the cores' epilogue the rows blocks of every rows kernel;
//! an f16 A quantised a row tile at a time into slots of its own
//! (`Knobs::quant_ring`).

use super::*;

/// How the ring runs a contraction ([`resolve`]).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ring {
    /// Waves in flight (0: the product in DRAM, no ring).
    pub depth: i64,
    /// N tiles a row group holds: the whole row where the epilogue reduces
    /// it or writes a statistic, else one.
    pub tiles: i64,
    /// Row groups of the product, and a wave's.
    pub groups: i64,
    pub lanes: i64,
    /// Cores a row group's epilogue rows split over.
    pub parts: i64,
    pub tail_bands: bool,
    /// The f16 A's quant ring (`Knobs::quant_ring`, 0: none) and its row
    /// tile slots.
    pub quant: i64,
    pub qa_slots: i64,
}

/// The ring `knobs` ask for on `mach`: at most `knobs.ring_depth` waves, as
/// many as the staging room holds, none for an epilogue of no op; an f16 A's
/// quant ring, at most
/// `knobs.quant_ring`, where its slots fit the room the waves leave.
pub fn resolve(mm: &Mm, e: &E, out: &Operand, knobs: &Knobs, mach: &Machine) -> Ring {
    let hw = Hw::of(mach);
    let ntn = mm.n / mm.tn;
    let tiles = if expr::reduces(e).is_empty() && out.kind != Kind::Row {
        1
    } else {
        ntn
    };
    let groups = mm.bt * (mm.mp / mm.tm) * (ntn / tiles);
    let lanes = groups.min((hw.mg / tiles).max(1));
    let wave = 2 * lanes * mm.tm * tiles * mm.tn;
    // An epilogue of no op has no core work for the waves to overlap.
    let depth = match expr::count(e) {
        0 => 0,
        _ => knobs.ring_depth.min(mach.stage_room() / wave),
    };
    let row_tiles = mm.bt * (mm.mp / mm.tm);
    // Row tiles one wave of row groups reaches.
    let span = cdiv(lanes, groups / row_tiles) + 1;
    let slots = |q: i64| row_tiles.min(q * span);
    let left = mach.stage_room() - depth * wave;
    let quant = if depth > 0 && takes_f16(mm.k, mm.nk, knobs, mach) {
        (1..=knobs.quant_ring.min(2))
            .rev()
            .find(|q| mach.mx7_bytes(slots(*q) * mm.tm * mm.kp) <= left)
            .unwrap_or(0)
    } else {
        0
    };
    let parts = if knobs.split_vc && depth > 0 {
        (1..=(hw.vc / lanes).max(1))
            .rev()
            .find(|p| mm.tm % (p * mm.quantum) == 0)
            .expect("one part divides a tile")
    } else {
        1
    };
    Ring {
        depth,
        tiles,
        groups,
        lanes,
        parts,
        tail_bands: knobs.tail_bands && depth > 0,
        quant,
        qa_slots: slots(quant),
    }
}

impl Ring {
    pub fn py(&self) -> Py {
        let i = Py::Int;
        Py::Obj(
            "Ring",
            vec![
                ("depth", i(self.depth)),
                ("tiles", i(self.tiles)),
                ("lanes", i(self.lanes)),
                ("parts", i(self.parts)),
                ("tail_bands", Py::Bool(self.tail_bands)),
                ("quant", i(self.quant)),
            ],
        )
    }

    /// A row group's rows a core runs.
    fn part_rows(&self, mm: &Mm) -> i64 {
        mm.tm / self.parts
    }

    /// Batch, M tile and first N tile of row group `gi`.
    fn group(&self, mm: &Mm, gi: &Idx) -> (Idx, Idx, Idx) {
        let per_row = mm.n / mm.tn / self.tiles;
        let per_batch = (mm.mp / mm.tm) * per_row;
        let r = gi.rem(per_batch);
        (
            gi.div(per_batch),
            r.div(per_row),
            r.rem(per_row).mul(self.tiles),
        )
    }
}

/// A part's epilogue trips `(first row, trips, plan)`: the widest dividing
/// the part (at most `most` rows), or with `tail_bands` the widest and a tail trip.
pub(super) fn segments(
    mm: &Mm,
    ring: &Ring,
    operands: &[Operand],
    out: &Operand,
    wc: i64,
    most: Option<i64>,
    hw: &Hw,
) -> R<Vec<(i64, i64, v::Plan)>> {
    let part = ring.part_rows(mm);
    let planning = Mm {
        tm: if ring.tail_bands { MAX_TM } else { part },
        ..*mm
    };
    let widest = epilogue_bands(&planning, operands, out, wc, hw)? * hw.band;
    let rows = most.map_or(widest, |m| widest.min(m));
    let mut out_segments = Vec::new();
    let mut start = 0;
    while start < part {
        let r = rows.min(part - start);
        let count = (part - start) / r;
        let pl = v::plan(
            operands,
            out.kind,
            r,
            ring.tiles * mm.tn,
            Some(r / hw.band),
            Some(wc),
            mm.quantum,
            hw,
        )?;
        out_segments.push((start, count, pl));
        start += count * r;
    }
    Ok(out_segments)
}

/// The `(first row, rows)` of every trip of a batch the ring's epilogue runs:
/// those starting below M.
fn trips_run(mm: &Mm, ring: &Ring, segs: &[(i64, i64, v::Plan)]) -> Vec<(i64, i64)> {
    let part = ring.part_rows(mm);
    let mut out = vec![];
    for tile in 0..mm.mp / mm.tm {
        for p in 0..ring.parts {
            for (start, count, pl) in segs {
                for k in 0..*count {
                    let s = tile * mm.tm + p * part + start + k * pl.rows;
                    if s < mm.m {
                        out.push((s, pl.rows));
                    }
                }
            }
        }
    }
    out
}

/// The rows of a batch the ring's epilogue writes: through the end of the
/// last trip starting below M.
pub(super) fn cover(mm: &Mm, ring: &Ring, segs: &[(i64, i64, v::Plan)]) -> i64 {
    trips_run(mm, ring, segs)
        .iter()
        .map(|(s, r)| s + r)
        .max()
        .unwrap_or(0)
}

/// Whether the trips pack `d`: N whole chunks of its K blocks in blocks of `wc`, rp
/// past M, each trip one band (MXFP7 packs a band a trip) in rp and one packed tile.
pub(super) fn packs(
    mm: &Mm,
    ring: &Ring,
    segs: &[(i64, i64, v::Plan)],
    wc: i64,
    d: &QDemand,
    hw: &Hw,
) -> bool {
    let chunk = hw.kblock * d.nk;
    let tile = hw.band * d.groups;
    // A loop of trips stays in one packed tile: its store views step affinely.
    let part = ring.part_rows(mm);
    let loops_in_tile = (0..mm.mp / mm.tm).all(|ti| {
        (0..ring.parts).all(|p| {
            segs.iter().all(|(start, count, pl)| {
                let first = ti * mm.tm + p * part + start;
                let live = (0..*count).filter(|k| first + k * pl.rows < mm.m).count() as i64;
                live == 0 || first / tile == (first + live * pl.rows - 1) / tile
            })
        })
    });
    d.groups > 0
        && d.cp == mm.n
        && mm.n % chunk == 0
        && wc % chunk == 0
        && d.rp >= mm.m
        && d.rp % hw.band == 0
        && loops_in_tile
        && trips_run(mm, ring, segs)
            .iter()
            .all(|(s, r)| *r == hw.band && s + r <= d.rp)
}

/// The trip of the last row tile holding row M inside it, past its first
/// row: `(part, segment, trip, first row)`.
fn straddle(mm: &Mm, ring: &Ring, segs: &[(i64, i64, v::Plan)]) -> Option<(i64, usize, i64, i64)> {
    let (part, last) = (ring.part_rows(mm), mm.mp / mm.tm - 1);
    for p in 0..ring.parts {
        for (si, (start, count, pl)) in segs.iter().enumerate() {
            for k in 0..*count {
                let s = last * mm.tm + p * part + start + k * pl.rows;
                if s < mm.m && mm.m < s + pl.rows {
                    return Some((p, si, k, s));
                }
            }
        }
    }
    None
}

/// The ring's body: `b` the kernel's buffers (`a`, `b`, `in1..`, `out`, a `q`
/// a demand).
#[allow(clippy::too_many_arguments)]
pub(super) fn schedule(
    t: &mut Trace,
    b: &[Buf],
    mm: &Mm,
    e: &E,
    extras: &[Operand],
    out: &Operand,
    ring: &Ring,
    segs: &[(i64, i64, v::Plan)],
    demands: &[QDemand],
    mach: &Machine,
) -> R<()> {
    let hw = &Hw::of(mach);
    let wg = ring.tiles * mm.tn;
    let slots = t.buffer_in(
        &[ring.depth * ring.lanes * mm.tm, wg],
        g::Dtype::F16,
        vec![(
            "tile".to_string(),
            g::LayoutValue::Ints(vec![mm.tm / hw.band, mm.tn / hw.band]),
        )],
        g::BufSpace::Stage,
    )?;
    let mut operands = vec![Operand {
        kind: Kind::Tile,
        pitch: wg,
        rows: ring.depth * ring.lanes * mm.tm,
        ..Operand::new(Kind::Tile)
    }];
    operands.extend_from_slice(extras);
    let mut ins = vec![slots.clone()];
    ins.extend_from_slice(&b[2..2 + extras.len()]);
    let bufs = v::Bufs::plain(ins, b[2 + extras.len()].clone());
    let part = ring.part_rows(mm);
    let (start, count, pl) = segs.last().expect("a segment");
    let last =
        (mm.mp / mm.tm - 1) * mm.tm + (ring.parts - 1) * part + start + (count - 1) * pl.rows;
    let past_m = last >= mm.m;
    let per_wave = ring.lanes * ring.tiles;
    let ncl = hw.mg.min(per_wave);
    let per = cdiv(per_wave, ncl);
    let ragged = ring.groups % ring.lanes != 0;
    let cores = ring.lanes * ring.parts;
    let on = if cores > hw.vc { At::Any } else { At::Var };
    let first_q = 3 + extras.len();
    let qs = b[first_q..first_q + demands.len()].to_vec();
    // With packings the trip holding row M runs alone, its rows past M masked.
    let cut = match qs.is_empty() {
        true => None,
        false => straddle(mm, ring, segs),
    };
    let mut runs: Vec<(usize, bool)> = (0..segs.len()).map(|si| (si, false)).collect();
    runs.extend(cut.map(|(_, si, _, _)| (si, true)));
    let phases = pl.nblk * runs.len() as i64;
    let record = ktpu_machine::packing::STATUS_RECORD / 2;
    let waves = cdiv(ring.groups, ring.lanes);
    let status = match qs.is_empty() {
        true => None,
        false => Some(t.buffer(&[waves * phases * cores * record], g::Dtype::F16, vec![])?),
    };
    let mask = match cut {
        Some((_, si, _, s0)) => {
            let m = t.buffer(&[segs[si].2.rows, pl.wc], g::Dtype::F16, vec![])?;
            t.fill_value(&m.whole(), 0, None, None)?;
            t.fill_value(&m.at(&[sl(0, mm.m - s0), ALL])?, 0x3c00_3c00, None, None)?;
            Some(m.whole())
        }
        None => None,
    };
    let qa = quant_ring(t, b, mm, ring, mach)?;
    let mut bq = b.to_vec();
    if let Some(q) = &qa {
        bq[0] = q.slots.clone();
    }
    if let Some(q) = qa.as_ref().filter(|q| q.ahead == 1) {
        q.wave(t, &Idx::Int(0))?;
    }
    t.range_pipe(cdiv(ring.groups, ring.lanes), Some(ring.depth), |t, w| {
        let slot0 = w.rem(ring.depth).mul(ring.lanes);
        if let Some(q) = qa.as_ref().filter(|q| q.ahead == 0) {
            q.wave(t, &w)?;
        }
        t.par(ncl, g::UnitKind::Mg, At::Var, |t, u| {
            t.range(per, |t, i| {
                let q = u.add(i.mul(ncl));
                let (lg, tl) = (q.div(ring.tiles), q.rem(ring.tiles));
                let gi = w.mul(ring.lanes).add(&lg);
                let work = |t: &mut Trace| {
                    let (bt, ti, tj0) = ring.group(mm, &gi);
                    let arow = match &qa {
                        Some(q) => q.row(&bt.mul(mm.mp / mm.tm).add(&ti)),
                        None => bt.mul(mm.mp).add(ti.mul(mm.tm)),
                    };
                    let srow = slot0.add(&lg).mul(mm.tm);
                    let c0 = tl.mul(mm.tn);
                    let dst = slots.at(&[sl(&srow, srow.add(mm.tm)), sl(&c0, c0.add(mm.tn))])?;
                    product(t, &bq, mm, &bt, &arow, &tj0.add(&tl), &dst, hw)
                };
                let live = |t: &mut Trace| {
                    if ragged {
                        t.when(Cond::new(&gi, g::CmpOp::Lt, ring.groups), work)
                    } else {
                        work(t)
                    }
                };
                if per * ncl != per_wave {
                    t.when(Cond::new(&q, g::CmpOp::Lt, per_wave), live)
                } else {
                    live(t)
                }
            })
        })?;
        if let Some(q) = qa.as_ref().filter(|q| q.ahead == 1) {
            q.wave(t, &w.add(1))?;
        }
        // One phase a column block and segment (and the cut trip): a vector
        // instance runs one stream loop.
        let mut phase = 0;
        for cb in 0..pl.nblk {
            for &(si, masked) in &runs {
                let (start, count, spl) = &segs[si];
                let j = phase;
                phase += 1;
                let last = mm.mp / mm.tm - 1;
                // The last row tile's trips a part: those starting below M but the cut one.
                let lives: Vec<i64> = (0..ring.parts)
                    .map(|pi| {
                        let first = last * mm.tm + pi * part + start;
                        let live =
                            (0..*count).filter(|k| first + k * spl.rows < mm.m).count() as i64;
                        live - i64::from(cut.is_some_and(|(pc, sc, _, _)| pc == pi && sc == si))
                    })
                    .collect();
                let partial = past_m || cut.is_some();
                if !masked && partial && last == 0 && lives.iter().all(|l| *l == 0) {
                    continue;
                }
                t.par(cores, g::UnitKind::Vc, on, |t, u| {
                    let (lg, p) = (u.div(ring.parts), u.rem(ring.parts));
                    let gi = w.mul(ring.lanes).add(&lg);
                    let packs = match &status {
                        Some(s) => {
                            let rec = w.mul(phases).add(j).mul(cores).add(&u).mul(record);
                            Some(v::Packs {
                                q: qs.clone(),
                                status: s.at(&[sl(&rec, rec.add(record))])?,
                                mask: mask.clone().filter(|_| masked),
                            })
                        }
                        None => None,
                    };
                    let work = |t: &mut Trace| {
                        let (bt, ti, tj0) = ring.group(mm, &gi);
                        let grow = bt.mul(mm.mp).add(ti.mul(mm.tm));
                        let gcol = tj0.mul(mm.tn);
                        let srow = slot0.add(&lg).mul(mm.tm);
                        let trips = |t: &mut Trace, n: i64, k0: i64| -> R<()> {
                            let pro = v::prologue(t, &bufs, &operands, spl, cb, &gcol)?;
                            t.range(n, |t, k| {
                                let local = p.mul(part).add(*start).add(k.add(k0).mul(spl.rows));
                                let mut at = v::Origin::rows(&grow.add(&local), operands.len());
                                for x in at.ins.iter_mut().skip(1) {
                                    x.1 = gcol.clone();
                                }
                                at.ins[0] = (srow.add(&local), Idx::Int(0));
                                at.out.1 = gcol.clone();
                                match &packs {
                                    Some(pk) => v::rows_block_packed(
                                        t, &bufs, e, &operands, out, spl, &at, cb, &pro, hw, pk,
                                    ),
                                    None => v::rows_block(
                                        t, &bufs, e, &operands, out, spl, &at, cb, &pro, hw,
                                    ),
                                }
                            })
                        };
                        if masked {
                            let (pc, _, kc, _) = cut.expect("a cut trip");
                            return t.when(Cond::new(&ti, g::CmpOp::Eq, last), |t| {
                                if ring.parts > 1 {
                                    t.when(Cond::new(&p, g::CmpOp::Eq, pc), |t| trips(t, 1, kc))
                                } else {
                                    trips(t, 1, kc)
                                }
                            });
                        }
                        if !partial {
                            return trips(t, *count, 0);
                        }
                        if last > 0 {
                            t.when(Cond::new(&ti, g::CmpOp::Lt, last), |t| trips(t, *count, 0))?;
                        }
                        if lives.iter().all(|l| *l == 0) {
                            return Ok(());
                        }
                        t.when(Cond::new(&ti, g::CmpOp::Eq, last), |t| {
                            for (pi, &live) in lives.iter().enumerate() {
                                let pi = pi as i64;
                                if live == 0 {
                                    continue;
                                }
                                if ring.parts > 1 {
                                    t.when(Cond::new(&p, g::CmpOp::Eq, pi), |t| trips(t, live, 0))?;
                                } else {
                                    trips(t, live, 0)?;
                                }
                            }
                            Ok(())
                        })
                    };
                    if ragged {
                        t.when(Cond::new(&gi, g::CmpOp::Lt, ring.groups), work)
                    } else {
                        work(t)
                    }
                })?;
            }
        }
        Ok(())
    })?;
    if let Some(s) = &status {
        // Rows past the trips run, to rp: packed zeros.
        let end = cover(mm, ring, segs);
        for q in &qs {
            let rp = q.spec.shape[0];
            if end < rp {
                let z = t.buffer(&[rp - end, mm.n], g::Dtype::F16, vec![])?;
                t.fill_value(&z.whole(), 0, None, None)?;
                let dst = q.at(&[sl(end, rp), ALL])?;
                t.quant_scheduled(&dst, &z.whole(), true, QUANT_SCHEDULE, false)?;
            }
        }
        t.quant_check(&s.whole())?;
    }
    match qa.and_then(|q| q.status) {
        Some(s) => t.quant_check(&s.whole()),
        None => Ok(()),
    }
}

/// An f16 A's quant ring: row tile `rt` (batch-major) in slot
/// `rt % qa_slots`, quantised as the waves reach its first row group.
struct QuantRing<'a> {
    a: &'a Buf,
    slots: Buf,
    /// The deferred finite status, a slice a row tile (`ahead` 1).
    status: Option<Buf>,
    slice: i64,
    /// Waves the quant runs ahead of the product.
    ahead: i64,
    mm: &'a Mm,
    ring: &'a Ring,
}

/// The quant ring `ring` takes, or none.
fn quant_ring<'a>(
    t: &mut Trace,
    b: &'a [Buf],
    mm: &'a Mm,
    ring: &'a Ring,
    mach: &Machine,
) -> R<Option<QuantRing<'a>>> {
    if ring.quant == 0 {
        return Ok(None);
    }
    let hw = Hw::of(mach);
    let slots = t.buffer_in(
        &[ring.qa_slots * mm.tm, mm.kp],
        g::Dtype::Mx7a,
        Side::A.layout(mm.tm / hw.band, mm.nk),
        g::BufSpace::Stage,
    )?;
    let ntm = mm.mp / mm.tm;
    let slice = ktpu_machine::packing::quant_status_bytes(mach.quant_cores()) / 2;
    let status = if ring.quant == 2 {
        Some(t.buffer(&[mm.bt * ntm * slice], g::Dtype::F16, vec![])?)
    } else {
        None
    };
    Ok(Some(QuantRing {
        a: &b[0],
        slots,
        status,
        slice,
        ahead: ring.quant - 1,
        mm,
        ring,
    }))
}

impl QuantRing<'_> {
    /// Row tile `rt`'s first row in the slots.
    fn row(&self, rt: &Idx) -> Idx {
        rt.rem(self.ring.qa_slots).mul(self.mm.tm)
    }

    /// Row tile `rt` quantised into its slot: the rows of A it holds, the
    /// rest zero.
    fn tile(&self, t: &mut Trace, rt: &Idx) -> R<()> {
        let mm = self.mm;
        let ntm = mm.mp / mm.tm;
        let (bt, ti) = (rt.div(ntm), rt.rem(ntm));
        let slot = self.row(rt);
        let dst = self.slots.at(&[sl(&slot, slot.add(mm.tm)), ALL])?;
        let status = match &self.status {
            Some(s) => {
                let lo = rt.mul(self.slice);
                Some(s.at(&[sl(&lo, lo.add(self.slice))])?)
            }
            None => None,
        };
        let quant = |t: &mut Trace, rows: i64| -> R<()> {
            let r0 = bt.mul(mm.m).add(ti.mul(mm.tm));
            let src = self.a.at(&[sl(&r0, r0.add(rows)), ALL])?;
            quant_a(t, &dst, &src, rows < mm.tm || mm.k < mm.kp, status.as_ref())
        };
        let tail = mm.m - (ntm - 1) * mm.tm;
        if tail == mm.tm {
            return quant(t, mm.tm);
        }
        if ntm > 1 {
            t.when(Cond::new(&ti, g::CmpOp::Lt, ntm - 1), |t| quant(t, mm.tm))?;
        }
        t.when(Cond::new(&ti, g::CmpOp::Eq, ntm - 1), |t| quant(t, tail))
    }

    /// The row tiles wave `w`'s row groups start.
    fn wave(&self, t: &mut Trace, w: &Idx) -> R<()> {
        let ring = self.ring;
        let per_row = ring.groups / (self.mm.bt * (self.mm.mp / self.mm.tm));
        for j in 0..ring.lanes {
            let gi = w.mul(ring.lanes).add(j);
            let starts = |t: &mut Trace| {
                if per_row == 1 {
                    return self.tile(t, &gi);
                }
                t.when(Cond::new(&gi.rem(per_row), g::CmpOp::Eq, 0), |t| {
                    self.tile(t, &gi.div(per_row))
                })
            };
            t.when(Cond::new(&gi, g::CmpOp::Lt, ring.groups), starts)?;
        }
        Ok(())
    }
}
