//! Vector-core kernels (`kernels/vector.py`): an expression over operands.
//!
//! [`rows_kernel`]: [R, W] rows (W whole `mg.kblock`-column blocks) in GT4
//! bands of `mg.lanes` rows, column blocks of at most [`Hw::max_block`],
//! trips of up to [`TRIP_BANDS`] bands the L1 holds double-buffered
//! (API.ROW.001-.004, .007). [`flat_kernel`]: N
//! elements as rows of `vc.vlmax` values (API.ROW.006, .010).

use super::relayout::Logical;
use super::{cdiv, Hw};
use crate::expr::{self, E};
use crate::kernel::{build, Kernel, Port};
use crate::mutants;
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, sl, spec, ALL};
use crate::trace::control::At;
use crate::trace::vector::MappedLoad;
use crate::trace::{fail, Arg, Buf, Idx, Trace, View, R, V};
use ktpu_graph as g;
use ktpu_isa::unit::vector::DIM_STRIDE_MAX;
use ktpu_machine::Machine;
use std::collections::HashMap;

/// L1 words a rows trip leaves free beside the operands (landing, slack).
pub const L1_MARGIN: i64 = 16;
/// Bands a rows trip takes at most: stack policy, not a unit fact.
pub const TRIP_BANDS: i64 = 4;

/// The widest column block a plan searches from.
fn max_block(hw: &Hw) -> i64 {
    if mutants::on("api.rows/max_block") {
        4096
    } else {
        hw.max_block()
    }
}

/// How a kernel reads one buffer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// An [R, W] view, rows `pitch` apart.
    Full,
    /// The same of a cluster's drained sub-tiles.
    Tile,
    /// One [W] row every row reads.
    Col,
    /// A statistic: R values, a word's lanes a word.
    Row,
    /// One value, every row's.
    Scalar,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Full => "full",
            Kind::Tile => "tile",
            Kind::Col => "col",
            Kind::Row => "row",
            Kind::Scalar => "scalar",
        }
    }

    pub fn parse(s: &str) -> Option<Kind> {
        [Kind::Full, Kind::Tile, Kind::Col, Kind::Row, Kind::Scalar]
            .into_iter()
            .find(|k| k.name() == s)
    }
}

/// How a kernel reads one buffer, in elements of it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Operand {
    pub kind: Kind,
    pub dtype: g::Dtype,
    /// Element of row 0, column 0 (row, scalar: the value).
    pub offset: i64,
    /// Elements between rows (full, tile).
    pub pitch: i64,
    /// Rows the buffer holds past the offset (its declared extent).
    pub rows: i64,
}

impl Operand {
    pub fn new(kind: Kind) -> Operand {
        Operand {
            kind,
            dtype: g::Dtype::F16,
            offset: 0,
            pitch: 0,
            rows: 0,
        }
    }

    pub fn full(pitch: i64, rows: i64) -> Operand {
        Operand {
            pitch,
            rows,
            ..Operand::new(Kind::Full)
        }
    }

    pub fn py(&self) -> Py {
        Py::Obj(
            "Operand",
            vec![
                ("kind", Py::str(self.kind.name())),
                ("dtype", Py::str(self.dtype.name())),
                ("offset", Py::Int(self.offset)),
                ("pitch", Py::Int(self.pitch)),
                ("rows", Py::Int(self.rows)),
            ],
        )
    }

    pub(super) fn rows_major(&self) -> bool {
        matches!(self.kind, Kind::Full | Kind::Tile)
    }

    /// Its rows lie further apart than a descriptor's stride reaches.
    pub(super) fn wide(&self) -> bool {
        self.rows_major() && self.pitch * Hw::item(self.dtype) > DIM_STRIDE_MAX
    }

    fn divmod(&self) -> R<(i64, i64)> {
        if self.pitch <= 0 {
            return fail(
                "API.READ.001",
                format!("a {} read with pitch {}", self.kind.name(), self.pitch),
            );
        }
        Ok((
            self.offset.div_euclid(self.pitch),
            self.offset.rem_euclid(self.pitch),
        ))
    }

    /// The logical strides of a row and a column of its read: a statistic
    /// a value a row, a column one row repeated, a scalar one value.
    pub fn logical_strides(&self) -> (i64, i64) {
        match self.kind {
            Kind::Full | Kind::Tile => (self.pitch, 1),
            Kind::Row => (1, 0),
            Kind::Col => (0, 1),
            Kind::Scalar => (0, 0),
        }
    }
}

/// How a rows stage covers [R, W]: column blocks of `wc` (`nblk`), trips
/// of `rows` rows (`nb` trips, `rp` rows with the tail).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Plan {
    pub wc: i64,
    pub nblk: i64,
    pub rows: i64,
    pub nb: i64,
    pub rp: i64,
}

impl Plan {
    pub fn py(&self) -> Py {
        Py::Obj(
            "Plan",
            vec![
                ("wc", Py::Int(self.wc)),
                ("nblk", Py::Int(self.nblk)),
                ("rows", Py::Int(self.rows)),
                ("nb", Py::Int(self.nb)),
                ("rp", Py::Int(self.rp)),
            ],
        )
    }
}

pub fn holds_stat(operands: &[Operand], out: Kind) -> bool {
    out == Kind::Row || operands.iter().any(|o| o.kind == Kind::Row)
}

/// Whole words of R statistic values.
pub fn stat_words(r: i64, hw: &Hw) -> i64 {
    cdiv(r, hw.stat_rows())
}

/// How trips are dealt over cores (RUL.016).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Trips {
    /// Core c starts at trip `c * h` (`h = ceil(n / cores)`) and runs h,
    /// the last the rest.
    HChunk,
    /// Core c starts at trip `c q + ceil(c rem / cores)` (`q, rem =
    /// divmod(n, cores)`): counts differ by at most one.
    Balanced,
}

/// The reverse mutant of whole rows a band: a reduction across column blocks
/// planned as block partials again.
pub const BLOCK_PARTIALS_MUTANT: &str = "vector/block-partials";

/// The reverse mutant of the [`Knobs::trips`] default: the other split.
pub const OTHER_SPLIT_MUTANT: &str = "vector/other-split";

/// The vector generators' knobs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Knobs {
    /// The split every generator deals its trips by.
    pub trips: Trips,
}

/// The one source every generator and Python (`_rs.VECTOR_DEFAULTS`) read:
/// balanced, every core given work (RUL.016).
pub fn defaults() -> Knobs {
    let (chosen, other) = (Trips::Balanced, Trips::HChunk);
    Knobs {
        trips: if mutants::on(OTHER_SPLIT_MUTANT) {
            other
        } else {
            chosen
        },
    }
}

/// `n` trips over at most `cores` cores by [`defaults`]' split, as indices
/// of the core's variable.
pub struct Split {
    pub cores: i64,
    trips: Trips,
    /// The h-chunk's trips a core.
    h: i64,
    n: i64,
}

