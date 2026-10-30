//! The product through `@stage` slots (`Knobs::ring_depth`): a wave of row
//! groups at a time, the cores' epilogue the rows blocks of every rows kernel.

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
}

/// The ring `knobs` ask for on `mach`: at most `knobs.ring_depth` waves, as
/// many as the staging room holds.
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
    let depth = knobs.ring_depth.min(mach.stage_room() / wave);
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
/// the part, or with `tail_bands` the widest and a tail trip.
pub(super) fn segments(
    mm: &Mm,
    ring: &Ring,
    operands: &[Operand],
    out: &Operand,
    wc: i64,
    hw: &Hw,
) -> R<Vec<(i64, i64, v::Plan)>> {
    let part = ring.part_rows(mm);
    let planning = Mm {
        tm: if ring.tail_bands { MAX_TM } else { part },
        ..*mm
    };
    let rows = epilogue_bands(&planning, operands, out, wc, hw)? * hw.band;
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

/// The rows of a batch the ring's epilogue writes: through the end of the
/// last trip starting below M.
pub(super) fn cover(mm: &Mm, ring: &Ring, segs: &[(i64, i64, v::Plan)]) -> i64 {
    let part = ring.part_rows(mm);
    let mut end = 0;
    for tile in 0..mm.mp / mm.tm {
        for p in 0..ring.parts {
            for (start, count, pl) in segs {
                for k in 0..*count {
                    let s = tile * mm.tm + p * part + start + k * pl.rows;
                    if s < mm.m {
                        end = end.max(s + pl.rows);
                    }
                }
            }
        }
    }
    end
}

/// The ring's body: `b` the kernel's buffers (`a`, `b`, `in1..`, `out`).
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
    hw: &Hw,
) -> R<()> {
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
    let bufs = v::Bufs {
        ins,
        out: b[2 + extras.len()].clone(),
    };
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
    t.range_pipe(cdiv(ring.groups, ring.lanes), Some(ring.depth), |t, w| {
        let slot0 = w.rem(ring.depth).mul(ring.lanes);
        t.par(ncl, g::UnitKind::Mg, At::Var, |t, u| {
            t.range(per, |t, i| {
                let q = u.add(i.mul(ncl));
                let (lg, tl) = (q.div(ring.tiles), q.rem(ring.tiles));
                let gi = w.mul(ring.lanes).add(&lg);
                let work = |t: &mut Trace| {
                    let (bt, ti, tj0) = ring.group(mm, &gi);
                    let arow = bt.mul(mm.mp).add(ti.mul(mm.tm));
                    let srow = slot0.add(&lg).mul(mm.tm);
                    let c0 = tl.mul(mm.tn);
                    let dst = slots.at(&[sl(&srow, srow.add(mm.tm)), sl(&c0, c0.add(mm.tn))])?;
                    product(t, b, mm, &bt, &arow, &tj0.add(&tl), &dst, hw)
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
        // One phase a column block and segment: a vector instance runs one
        // stream loop.
        for cb in 0..pl.nblk {
            for (start, count, spl) in segs {
                t.par(cores, g::UnitKind::Vc, on, |t, u| {
                    let (lg, p) = (u.div(ring.parts), u.rem(ring.parts));
                    let gi = w.mul(ring.lanes).add(&lg);
                    let work = |t: &mut Trace| {
                        let (bt, ti, tj0) = ring.group(mm, &gi);
                        let grow = bt.mul(mm.mp).add(ti.mul(mm.tm));
                        let gcol = tj0.mul(mm.tn);
                        let srow = slot0.add(&lg).mul(mm.tm);
                        let trips = |t: &mut Trace, n: i64| -> R<()> {
                            let pro = v::prologue(t, &bufs, &operands, spl, cb, &gcol)?;
                            t.range(n, |t, k| {
                                let local = p.mul(part).add(*start).add(k.mul(spl.rows));
                                let mut at = v::Origin::rows(&grow.add(&local), operands.len());
                                for x in at.ins.iter_mut().skip(1) {
                                    x.1 = gcol.clone();
                                }
                                at.ins[0] = (srow.add(&local), Idx::Int(0));
                                at.out.1 = gcol.clone();
                                v::rows_block(t, &bufs, e, &operands, out, spl, &at, cb, &pro, hw)
                            })
                        };
                        if !past_m {
                            return trips(t, *count);
                        }
                        // The last M tile of a batch runs the trips starting
                        // below M: a count a part.
                        let last = mm.mp / mm.tm - 1;
                        if last > 0 {
                            t.when(Cond::new(&ti, g::CmpOp::Lt, last), |t| trips(t, *count))?;
                        }
                        t.when(Cond::new(&ti, g::CmpOp::Eq, last), |t| {
                            for pi in 0..ring.parts {
                                let first = last * mm.tm + pi * part + start;
                                let live = (0..*count)
                                    .filter(|k| first + k * spl.rows < mm.m)
                                    .count() as i64;
                                if live == 0 {
                                    continue;
                                }
                                if ring.parts > 1 {
                                    t.when(Cond::new(&p, g::CmpOp::Eq, pi), |t| trips(t, live))?;
                                } else {
                                    trips(t, live)?;
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
    })
}
