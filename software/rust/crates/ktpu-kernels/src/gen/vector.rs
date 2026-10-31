//! Vector-core kernels (`kernels/vector.py`): an expression over operands.
//!
//! [`rows_kernel`]: [R, W] rows (W whole `mg.kblock`-column blocks) in GT4
//! bands of `mg.lanes` rows, column blocks of at most [`Hw::max_block`],
//! trips of up to [`TRIP_BANDS`] bands the L1 holds double-buffered
//! (API.ROW.001-.004, .007). [`flat_kernel`]: N
//! elements as rows of `vc.vlmax` values (API.ROW.006, .010).

use super::{cdiv, Hw};
use crate::expr::{self, E};
use crate::kernel::{build, Kernel, Port};
use crate::mutants;
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{f16, sl, spec, ALL};
use crate::trace::control::At;
use crate::trace::vector::MappedLoad;
use crate::trace::{fail, Arg, Buf, Idx, Trace, R};
use ktpu_graph as g;
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

    fn rows_major(&self) -> bool {
        matches!(self.kind, Kind::Full | Kind::Tile)
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

impl Split {
    pub fn start(&self, c: &Idx) -> Idx {
        if self.cores == 1 {
            return Idx::Int(0);
        }
        match self.trips {
            Trips::HChunk => c.mul(self.h),
            Trips::Balanced => {
                let (q, rem) = (self.n / self.cores, self.n % self.cores);
                c.mul(q).add(c.mul(rem).add(self.cores - 1).div(self.cores))
            }
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
        Some(b) if b != 0 => b,
        _ => fit.min(TRIP_BANDS),
    }
    .max(1);
    let mut rows = hw.band * b;
    while rows % quantum != 0 {
        rows += hw.band;
    }
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
/// when only some are.
#[derive(Clone, Debug)]
pub struct Mapped {
    pub steps: g::Attr,
    pub rows: Option<i64>,
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
}

/// One rows stage (`rows_stage`): one phase a column block, its rows split
/// over the cores for row-major buffers, a tile row an instance for a
/// tiled one (`tile_rows`) of several column blocks.
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
    tile_rows: Option<i64>,
    hw: &Hw,
) -> R<()> {
    for cb in 0..pl.nblk {
        if let (true, Some(tr)) = (pl.nblk > 1, tile_rows) {
            let per = tr / pl.rows;
            let n = cdiv(pl.nb, per);
            let at = if n > cores { At::Any } else { At::Var };
            t.par(n, g::UnitKind::Vc, at, |t, u| {
                let first = u.mul(per);
                rows_trips(
                    t,
                    bufs,
                    e,
                    operands,
                    out,
                    pl,
                    &first,
                    Idx::Int(per),
                    row0,
                    cb,
                    hw,
                )
            })?;
            continue;
        }
        let sp = split(pl.nb, cores);
        t.par(sp.cores, g::UnitKind::Vc, At::Var, |t, c| {
            let (first, trips) = (sp.start(&c), sp.trips(&c));
            rows_trips(t, bufs, e, operands, out, pl, &first, trips, row0, cb, hw)
        })?;
    }
    Ok(())
}

/// An instance's trips of column block `cb` from band `first`: the
/// prologue (each `col` operand's block), then one band a trip.
#[allow(clippy::too_many_arguments)]
fn rows_trips(
    t: &mut Trace,
    bufs: &Bufs,
    e: &E,
    operands: &[Operand],
    out: &Operand,
    pl: &Plan,
    first: &Idx,
    trips: Idx,
    row0: &Idx,
    cb: i64,
    hw: &Hw,
) -> R<()> {
    let pro = prologue(t, bufs, operands, pl, cb, &Idx::Int(0))?;
    t.range(trips, |t, k| {
        let r = first.mul(pl.rows).add(k.mul(pl.rows)).add(row0);
        let at = Origin::rows(&r, operands.len());
        rows_block(t, bufs, e, operands, out, pl, &at, cb, &pro, hw)
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
        let keys = if pl.nblk > 1 {
            vec![crate::trace::buffer::at(cb), sl(r, r.add(pl.rows))]
        } else {
            vec![sl(r, r.add(pl.rows))]
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

/// `v` over the [rows, cols] window of `dst` at (r0, c0): packed once, then
/// stored over each of its bands (a row broadcast is one band of 4 rows,
/// repeated down).
fn store_over(t: &mut Trace, dst: &Buf, r0: &Idx, c0: &Idx, rows: i64, cols: i64, v: Arg) -> R<()> {
    let mut v = v;
    if let Arg::Val(x) = v {
        let xv = t.value(x);
        if xv.space == g::ValSpace::L1 && xv.shape.len() == 1 {
            v = Arg::Val(t.unpack(x, None)?);
        }
    }
    let p = match v {
        Arg::Num(_) => {
            return fail(
                "API.OP.001",
                "a rows kernel stores a computed value, not a number",
            )
        }
        Arg::Val(x) if t.value(x).space == g::ValSpace::L1 => x,
        Arg::Val(x) => {
            let x = if t.value(x).stat() && !crate::mutants::on("kernels.vector/stat_rows_form") {
                t.broadcast(x, t.facts.kblock)?
            } else {
                x
            };
            let gt4 = t.value(x).stat() || t.value(x).mode == Some(g::Mode::Gt4);
            t.pack(x, dst.spec.dtype, gt4)?
        }
    };
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
    if pl.nblk > 1 && reduces.len() > 1 {
        return fail(
            "API.ROW.009",
            "several reductions over a row wider than one block",
        );
    }
    let out = if out.kind == Kind::Row {
        let rows = out.rows.max(stat_words(r, hw) * hw.stat_rows());
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
                None => rows_stage(
                    t,
                    &bufs,
                    e,
                    operands,
                    out,
                    sp,
                    hw.vc,
                    &Idx::Int(*row0),
                    None,
                    &hw,
                )?,
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
    let inner = Bufs {
        ins,
        out: s.clone(),
        maps,
    };
    rows_stage(t, &inner, e, &ops, &tail, pl, 1, &Idx::Int(0), None, hw)?;
    if out.kind == Kind::Row && pl.nblk > 1 {
        let dst = bufs.out.at(&[ALL, sl(row0, row0 + pl.rows)])?;
        t.copy(&dst, &s.at(&[ALL, sl(0, pl.rows)])?, None, None)
    } else if out.kind == Kind::Row {
        let dst = bufs.out.at(&[sl(row0, row0 + pl.rows)])?;
        t.copy(&dst, &s.at(&[sl(0, pl.rows)])?, None, None)
    } else {
        let (rr, cc) = out.divmod()?;
        let dst = bufs
            .out
            .at(&[sl(rr + row0, rr + row0 + rows), sl(cc, cc + w)])?;
        t.copy(&dst, &s.at(&[sl(0, rows), ALL])?, None, None)
    }
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