/// Balanced tile boundary for participant `p`, including the final boundary.
pub fn balanced_cut(n: i64, p: &Idx, participants: i64) -> Idx {
    let (q, rem) = (n / participants, n % participants);
    p.mul(q)
        .add(p.mul(rem).add(participants - 1).div(participants))
}

impl Split {
    pub fn start(&self, c: &Idx) -> Idx {
        if self.cores == 1 {
            return Idx::Int(0);
        }
        match self.trips {
            Trips::HChunk => c.mul(self.h),
            Trips::Balanced => balanced_cut(self.n, c, self.cores),
        }
    }

    pub fn trips(&self, c: &Idx) -> Idx {
        if self.cores == 1 {
            return Idx::Int(self.n);
        }
        match (self.trips, self.cores) {
            (Trips::HChunk, 2) => Idx::Int(self.h).add(c.mul(self.n - 2 * self.h)),
            (Trips::HChunk, m) => Idx::Int(self.h).add(c.div(m - 1).mul(self.n - m * self.h)),
            (Trips::Balanced, _) => self.start(&c.add(1)).sub(self.start(c)),
        }
    }
}

pub fn split(n: i64, cores: i64) -> Split {
    let trips = defaults().trips;
    let cores = if mutants::on("api.vector/split_one") {
        1
    } else {
        cores.min(n).max(1)
    };
    if cores == 1 {
        return Split {
            cores: 1,
            trips,
            h: n,
            n,
        };
    }
    let h = cdiv(n, cores);
    let cores = match trips {
        Trips::HChunk => cdiv(n, h),
        Trips::Balanced => cores,
    };
    Split { cores, trips, h, n }
}

/// The widest column block, [`Hw::max_block`] halving down to one GT4
/// block, at most `most`, dividing `w`.
pub fn block(w: i64, most: i64, hw: &Hw) -> R<i64> {
    let mut wc = if mutants::on("isa.cluster/plan") {
        hw.kblock
    } else {
        hw.max_block()
    };
    while wc >= hw.kblock {
        if wc <= most && w % wc == 0 {
            return Ok(wc);
        }
        wc /= 2;
    }
    fail(
        "API.ROW.008",
        format!("{w} columns do not split into blocks of {}", hw.kblock),
    )
}

/// Bands of `mg.lanes` rows x `wc` columns the L1 holds double-buffered
/// beside the prologue (a phase is one column block of W).
pub fn bands_fit(operands: &[Operand], out: Kind, wc: i64, hw: &Hw) -> i64 {
    if mutants::on("api.vector/bands_one") {
        return 1;
    }
    let band = hw.band * wc * 2 / hw.word_bytes;
    let mut words: i64 = operands
        .iter()
        .filter(|o| o.rows_major())
        .map(|_| band)
        .sum();
    words += operands
        .iter()
        .filter(|o| matches!(o.kind, Kind::Row | Kind::Scalar))
        .count() as i64;
    words += if out == Kind::Row { 1 } else { band };
    let prologue: i64 = operands
        .iter()
        .filter(|o| o.kind == Kind::Col)
        .map(|_| wc * 2 / hw.word_bytes)
        .sum();
    let room = hw.l1_words - L1_MARGIN - prologue;
    if words == 0 {
        TRIP_BANDS
    } else {
        room.div_euclid(2 * words)
    }
}

/// The trips and blocks of a rows stage (API.ROW.002): as many bands a
/// trip as the L1 holds double-buffered (at most [`TRIP_BANDS`]), `quantum` rows
/// dividing a trip; unless `wc` is given the column block narrows until
/// the bands of `quantum` rows of every operand fit.
#[allow(clippy::too_many_arguments)]
pub fn plan(
    operands: &[Operand],
    out: Kind,
    r: i64,
    w: i64,
    bands: Option<i64>,
    wc: Option<i64>,
    quantum: i64,
    hw: &Hw,
) -> R<Plan> {
    if w % hw.kblock != 0 {
        return fail(
            "API.ROW.008",
            format!("a GT4 row is whole {}-column blocks, not {w}", hw.kblock),
        );
    }
    let need = (quantum / hw.band).max(1);
    let wc = match wc {
        Some(wc) => wc,
        None => {
            let widest = if w <= max_block(hw) {
                w
            } else {
                block(w, max_block(hw), hw)?
            };
            let blocks: Vec<i64> = (1..=widest / hw.kblock)
                .rev()
                .map(|k| k * hw.kblock)
                .filter(|b| w % b == 0)
                .collect();
            blocks
                .iter()
                .copied()
                .find(|b| bands_fit(operands, out, *b, hw) >= need)
                .unwrap_or(*blocks.last().expect("a block"))
        }
    };
    let fit = bands_fit(operands, out, wc, hw);
    if fit < need {
        return fail(
            "API.ROW.002",
            format!("{need} bands of {wc} columns do not fit a core's L1"),
        );
    }
    let b = match bands {
        Some(b) if b != 0 => b.min(fit),
        _ => fit.min(TRIP_BANDS),
    }
    .max(need);
    // Whole quantums within the bands that fit: rounding up would overrun the L1.
    let rows = (hw.band * b / quantum).max(1) * quantum;
    let nb = cdiv(r, rows);
    Ok(Plan {
        wc,
        nblk: w / wc,
        rows,
        nb,
        rp: nb * rows,
    })
}

/// `(plan, first row, rows)`: the trips of whole `pl.rows`, then the rest
/// in one trip of whole `quantum`s, then (R not a multiple of `quantum`)
/// one band of `quantum` rows holding the last `rows` (None: every row of
/// the stage is one of R) (API.ROW.004).
pub fn stages(pl: &Plan, r: i64, quantum: i64) -> Vec<(Plan, i64, Option<i64>)> {
    if mutants::on("api.vector/stages_whole") {
        return vec![(*pl, 0, None)];
    }
    let (full, rem) = (r / pl.rows, r % pl.rows);
    let mut out = vec![];
    let with = |rows: i64, nb: i64| Plan {
        rows,
        nb,
        rp: nb * rows,
        ..*pl
    };
    if full > 0 {
        out.push((with(pl.rows, full), 0, None));
    }
    let whole = rem / quantum * quantum;
    if whole > 0 {
        out.push((with(whole, 1), full * pl.rows, None));
    }
    if rem % quantum != 0 {
        out.push((
            with(quantum, 1),
            full * pl.rows + whole,
            Some(rem % quantum),
        ));
    }
    out
}

/// Rows a rows kernel over R rows writes past R.
pub fn overrun(r: i64, quantum: i64) -> i64 {
    cdiv(r, quantum) * quantum - r
}

/// An operand's buffer extents: [rows, pitch] covering its view (rows past
/// R the allocation's tail), a [W] row, a statistic's words, a scalar's
/// word.
pub fn port_shape(o: &Operand, rp: i64, w: i64, hw: &Hw) -> R<Vec<i64>> {
    let sr = hw.stat_rows();
    let mut shape = match o.kind {
        Kind::Full | Kind::Tile => {
            let (r0, _) = o.divmod()?;
            vec![o.rows.max(r0 + rp), o.pitch]
        }
        Kind::Col => vec![o.offset + w],
        Kind::Row => vec![o.rows.max(stat_words(o.offset + rp, hw) * sr)],
        Kind::Scalar => vec![o.rows.max(o.offset.div_euclid(sr) * sr + sr)],
    };
    if mutants::on("api.vector/port_pad") {
        *shape.last_mut().expect("a dim") += 32;
    }
    Ok(shape)
}

/// A composed operand's read: its `read_map` steps, the real rows of a band
/// and columns of a block when only some are.
#[derive(Clone, Debug)]
pub struct Mapped {
    pub steps: g::Attr,
    pub rows: Option<i64>,
    pub cols: Option<i64>,
}

/// The rows kernel's buffers: `in0..` and `out`; an operand of `maps` reads
/// its buffer's source values.
pub struct Bufs {
    pub ins: Vec<Buf>,
    pub out: Buf,
    pub maps: HashMap<usize, Mapped>,
}

impl Bufs {
    /// Every operand read as it lies.
    pub fn plain(ins: Vec<Buf>, out: Buf) -> Bufs {
        Bufs {
            ins,
            out,
            maps: HashMap::new(),
        }
    }

    /// The same reads into `out`.
    pub fn to(&self, out: &Buf) -> Bufs {
        Bufs {
            ins: self.ins.clone(),
            out: out.clone(),
            maps: self.maps.clone(),
        }
    }
}

/// The reverse mutant of [`rows_trips`]: a band stores its first member alone.
pub const LOST_MEMBER_MUTANT: &str = "fused/lost-member";

/// One expression of a band and where it goes: `dst` read as `out` (a
/// [`Bufs`]' `out` is not read where members are given).
#[derive(Clone)]
pub struct Member<'a> {
    pub e: &'a E,
    pub out: Operand,
    pub dst: Buf,
}

impl<'a> Member<'a> {
    /// `e` alone into `bufs.out`.
    pub fn sole(e: &'a E, out: &Operand, bufs: &Bufs) -> [Member<'a>; 1] {
        [Member {
            e,
            out: *out,
            dst: bufs.out.clone(),
        }]
    }
}

/// One rows stage: the rectangle of row trips by column blocks over the cores
/// ([`rectangle`]). A tiled operand's trips and blocks each lie in one tile.
#[allow(clippy::too_many_arguments)]
pub fn rows_stage(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    pl: &Plan,
    cores: i64,
    row0: &Idx,
    hw: &Hw,
) -> R<()> {
    if out.wide() || operands.iter().any(Operand::wide) {
        return narrow_stage(t, bufs, e, operands, out, pl, cores, row0, hw);
    }
    let plain = in_place(row0, operands.len());
    let sole = Member::sole(e, out, bufs);
    rectangle(t, bufs, &sole, operands, pl, cores, 0, pl.nblk, &plain, hw)
}

/// [`rows_stage`] of several members over one domain: a band reads each operand
/// once and stores every member; past a descriptor's stride a member at a time.
#[allow(clippy::too_many_arguments)]
pub fn members_stage(
    t: &mut Trace,
    bufs: &Bufs,
    members: &[Member],
    operands: &[Operand],
    pl: &Plan,
    cores: i64,
    row0: &Idx,
    hw: &Hw,
) -> R<()> {
    if members.iter().any(|m| m.out.wide()) || operands.iter().any(Operand::wide) {
        for m in members {
            let b = bufs.to(&m.dst);
            rows_stage(t, &b, m.e, operands, &m.out, pl, cores, row0, hw)?;
        }
        return Ok(());
    }
    let plain = in_place(row0, operands.len());
    rectangle(
        t, bufs, members, operands, pl, cores, 0, pl.nblk, &plain, hw,
    )
}

/// Each of `n` operands and the output at the same row (from `row0`) and column.
fn in_place(row0: &Idx, n: usize) -> impl Fn(&Idx, &Idx) -> Origin + '_ {
    move |r, col| {
        let row = r.add(row0);
        Origin {
            ins: vec![(row.clone(), col.clone()); n],
            out: (row, col.clone()),
        }
    }
}

/// [`rows_stage`] over a buffer tiled every `tile_rows` rows: a trip's rows
/// step affinely only inside a tile, so past one tile row of several column
/// blocks each instance takes a tile row.
#[allow(clippy::too_many_arguments)]
pub fn tile_stage(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    pl: &Plan,
    cores: i64,
    row0: &Idx,
    tile_rows: i64,
    hw: &Hw,
) -> R<()> {
    if pl.nblk == 1 || pl.rp <= tile_rows {
        return rows_stage(t, bufs, e, operands, out, pl, cores, row0, hw);
    }
    let plain = in_place(row0, operands.len());
    let sole = Member::sole(e, out, bufs);
    let per = tile_rows / pl.rows;
    let n = cdiv(pl.nb, per);
    let at = if n > cores { At::Any } else { At::Var };
    for cb in 0..pl.nblk {
        t.par(n, g::UnitKind::Vc, at, |t, u| {
            let (first, trips, cb) = (u.mul(per), Idx::Int(per), Idx::Int(cb));
            rows_trips(t, bufs, &sole, operands, pl, &first, trips, &cb, &plain, hw)
        })?;
    }
    Ok(())
}

/// Row trips by column blocks `b0..b0 + blocks`, one stream loop an instance:
/// a phase a block with its rows split over the cores, or where blocks reach
/// more cores than rows do, a phase a core's worth of blocks, whole rows each.
#[allow(clippy::too_many_arguments)]
fn rectangle(
    t: &mut Trace,
    bufs: &Bufs,
    members: &[Member],
    operands: &[Operand],
    pl: &Plan,
    cores: i64,
    b0: i64,
    blocks: i64,
    origin: &dyn Fn(&Idx, &Idx) -> Origin,
    hw: &Hw,
) -> R<()> {
    let end = b0 + blocks;
    let (by_rows, by_blocks) = (split(pl.nb, cores), split(blocks, cores));
    if by_blocks.cores > by_rows.cores {
        let mut next = b0;
        while next < end {
            let n = by_blocks.cores.min(end - next);
            t.par(n, g::UnitKind::Vc, At::Var, |t, c| {
                let cb = c.add(next);
                let all = Idx::Int(pl.nb);
                rows_trips(
                    t,
                    bufs,
                    members,
                    operands,
                    pl,
                    &Idx::Int(0),
                    all,
                    &cb,
                    origin,
                    hw,
                )
            })?;
            next += n;
        }
        return Ok(());
    }
    for cb in b0..end {
        t.par(by_rows.cores, g::UnitKind::Vc, At::Var, |t, c| {
            let (first, trips) = (by_rows.start(&c), by_rows.trips(&c));
            rows_trips(
                t,
                bufs,
                members,
                operands,
                pl,
                &first,
                trips,
                &Idx::Int(cb),
                origin,
                hw,
            )
        })?;
    }
    Ok(())
}

/// `o`'s rows of `buf` as a composed read of themselves: a descriptor rebased
/// a row where no stride reaches the next.
fn own_rows(o: &Operand, buf: &Buf) -> Mapped {
    let shape = &buf.spec.shape;
    let (rows, n) = (shape[0], shape.iter().product::<i64>());
    let walk = vec![(rows, o.pitch), (o.pitch, 1)];
    let steps = [Logical {
        n,
        at: 0,
        dat: 0,
        m: n,
        walk: walk.clone(),
        dwalk: walk,
    }];
    Mapped {
        steps: g::Attr::Tuple(steps.iter().map(Logical::attr).collect()),
        rows: None,
        cols: None,
    }
}

/// A rows stage past a descriptor's stride: f16 operands read composed, others
/// copied into narrow scratch a stride's reach of column blocks at a time, a
/// wide output out.
#[allow(clippy::too_many_arguments)]
fn narrow_stage(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    pl: &Plan,
    cores: i64,
    row0: &Idx,
    hw: &Hw,
) -> R<()> {
    let widest = operands
        .iter()
        .chain([out])
        .filter(|o| o.wide())
        .map(|o| Hw::item(o.dtype))
        .max()
        .expect("a wide operand");
    let per = (DIM_STRIDE_MAX / widest / pl.wc).min(pl.nblk);
    let pitch = per * pl.wc;
    let narrow = |o: &Operand| Operand {
        offset: 0,
        pitch,
        rows: pl.rp,
        ..*o
    };
    let mut maps = bufs.maps.clone();
    for (i, o) in operands.iter().enumerate() {
        if o.wide() && o.kind == Kind::Full && o.dtype == g::Dtype::F16 {
            maps.entry(i).or_insert_with(|| own_rows(o, &bufs.ins[i]));
        }
    }
    let moved: Vec<usize> = (0..operands.len())
        .filter(|i| operands[*i].wide() && !maps.contains_key(i))
        .collect();
    let mut ins = bufs.ins.clone();
    let mut ops = operands.to_vec();
    for &i in &moved {
        ins[i] = t.buffer(&[pl.rp, pitch], operands[i].dtype, vec![])?;
        ops[i] = narrow(&operands[i]);
    }
    let (dst, tail) = if out.wide() {
        let s = t.buffer(&[pl.rp, pitch], out.dtype, vec![])?;
        (s, narrow(out))
    } else {
        (bufs.out.clone(), *out)
    };
    let inner = Bufs {
        ins,
        out: dst.clone(),
        maps,
    };
    for start in (0..pl.nblk).step_by(per as usize) {
        let end = (start + per).min(pl.nblk);
        let cols = (end - start) * pl.wc;
        let col = start * pl.wc;
        for &i in &moved {
            let (rr, cc) = operands[i].divmod()?;
            let lo = row0.add(rr);
            let src = bufs.ins[i].at(&[sl(&lo, lo.add(pl.rp)), sl(cc + col, cc + col + cols)])?;
            let s = inner.ins[i].at(&[sl(0, pl.rp), sl(0, cols)])?;
            t.copy(&s, &src, None, None)?;
        }
        let origin = |r: &Idx, c: &Idx| Origin {
            ins: (0..operands.len())
                .map(|i| match moved.contains(&i) {
                    true => (r.clone(), c.add(-col)),
                    false => (r.add(row0), c.clone()),
                })
                .collect(),
            out: match out.wide() {
                true => (r.clone(), c.add(-col)),
                false => (r.add(row0), c.clone()),
            },
        };
        let (blocks, sole) = (end - start, Member::sole(e, &tail, &inner));
        rectangle(
            t, &inner, &sole, &ops, pl, cores, start, blocks, &origin, hw,
        )?;
        if out.wide() {
            let (rr, cc) = out.divmod()?;
            let lo = row0.add(rr);
            let d = bufs
                .out
                .at(&[sl(&lo, lo.add(pl.rp)), sl(cc + col, cc + col + cols)])?;
            t.copy(&d, &dst.at(&[sl(0, pl.rp), sl(0, cols)])?, None, None)?;
        }
    }
    Ok(())
}

/// An instance's trips of column block `cb` from band `first`: the prologue (each
/// `col` operand's block), then a band a trip at `origin` storing every member.
#[allow(clippy::too_many_arguments)]
fn rows_trips(
    t: &mut Trace,
    bufs: &Bufs,
    members: &[Member],
    operands: &[Operand],
    pl: &Plan,
    first: &Idx,
    trips: Idx,
    cb: &Idx,
    origin: &dyn Fn(&Idx, &Idx) -> Origin,
    hw: &Hw,
) -> R<()> {
    let col = cb.mul(pl.wc);
    let pro = prologue(t, bufs, operands, pl, 0, &col)?;
    let stored = if mutants::on(LOST_MEMBER_MUTANT) {
        1
    } else {
        members.len()
    };
    t.range(trips, |t, k| {
        let at = origin(&first.mul(pl.rows).add(k.mul(pl.rows)), &col);
        let vals = band_values(t, bufs, operands, pl, &at, 0, &pro, hw)?;
        for m in &members[..stored] {
            let x = expr::emit(t, m.e, &vals)?;
            band_store(t, &m.dst, &m.out, pl, &at, 0, x)?;
        }
        Ok(())
    })
}

/// Each `col` operand's column block `cb` of a window from column `col`.
pub fn prologue(
    t: &mut Trace,
    bufs: &Bufs,
    operands: &[Operand],
    pl: &Plan,
    cb: i64,
    col: &Idx,
) -> R<HashMap<usize, Arg>> {
    let mut pro = HashMap::new();
    for (i, o) in operands.iter().enumerate() {
        if o.kind == Kind::Col && !bufs.maps.contains_key(&i) {
            let lo = col.add(o.offset + cb * pl.wc);
            let l = t.load(&bufs.ins[i].at(&[sl(&lo, lo.add(pl.wc))])?, true)?;
            pro.insert(i, Arg::Val(t.unpack(l, None)?));
        }
    }
    Ok(pro)
}

/// Where a band lies: each operand's and the output's (row, column) in its
/// buffer before the operand's own offset.
pub struct Origin {
    pub ins: Vec<(Idx, Idx)>,
    pub out: (Idx, Idx),
}

impl Origin {
    /// Row `r`, column 0 of every buffer.
    pub fn rows(r: &Idx, n: usize) -> Origin {
        Origin {
            ins: vec![(r.clone(), Idx::Int(0)); n],
            out: (r.clone(), Idx::Int(0)),
        }
    }
}

/// One band of rows at `at`, column block `cb`.
#[allow(clippy::too_many_arguments)]
pub fn rows_block(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    pl: &Plan,
    at: &Origin,
    cb: i64,
    pro: &HashMap<usize, Arg>,
    hw: &Hw,
) -> R<()> {
    let vals = band_values(t, bufs, operands, pl, at, cb, pro, hw)?;
    let v = expr::emit(t, e, &vals)?;
    band_store(t, &bufs.out, out, pl, at, cb, v)
}

/// Every operand's value in one band of rows at `at`, column block `cb`,
/// `pro` the prologue's.
#[allow(clippy::too_many_arguments)]
pub(super) fn band_values(
    t: &mut Trace,
    bufs: &Bufs,
    operands: &[Operand],
    pl: &Plan,
    at: &Origin,
    cb: i64,
    pro: &HashMap<usize, Arg>,
    hw: &Hw,
) -> R<HashMap<usize, Arg>> {
    let col0 = cb * pl.wc;
    let sr = hw.stat_rows();
    let mut vals = pro.clone();
    for (i, o) in operands.iter().enumerate() {
        let buf = &bufs.ins[i];
        let (r, c) = &at.ins[i];
        if let Some(mapped) = bufs.maps.get(&i) {
            let (pitch, stride) = o.logical_strides();
            let read = MappedLoad {
                steps: mapped.steps.clone(),
                at: r
                    .mul(pitch)
                    .add(c.mul(stride))
                    .add(o.offset + col0 * stride),
                pitch,
                stride,
                rows: mapped.rows,
                cols: mapped.cols,
            };
            let shape = vec![pl.rows, pl.wc];
            vals.insert(i, Arg::Val(t.load_mapped(buf, &read, shape)?));
            continue;
        }
        match o.kind {
            Kind::Full | Kind::Tile => {
                let (rr, cc) = o.divmod()?;
                let lo = Idx::Int(rr).add(r);
                let c = c.add(cc + col0);
                let view = buf.at(&[sl(&lo, lo.add(pl.rows)), sl(&c, c.add(pl.wc))])?;
                vals.insert(i, Arg::Val(t.load(&view, true)?));
            }
            Kind::Row => {
                let lo = Idx::Int(o.offset).add(r);
                let view = buf.at(&[sl(&lo, lo.add(pl.rows))])?;
                vals.insert(i, Arg::Val(t.load(&view, false)?));
            }
            Kind::Scalar => {
                let w0 = o.offset.div_euclid(sr) * sr;
                let lane = o.offset.rem_euclid(sr);
                let l = t.load(&buf.at(&[sl(w0, w0 + sr)])?, false)?;
                vals.insert(i, Arg::Val(t.unpack(l, Some(lane))?));
            }
            Kind::Col => {}
        }
    }
    Ok(vals)
}

/// `v` into `dst`, read as `out`, for the band at `at` of column block `cb`.
pub(super) fn band_store(
    t: &mut Trace,
    dst: &Buf,
    out: &Operand,
    pl: &Plan,
    at: &Origin,
    cb: i64,
    v: Arg,
) -> R<()> {
    let (r, c) = &at.out;
    if out.kind == Kind::Row {
        let r = r.add(out.offset);
        let keys = if pl.nblk > 1 {
            let block = c.div(pl.wc).add(cb);
            vec![crate::trace::buffer::at(block), sl(&r, r.add(pl.rows))]
        } else {
            vec![sl(&r, r.add(pl.rows))]
        };
        return t.store(&dst.at(&keys)?, v);
    }
    let (rr, cc) = out.divmod()?;
    store_over(
        t,
        dst,
        &Idx::Int(rr).add(r),
        &c.add(cc + cb * pl.wc),
        pl.rows,
        pl.wc,
        v,
    )
}

/// The consumers' MXFP7 packings a band also writes (`q`, at the output's rows and
/// columns), checked into record `status`; `mask` (nonzero below M) zeroes rows past M.
pub struct Packs {
    pub q: Vec<Buf>,
    pub status: View,
    pub mask: Option<View>,
}

/// [`rows_block`] whose band also goes to `packs`: the stored f16 value read
/// back, its rows past M zero, packed checked into each `q` at the band.
#[allow(clippy::too_many_arguments)]
pub fn rows_block_packed(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    pl: &Plan,
    at: &Origin,
    cb: i64,
    pro: &HashMap<usize, Arg>,
    hw: &Hw,
    packs: &Packs,
) -> R<()> {
    let vals = band_values(t, bufs, operands, pl, at, cb, pro, hw)?;
    let v = expr::emit(t, e, &vals)?;
    let p = packed(t, bufs.out.spec.dtype, v)?;
    if t.value(p).shape != [pl.rows, pl.wc] {
        return fail(
            "API.OP.007",
            "a packed epilogue band is the band's own values, not a broadcast",
        );
    }
    band_store(t, &bufs.out, out, pl, at, cb, Arg::Val(p))?;
    let mut x = t.unpack(p, None)?;
    if let Some(mask) = &packs.mask {
        let m = t.load(mask, true)?;
        x = t.math(g::ValueOp::Sel, &[Arg::Val(m), Arg::Val(x), Arg::Num(0.0)])?;
    }
    let (r, c) = &at.out;
    let (rr, cc) = out.divmod()?;
    let (r0, c0) = (Idx::Int(rr).add(r), c.add(cc + cb * pl.wc));
    for q in &packs.q {
        let qv = t.pack_checked(x, q.spec.dtype, &packs.status)?;
        let view = q.at(&[sl(&r0, r0.add(pl.rows)), sl(&c0, c0.add(pl.wc))])?;
        t.store(&view, Arg::Val(qv))?;
    }
    Ok(())
}

/// `v` as the @l1 value a store of element type `dtype` writes: a statistic
/// broadcast to a block, a register value packed.
fn packed(t: &mut Trace, dtype: g::Dtype, v: Arg) -> R<V> {
    let mut v = v;
    if let Arg::Val(x) = v {
        let xv = t.value(x);
        if xv.space == g::ValSpace::L1 && xv.shape.len() == 1 {
            v = Arg::Val(t.unpack(x, None)?);
        }
    }
    match v {
        Arg::Num(_) => fail(
            "API.OP.001",
            "a rows kernel stores a computed value, not a number",
        ),
        Arg::Val(x) if t.value(x).space == g::ValSpace::L1 => Ok(x),
        Arg::Val(x) => {
            let x = if t.value(x).stat() && !crate::mutants::on("kernels.vector/stat_rows_form") {
                t.broadcast(x, t.facts.kblock)?
            } else {
                x
            };
            let gt4 = t.value(x).stat() || t.value(x).mode == Some(g::Mode::Gt4);
            t.pack(x, dtype, gt4)
        }
    }
}

/// `v` over the [rows, cols] window of `dst` at (r0, c0): packed once, then
/// stored over each of its bands (a row broadcast is one band of 4 rows,
/// repeated down).
pub(super) fn store_over(
    t: &mut Trace,
    dst: &Buf,
    r0: &Idx,
    c0: &Idx,
    rows: i64,
    cols: i64,
    v: Arg,
) -> R<()> {
    let p = packed(t, dst.spec.dtype, v)?;
    let shape = t.value(p).shape.clone();
    let (h, w) = if shape.len() == 2 {
        (shape[0], shape[1])
    } else {
        (shape[0], cols)
    };
    if rows % h != 0 || cols % w != 0 {
        return fail(
            "DSL.VEC.019",
            format!("a [{h}, {w}] value does not tile a [{rows}, {cols}] window"),
        );
    }
    for i in (0..rows).step_by(h as usize) {
        for j in (0..cols).step_by(w as usize) {
            let lo = r0.add(i);
            let view = dst.at(&[sl(&lo, lo.add(h)), sl(c0.add(j), c0.add(j + w))])?;
            t.store(&view, Arg::Val(p))?;
        }
    }
    Ok(())
}

/// One block of the whole row, a trip one `quantum` of rows, when `e` reduces
/// across `pl`'s blocks, the band is no more values than a trip of
/// [`TRIP_BANDS`] bands and its buffers fit the L1 as the lowering lays them:
/// each reduction then folds inside the band, no partial leaving the core.
#[allow(clippy::too_many_arguments)]
fn whole_band(
    e: &E,
    operands: &[Operand],
    out: &Operand,
    r: i64,
    w: i64,
    quantum: i64,
    pl: &Plan,
    hw: &Hw,
) -> Option<Plan> {
    if pl.nblk == 1
        || expr::reduces(e).is_empty()
        || out.wide()
        || operands.iter().any(Operand::wide)
        || quantum * w > TRIP_BANDS * hw.band * max_block(hw)
        || mutants::on(BLOCK_PARTIALS_MUTANT)
    {
        return None;
    }
    // The stream lowering's L1 (ktpu-l2 `lower/vector/ctx/prepare.rs`): a view
    // read or written in the trip loop takes two buffers, a prologue's one; a
    // store may take a load's two buffers of its words.
    let words_of = |o: &Operand, n: i64| cdiv(n * Hw::item(o.dtype), hw.word_bytes);
    let stat = stat_words(quantum, hw);
    let looped = |o: &Operand| match o.kind {
        Kind::Full | Kind::Tile => words_of(o, quantum * w),
        Kind::Row => stat,
        Kind::Scalar => 1,
        Kind::Col => 0,
    };
    let once = |o: &Operand| match o.kind {
        Kind::Col => words_of(o, w),
        _ => 0,
    };
    let held: i64 = operands.iter().map(|o| 2 * looped(o) + once(o)).sum();
    let stored = match out.kind {
        Kind::Row => stat,
        _ => words_of(out, quantum * w),
    };
    let band = |o: &Operand| o.rows_major() && looped(o) == stored;
    let shared = operands.iter().any(band);
    let words = held + if shared { 0 } else { 2 * stored };
    if words > hw.l1_words {
        return None;
    }
    let nb = cdiv(r, quantum);
    Some(Plan {
        wc: w,
        nblk: 1,
        rows: quantum,
        nb,
        rp: nb * quantum,
    })
}

/// What a rows kernel's arguments decide: its plan, the rows a trip divides
/// into, and the extents of `out`.
pub struct RowsShape {
    pub pl: Plan,
    pub quantum: i64,
    pub out: Vec<i64>,
}

/// The shape of `e` over [R, W] rows into `out` (API.ROW.001); with several
/// blocks a reduction's output is [blocks, R] partials.
pub fn rows_shape(
    e: &E,
    operands: &[Operand],
    r: i64,
    w: i64,
    out: &Operand,
    bands: Option<i64>,
    hw: &Hw,
) -> R<RowsShape> {
    let reduces = expr::reduces(e);
    let stats = operands
        .iter()
        .all(|o| matches!(o.kind, Kind::Row | Kind::Scalar));
    if out.kind == Kind::Row && reduces.is_empty() && !stats {
        return fail(
            "API.ROW.009",
            "a statistic output comes from a row reduction or statistics",
        );
    }
    let quantum = if holds_stat(operands, out.kind) {
        hw.stat_rows()
    } else {
        hw.band
    };
    let pl = plan(operands, out.kind, r, w, bands, None, quantum, hw)?;
    let pl = whole_band(e, operands, out, r, w, quantum, &pl, hw).unwrap_or(pl);
    let out = if out.kind == Kind::Row {
        let rows = out
            .rows
            .max(stat_words(out.offset + r, hw) * hw.stat_rows());
        if pl.nblk > 1 {
            vec![pl.nblk, rows]
        } else {
            vec![rows]
        }
    } else {
        port_shape(out, r, w, hw)?
    };
    Ok(RowsShape { pl, quantum, out })
}

/// A kernel computing `e` over [R, W] rows into `out` ([`rows_shape`]):
/// returns it and its column blocks.
pub fn rows_kernel(
    e: &E,
    operands: &[Operand],
    r: i64,
    w: i64,
    out: &Operand,
    bands: Option<i64>,
    m: &Machine,
) -> R<(Kernel, i64)> {
    let hw = Hw::of(m);
    let RowsShape {
        pl,
        quantum,
        out: out_shape,
    } = rows_shape(e, operands, r, w, out, bands, &hw)?;
    if pl.nblk > 1 && expr::reduces(e).len() > 1 {
        return fail(
            "API.ROW.009",
            "several reductions over a row wider than one block",
        );
    }
    let parts = stages(&pl, r, quantum);
    let mut ports = vec![];
    let mut given = vec![];
    for (i, o) in operands.iter().enumerate() {
        let shape = port_shape(o, r, w, &hw)?;
        ports.push(Port::input(&format!("in{i}"), &shape, g::Dtype::F16));
        given.push((
            format!("in{i}"),
            spec(&shape, o.dtype, vec![], g::BufSpace::Dram)?,
        ));
    }
    ports.push(Port::output("out", &out_shape, g::Dtype::F16));
    given.push(("out".to_string(), f16(&out_shape)?));
    let key = name(
        "rows",
        vec![
            Py::Str(expr::py(e).repr()),
            Py::Tuple(operands.iter().map(Operand::py).collect()),
            Py::Int(r),
            Py::Int(w),
            out.py(),
            pl.py(),
        ],
    );
    let k = build(&key, m, ports, given, |t, b| {
        let bufs = Bufs::plain(b[..operands.len()].to_vec(), b[operands.len()].clone());
        for (sp, row0, rows) in &parts {
            match rows {
                None => rows_stage(t, &bufs, e, operands, out, sp, hw.vc, &Idx::Int(*row0), &hw)?,
                Some(rows) => tail_band(t, &bufs, e, operands, out, sp, *row0, *rows, w, &hw)?,
            }
        }
        Ok(())
    })?;
    Ok((k, pl.nblk))
}

/// The last band of a rows kernel, `rows` (< `pl.rows`) of its rows real
/// (API.ROW.004): each operand's rows copied into a zeroed band of scratch,
/// the band computed there, its first `rows` rows copied out -- no row
/// past R read or written, but a statistic's: its word, whole.
#[allow(clippy::too_many_arguments)]
pub(super) fn tail_band(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    pl: &Plan,
    row0: i64,
    rows: i64,
    w: i64,
    hw: &Hw,
) -> R<()> {
    let sole = Member::sole(e, out, bufs);
    members_tail_band(t, bufs, &sole, operands, pl, row0, rows, w, hw)
}

/// [`tail_band`] of several members: each operand's rows copied once, every
/// member computed from them ([`members_stage`]) and copied out.
#[allow(clippy::too_many_arguments)]
pub(super) fn members_tail_band(
    t: &mut Trace,
    bufs: &Bufs,
    members: &[Member],
    operands: &[Operand],
    pl: &Plan,
    row0: i64,
    rows: i64,
    w: i64,
    hw: &Hw,
) -> R<()> {
    let mut ins = bufs.ins.clone();
    let mut ops = operands.to_vec();
    let mut maps = bufs.maps.clone();
    for (i, o) in operands.iter().enumerate() {
        if let Some(mapped) = maps.get_mut(&i) {
            mapped.rows = Some(rows);
            ops[i].offset += row0 * o.logical_strides().0;
            continue;
        }
        if o.kind == Kind::Row {
            let s = t.buffer(&[pl.rows], o.dtype, vec![])?;
            let at = o.offset + row0;
            t.copy(
                &s.at(&[sl(0, pl.rows)])?,
                &bufs.ins[i].at(&[sl(at, at + pl.rows)])?,
                None,
                None,
            )?;
            ins[i] = s;
            ops[i] = Operand {
                kind: Kind::Row,
                dtype: o.dtype,
                offset: 0,
                pitch: 0,
                rows: pl.rows,
            };
            continue;
        }
        if !o.rows_major() {
            continue;
        }
        let (rr, cc) = o.divmod()?;
        let s = t.buffer(&[pl.rows, w], o.dtype, vec![])?;
        t.fill_value(&s.whole(), 0, None, None)?;
        let src = bufs.ins[i].at(&[sl(rr + row0, rr + row0 + rows), sl(cc, cc + w)])?;
        t.copy(&s.at(&[sl(0, rows), ALL])?, &src, None, None)?;
        ins[i] = s;
        ops[i] = Operand {
            kind: o.kind,
            dtype: o.dtype,
            offset: 0,
            pitch: w,
            rows: pl.rows,
        };
    }
    let mut inner = Vec::with_capacity(members.len());
    for m in members {
        let out = &m.out;
        let (s, tail) = if out.kind == Kind::Row {
            let shape = if pl.nblk > 1 {
                vec![pl.nblk, pl.rows]
            } else {
                vec![pl.rows]
            };
            let tail = Operand {
                kind: Kind::Row,
                dtype: out.dtype,
                offset: 0,
                pitch: 0,
                rows: pl.rows,
            };
            (t.buffer(&shape, out.dtype, vec![])?, tail)
        } else {
            let tail = Operand {
                kind: out.kind,
                dtype: out.dtype,
                offset: 0,
                pitch: w,
                rows: pl.rows,
            };
            (t.buffer(&[pl.rows, w], out.dtype, vec![])?, tail)
        };
        inner.push(Member {
            e: m.e,
            out: tail,
            dst: s,
        });
    }
    let staged = Bufs {
        ins,
        out: inner[0].dst.clone(),
        maps,
    };
    members_stage(t, &staged, &inner, &ops, pl, hw.vc, &Idx::Int(0), hw)?;
    for (m, s) in members.iter().zip(&inner) {
        let (out, s) = (&m.out, &s.dst);
        if out.kind == Kind::Row && pl.nblk > 1 {
            let dst = m
                .dst
                .at(&[ALL, sl(out.offset + row0, out.offset + row0 + pl.rows)])?;
            t.copy(&dst, &s.at(&[ALL, sl(0, pl.rows)])?, None, None)?;
        } else if out.kind == Kind::Row {
            let dst = m
                .dst
                .at(&[sl(out.offset + row0, out.offset + row0 + pl.rows)])?;
            t.copy(&dst, &s.at(&[sl(0, pl.rows)])?, None, None)?;
        } else {
            let (rr, cc) = out.divmod()?;
            let dst = m
                .dst
                .at(&[sl(rr + row0, rr + row0 + rows), sl(cc, cc + w)])?;
            t.copy(&dst, &s.at(&[sl(0, rows), ALL])?, None, None)?;
        }
    }
    Ok(())
}

/// The `cols` (< a block) columns of `r` rows from column `w`, through a
/// zeroed [`r`, block] scratch: no column past the row read or written.
#[allow(clippy::too_many_arguments)]
pub(super) fn tail_columns(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    r: i64,
    w: i64,
    cols: i64,
    quantum: i64,
    hw: &Hw,
) -> R<()> {
    let sole = Member::sole(e, out, bufs);
    members_tail_columns(t, bufs, &sole, operands, r, w, cols, quantum, hw)
}

/// [`tail_columns`] of several members: each operand's columns copied once,
/// every member computed from them and copied out.
#[allow(clippy::too_many_arguments)]
pub(super) fn members_tail_columns(
    t: &mut Trace,
    bufs: &Bufs,
    members: &[Member],
    operands: &[Operand],
    r: i64,
    w: i64,
    cols: i64,
    quantum: i64,
    hw: &Hw,
) -> R<()> {
    let block = hw.kblock;
    let mut ins = bufs.ins.clone();
    let mut ops = operands.to_vec();
    let mut maps = bufs.maps.clone();
    for (i, o) in operands.iter().enumerate() {
        if let Some(mapped) = maps.get_mut(&i) {
            mapped.cols = Some(cols);
            ops[i].offset += w * o.logical_strides().1;
            continue;
        }
        match o.kind {
            Kind::Full | Kind::Tile => {
                let (rr, cc) = o.divmod()?;
                let s = t.buffer(&[r, block], o.dtype, vec![])?;
                t.fill_value(&s.whole(), 0, None, None)?;
                let src = bufs.ins[i].at(&[sl(rr, rr + r), sl(cc + w, cc + w + cols)])?;
                t.copy(&s.at(&[sl(0, r), sl(0, cols)])?, &src, None, None)?;
                ins[i] = s;
                ops[i] = Operand {
                    offset: 0,
                    pitch: block,
                    rows: r,
                    ..*o
                };
            }
            Kind::Col => {
                let s = t.buffer(&[block], o.dtype, vec![])?;
                t.fill_value(&s.whole(), 0, None, None)?;
                let src = bufs.ins[i].at(&[sl(o.offset + w, o.offset + w + cols)])?;
                t.copy(&s.at(&[sl(0, cols)])?, &src, None, None)?;
                ins[i] = s;
                ops[i] = Operand { offset: 0, ..*o };
            }
            Kind::Row | Kind::Scalar => {}
        }
    }
    let mut inner = Vec::with_capacity(members.len());
    for m in members {
        inner.push(Member {
            e: m.e,
            out: Operand {
                offset: 0,
                pitch: block,
                rows: r,
                ..m.out
            },
            dst: t.buffer(&[r, block], m.out.dtype, vec![])?,
        });
    }
    let staged = Bufs {
        ins,
        out: inner[0].dst.clone(),
        maps,
    };
    let mut planned = ops.clone();
    planned.extend(inner[1..].iter().map(|_| Operand::full(block, 0)));
    let kind = inner[0].out.kind;
    let pl = plan(&planned, kind, r, block, None, Some(block), quantum, hw)?;
    for (sp, row0, rows) in &stages(&pl, r, quantum) {
        match rows {
            None => members_stage(t, &staged, &inner, &ops, sp, hw.vc, &Idx::Int(*row0), hw)?,
            Some(rows) => members_tail_band(t, &staged, &inner, &ops, sp, *row0, *rows, block, hw)?,
        }
    }
    for (m, s) in members.iter().zip(&inner) {
        let (rr, cc) = m.out.divmod()?;
        let dst = m.dst.at(&[sl(rr, rr + r), sl(cc + w, cc + w + cols)])?;
        t.copy(&dst, &s.dst.at(&[sl(0, r), sl(0, cols)])?, None, None)?;
    }
    Ok(())
}

/// Rows of `vc.vlmax` values a flat trip takes: at most 16, double-buffered
/// in the L1 beside `reads` and `outs` a row each, and `scalars` words.
pub(super) fn flat_rows(reads: &[g::Dtype], scalars: usize, outs: &[g::Dtype], hw: &Hw) -> i64 {
    let row_words = |d: &g::Dtype| hw.vlmax * Hw::item(*d) / hw.word_bytes;
    let words: i64 = reads.iter().chain(outs).map(row_words).sum::<i64>() + scalars as i64;
    let mut rows = 16;
    while rows > 1 && 2 * rows * words > hw.l1_words - L1_MARGIN {
        rows /= 2;
    }
    rows
}

/// `total` rows as trips of `rows` over `cores`, each `trip(first row,
/// rows)`, then the rest a core's run of 4s, a 2 and a 1.
pub(super) fn flat_trips(
    t: &mut Trace,
    total: i64,
    rows: i64,
    cores: i64,
    trip: &dyn Fn(&mut Trace, &Idx, i64) -> R<()>,
) -> R<()> {
    let (full, rem) = (total / rows, total % rows);
    if full > 0 {
        let sp = split(full, cores);
        t.par(sp.cores, g::UnitKind::Vc, At::Var, |t, c| {
            let (first, trips) = (sp.start(&c), sp.trips(&c));
            t.range(trips, |t, k| {
                let r = first.mul(rows).add(k.mul(rows));
                trip(t, &r, rows)
            })
        })?;
    }
    let mut r = full * rows;
    for h in [rem / 4 * 4, (rem % 4) & 2, rem % 2] {
        if h > 0 {
            t.par(1, g::UnitKind::Vc, At::Var, |t, _| {
                t.range(1, |t, _| trip(t, &Idx::Int(r), h))
            })?;
            r += h;
        }
    }
    Ok(())
}

/// `e` over `n` elements as rows of `vc.vlmax` values (API.ROW.006): every
/// operand at the output's index, but `(i, lane)` of `scalars` (value
/// `lane` of operand i's word, every element's).
pub fn flat_kernel(
    e: &E,
    dtypes: &[g::Dtype],
    n: i64,
    out_dtype: g::Dtype,
    scalars: &[(usize, i64)],
    m: &Machine,
) -> R<Kernel> {
    let hw = Hw::of(m);
    let mut lanes: Vec<(usize, i64)> = vec![];
    for &(i, l) in scalars {
        match lanes.iter_mut().find(|(j, _)| *j == i) {
            Some(slot) => slot.1 = l,
            None => lanes.push((i, l)),
        }
    }
    let lane_of = |i: usize| lanes.iter().find(|(j, _)| *j == i).map(|(_, l)| *l);
    let held: Vec<g::Dtype> = dtypes
        .iter()
        .enumerate()
        .filter(|(i, _)| lane_of(*i).is_none())
        .map(|(_, d)| *d)
        .collect();
    let rows = flat_rows(&held, lanes.len(), &[out_dtype], &hw);
    let total = cdiv(n, hw.vlmax);
    let exact = n % hw.vlmax != 0 && !mutants::on("memory/flat-overrun");
    let extent = |d: g::Dtype| cdiv(n * Hw::item(d), hw.word_bytes);
    let mut ports = vec![];
    let mut given = vec![];
    for (i, d) in dtypes.iter().enumerate() {
        let shape = if lane_of(i).is_some() {
            vec![hw.lanes]
        } else {
            if exact {
                vec![extent(*d), hw.word_bytes / Hw::item(*d)]
            } else {
                vec![total, hw.vlmax]
            }
        };
        ports.push(Port::input(&format!("in{i}"), &shape, *d));
        given.push((
            format!("in{i}"),
            spec(&shape, *d, vec![], g::BufSpace::Dram)?,
        ));
    }
    let out_shape = if exact {
        [extent(out_dtype), hw.word_bytes / Hw::item(out_dtype)]
    } else {
        [total, hw.vlmax]
    };
    ports.push(Port::output("out", &out_shape, out_dtype));
    given.push((
        "out".to_string(),
        spec(&out_shape, out_dtype, vec![], g::BufSpace::Dram)?,
    ));
    let mut sorted = lanes.clone();
    sorted.sort();
    let key = name(
        "flat",
        vec![
            Py::Str(expr::py(e).repr()),
            Py::Tuple(dtypes.iter().map(|d| Py::str(d.name())).collect()),
            Py::Int(n),
            Py::str(out_dtype.name()),
            Py::Tuple(
                sorted
                    .iter()
                    .map(|(i, l)| Py::ints(&[*i as i64, *l]))
                    .collect(),
            ),
        ],
    );
    let trip = |t: &mut Trace, b: &[Buf], r: &Idx, h: i64| -> R<()> {
        let mut vals = HashMap::new();
        for (i, buf) in b.iter().enumerate().take(dtypes.len()) {
            let v = match lane_of(i) {
                Some(l) => {
                    let x = t.load(&buf.at(&[sl(0, hw.lanes)])?, false)?;
                    t.unpack(x, Some(l))?
                }
                None => t.load(&buf.at(&[sl(r, r.add(h)), ALL])?, false)?,
            };
            vals.insert(i, Arg::Val(v));
        }
        let v = expr::emit(t, e, &vals)?;
        t.store(&b[dtypes.len()].at(&[sl(r, r.add(h)), ALL])?, v)
    };
    build(&key, m, ports, given, |t, ports| {
        let mut padded = ports.to_vec();
        if exact {
            for (i, &dtype) in dtypes.iter().enumerate() {
                if lane_of(i).is_some() {
                    continue;
                }
                let s = t.buffer(&[total, hw.vlmax], dtype, vec![])?;
                t.fill_value(&s.whole(), 0, None, None)?;
                let walk = [(extent(dtype), hw.word_bytes)];
                t.copy(
                    &s.whole(),
                    &ports[i].whole(),
                    Some((&walk, None)),
                    Some((&walk, None)),
                )?;
                padded[i] = s;
            }
            padded[dtypes.len()] = t.buffer(&[total, hw.vlmax], out_dtype, vec![])?;
        }
        let b = padded.as_slice();
        flat_trips(t, total, rows, hw.vc, &|t, r, h| trip(t, b, r, h))?;
        if exact {
            let walk = [(extent(out_dtype), hw.word_bytes)];
            t.copy(
                &ports[dtypes.len()].whole(),
                &b[dtypes.len()].whole(),
                Some((&walk, None)),
                Some((&walk, None)),
            )?;
        }
        Ok(())
    })
}
