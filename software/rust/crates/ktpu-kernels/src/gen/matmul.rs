//! Contractions (`kernels/matmul.py`): `out = epilogue(A @ B^T, extras)`
//! on the clusters and the vector cores, one kernel (API.OP.005-.007).
//!
//! A and B are MXFP7 forms ([`quant_kernel`]): A [bt * mp, kp], B [N, kp]
//! (or one a batch). The clusters sweep tiles of tm x tn, K in chunks of
//! `mg.kblock * nk`, and drain each tile into a `tile=` scratch; the vector cores
//! read it a band at a time, apply the epilogue (its `Load(0)` the
//! product) and store row-major f16 (API.FORM.011).

use super::vector::{self as v, Kind, Operand, TRIP_BANDS};
use super::{cdiv, Hw};
use crate::expr::{self, E};
use crate::kernel::{build, layout, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, ALL};
use crate::trace::control::At;
use crate::trace::{fail, Buf, Cond, Idx, Trace, R};
use ktpu_graph as g;
use ktpu_machine::Machine;

/// The widest product tile's rows: stack policy, below the bank bound
/// (`(tm / mg.lanes) * nk <= mg.l1_bank_entries`) the plan also checks.
pub const MAX_TM: i64 = 256;

/// One contraction's tiling.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Mm {
    pub bt: i64,
    pub m: i64,
    pub n: i64,
    pub k: i64,
    /// m padded to the tile, k to the K chunk.
    pub mp: i64,
    pub kp: i64,
    pub tm: i64,
    pub tn: i64,
    pub nk: i64,
    /// One B for every batch.
    pub shared: bool,
    /// Rows a batch's padding and an epilogue trip come in: a statistic
    /// word's rows where the epilogue reads a statistic, else 4.
    pub quantum: i64,
}

impl Mm {
    /// K of one chunk: `nk` GT4 blocks.
    pub fn kc(&self, hw: &Hw) -> i64 {
        hw.kblock * self.nk
    }

    pub fn py(&self) -> Py {
        let i = Py::Int;
        Py::Obj(
            "Mm",
            vec![
                ("bt", i(self.bt)),
                ("m", i(self.m)),
                ("n", i(self.n)),
                ("k", i(self.k)),
                ("mp", i(self.mp)),
                ("kp", i(self.kp)),
                ("tm", i(self.tm)),
                ("tn", i(self.tn)),
                ("nk", i(self.nk)),
                ("shared", Py::Bool(self.shared)),
                ("quantum", i(self.quantum)),
            ],
        )
    }
}

/// The tiling of `bt` [M, K] x [N, K] products, each batch's rows padded
/// to whole `quantum`s (API.OP.006: N whole `mg.kblock`-column blocks).
pub fn plan(bt: i64, m: i64, n: i64, k: i64, shared: bool, quantum: i64, mach: &Machine) -> R<Mm> {
    let hw = Hw::of(mach);
    let (kb, band) = (hw.kblock, hw.band);
    if n % kb != 0 {
        return fail(
            "API.OP.006",
            format!("N = {n}: the product's columns are whole blocks of {kb}"),
        );
    }
    let kp = cdiv(k, kb) * kb;
    let nk = if kp % (hw.kstep * kb) == 0 {
        hw.kstep
    } else {
        1
    };
    let mp = cdiv(m, quantum) * quantum;
    let mut tm = None;
    let mut t = MAX_TM.min(mp);
    while t >= band {
        if mp % t == 0 && t % quantum == 0 && (t / band) * nk <= hw.bank {
            tm = Some(t);
            break;
        }
        t -= band;
    }
    let Some(tm) = tm else {
        return fail("API.OP.006", format!("no product tile divides {mp} rows"));
    };
    let mut tn = v::block(n, hw.max_block(), &hw)?;
    while (tm / band) * (tn / band) > hw.acc_subtiles {
        tn /= 2;
    }
    Ok(Mm {
        bt,
        m,
        n,
        k,
        mp,
        kp,
        tm,
        tn,
        nk,
        shared,
        quantum,
    })
}

/// Which packing: A (rows of the left operand) or B.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    A,
    B,
}

impl Side {
    pub fn name(self) -> &'static str {
        match self {
            Side::A => "a",
            Side::B => "b",
        }
    }

    fn dtype(self) -> g::Dtype {
        match self {
            Side::A => g::Dtype::Mx7a,
            Side::B => g::Dtype::Mx7b,
        }
    }

    fn layout(self, groups: i64, nk: i64) -> Vec<(String, g::LayoutValue)> {
        match self {
            Side::A => layout(&[("gm", groups), ("nk", nk)]),
            Side::B => layout(&[("gn", groups), ("nk", nk)]),
        }
    }
}

/// `batches` row blocks of [rows, cols] (each `src.offset + b * rows *
/// pitch`) quantised into [batches * rp, cp] in the A or B packing, tiles
/// of `g` lane groups; padding rows and columns zero (API.FORM.006).
#[allow(clippy::too_many_arguments)]
pub fn quant_kernel(
    src: &Operand,
    rows: i64,
    cols: i64,
    rp: i64,
    cp: i64,
    side: Side,
    groups: i64,
    nk: i64,
    batches: i64,
    mach: &Machine,
) -> R<Kernel> {
    quant_kernel_contract(
        src, rows, cols, rp, cp, side, groups, nk, batches, mach, false,
    )
}

/// The quant generator with a caller-validated finite input contract.
#[allow(clippy::too_many_arguments)]
pub fn quant_kernel_contract(
    src: &Operand,
    rows: i64,
    cols: i64,
    rp: i64,
    cp: i64,
    side: Side,
    groups: i64,
    nk: i64,
    batches: i64,
    mach: &Machine,
    finite: bool,
) -> R<Kernel> {
    let hw = Hw::of(mach);
    let dtype = side.dtype();
    let lay = side.layout(groups, nk);
    if src.pitch <= 0
        || cols % hw.lanes != 0
        || src.pitch % hw.lanes != 0
        || src.offset % hw.lanes != 0
    {
        return fail(
            "API.FORM.007",
            format!(
                "a quantised operand's rows are whole words: {cols} columns, pitch {}, offset {}",
                src.pitch, src.offset
            ),
        );
    }
    let total = src.offset + batches * rows * src.pitch;
    let srows = cdiv(total, src.pitch);
    let xs = [srows, src.pitch];
    let qs = [batches * rp, cp];
    let ports = vec![
        Port::input("x", &xs, src.dtype),
        Port::output("q", &qs, dtype).with_layout(lay.clone()),
    ];
    let given = vec![
        (
            "x".to_string(),
            spec(&xs, src.dtype, vec![], g::BufSpace::Dram)?,
        ),
        ("q".to_string(), spec(&qs, dtype, lay, g::BufSpace::Dram)?),
    ];
    let direct = rows == rp && cols == cp && src.pitch == cp && src.offset == 0;
    let key = name(
        "quant",
        vec![
            src.py(),
            Py::Int(rows),
            Py::Int(cols),
            Py::Int(rp),
            Py::Int(cp),
            Py::str(side.name()),
            Py::Int(groups),
            Py::Int(nk),
            Py::Int(batches),
        ],
    );
    build(&key, mach, ports, given, |t, b| {
        let (x, q) = (&b[0], &b[1]);
        if direct {
            return t.quant_finite(&q.whole(), &x.at(&[sl(0, batches * rp), ALL])?, finite);
        }
        let z = t.buffer(&[batches * rp, cp], g::Dtype::F16, vec![])?;
        t.fill_value(&z.whole(), 0, None, None)?;
        let (r0, c0) = (src.offset / src.pitch, src.offset % src.pitch);
        for bi in 0..batches {
            let dst = z.at(&[sl(bi * rp, bi * rp + rows), sl(0, cols)])?;
            let lo = r0 + bi * rows;
            let from = x.at(&[sl(lo, lo + rows), sl(c0, c0 + cols)])?;
            t.copy(&dst, &from, None, None)?;
        }
        t.quant_finite(&q.whole(), &z.whole(), finite)
    })
}

/// The epilogue's column block: the widest of GT4 blocks dividing the
/// tile's width whose trip of `quantum` rows fits the L1.
fn epilogue_block(mm: &Mm, operands: &[Operand], out: &Operand, hw: &Hw) -> i64 {
    let need = mm.quantum / hw.band;
    (1..=mm.tn / hw.kblock)
        .rev()
        .map(|k| k * hw.kblock)
        .filter(|w| mm.tn % w == 0)
        .find(|w| v::bands_fit(operands, out.kind, *w, hw) >= need)
        .unwrap_or(hw.kblock)
}

/// Bands a trip: the most that fit the L1 whose rows divide the tile, in
/// whole quanta ([`TRIP_BANDS`] halving to one).
fn epilogue_bands(mm: &Mm, operands: &[Operand], out: &Operand, wc: i64, hw: &Hw) -> R<i64> {
    let (q, band) = (mm.quantum, hw.band);
    let fit = v::plan(operands, out.kind, mm.m, mm.n, None, Some(wc), q, hw)?.rows / band;
    std::iter::successors(Some(TRIP_BANDS), |b| (*b > 1).then_some(b / 2))
        .find(|b| (*b <= fit || band * b == q) && mm.tm % (band * b) == 0 && (band * b) % q == 0)
        .map_or_else(
            || {
                fail(
                    "API.ROW.002",
                    format!("no epilogue trip divides a tile of {} rows", mm.tm),
                )
            },
            Ok,
        )
}

/// The kernel for `mm` (API.OP.005, .007): ports `a` (mx7a), `b` (mx7b),
/// `in1..` the epilogue's operands (`Load(i)`), `out`.
pub fn mm_kernel(mm: &Mm, e: &E, extras: &[Operand], out: &Operand, mach: &Machine) -> R<Kernel> {
    let hw = Hw::of(mach);
    let (clusters, cores) = (hw.mg, hw.vc);
    let (ntm, ntn) = (mm.mp / mm.tm, mm.n / mm.tn);
    let tiles = ntm * ntn;
    let ncl = clusters.min(tiles).max(1);
    let per = cdiv(tiles, ncl);
    let lay_a = Side::A.layout(mm.tm / hw.band, mm.nk);
    let lay_b = Side::B.layout(mm.tn / hw.band, mm.nk);
    let nb_rows = if mm.shared { mm.n } else { mm.bt * mm.n };
    let mut operands = vec![Operand {
        kind: Kind::Tile,
        pitch: mm.n,
        rows: mm.bt * mm.mp,
        ..Operand::new(Kind::Tile)
    }];
    operands.extend_from_slice(extras);
    let wc = epilogue_block(mm, &operands, out, &hw);
    let bands = epilogue_bands(mm, &operands, out, wc, &hw)?;
    let pl = v::plan(
        &operands,
        out.kind,
        mm.m,
        mm.n,
        Some(bands),
        Some(wc),
        mm.quantum,
        &hw,
    )?;
    let (sa, sb) = ([mm.bt * mm.mp, mm.kp], [nb_rows, mm.kp]);
    let mut ports = vec![
        Port::input("a", &sa, g::Dtype::Mx7a).with_layout(lay_a.clone()),
        Port::input("b", &sb, g::Dtype::Mx7b).with_layout(lay_b.clone()),
    ];
    let mut given = vec![
        (
            "a".to_string(),
            spec(&sa, g::Dtype::Mx7a, lay_a, g::BufSpace::Dram)?,
        ),
        (
            "b".to_string(),
            spec(&sb, g::Dtype::Mx7b, lay_b, g::BufSpace::Dram)?,
        ),
    ];
    for (i, o) in extras.iter().enumerate() {
        let shape = v::port_shape(o, mm.bt * pl.rp, mm.n, &hw)?;
        let n = format!("in{}", i + 1);
        ports.push(Port::input(&n, &shape, g::Dtype::F16));
        given.push((n, spec(&shape, o.dtype, vec![], g::BufSpace::Dram)?));
    }
    let os = v::port_shape(out, mm.bt * pl.rp, mm.n, &hw)?;
    ports.push(Port::output("out", &os, g::Dtype::F16));
    given.push((
        "out".to_string(),
        spec(&os, out.dtype, vec![], g::BufSpace::Dram)?,
    ));
    let key = name(
        "mm",
        vec![
            mm.py(),
            Py::Str(expr::py(e).repr()),
            Py::Tuple(extras.iter().map(Operand::py).collect()),
            out.py(),
            Py::Int(clusters),
            Py::Int(cores),
        ],
    );
    let batch = |t: &mut Trace, b: &[Buf], h: &Buf, bt: &Idx| -> R<()> {
        t.par(ncl, g::UnitKind::Mg, At::Var, |t, u| {
            t.range(per, |t, i| {
                let tile = u.add(i.mul(ncl));
                let (ti, tj) = (tile.div(ntn), tile.rem(ntn));
                if per * ncl != tiles {
                    t.when(Cond::new(&tile, g::CmpOp::Lt, tiles), |t| {
                        product_tile(t, b, h, mm, bt, &ti, &tj, &hw)
                    })
                } else {
                    product_tile(t, b, h, mm, bt, &ti, &tj, &hw)
                }
            })
        })?;
        let mut ins = vec![h.clone()];
        ins.extend_from_slice(&b[2..2 + extras.len()]);
        let bufs = v::Bufs {
            ins,
            out: b[2 + extras.len()].clone(),
        };
        v::rows_stage(
            t,
            &bufs,
            e,
            &operands,
            out,
            &pl,
            cores,
            &bt.mul(mm.mp),
            Some(mm.tm),
            &hw,
        )
    };
    build(&key, mach, ports, given, |t, b| {
        let tile = vec![(
            "tile".to_string(),
            g::LayoutValue::Ints(vec![mm.tm / hw.band, mm.tn / hw.band]),
        )];
        let h = t.buffer(&[mm.bt * mm.mp, mm.n], g::Dtype::F16, tile)?;
        if mm.bt == 1 {
            batch(t, b, &h, &Idx::Int(0))
        } else {
            t.range(mm.bt, |t, bt| batch(t, b, &h, &bt))
        }
    })
}

/// One tile: zero, a gemm a K chunk, drained into the tile scratch.
#[allow(clippy::too_many_arguments)]
fn product_tile(
    t: &mut Trace,
    b: &[Buf],
    h: &Buf,
    mm: &Mm,
    bt: &Idx,
    ti: &Idx,
    tj: &Idx,
    hw: &Hw,
) -> R<()> {
    let acc = t.zero(mm.tm, mm.tn)?;
    let arow = bt.mul(mm.mp).add(ti.mul(mm.tm));
    let brow = if mm.shared {
        tj.mul(mm.tn)
    } else {
        bt.mul(mm.n).add(tj.mul(mm.tn))
    };
    let kc = mm.kc(hw);
    let mut acc = acc;
    t.range(mm.kp / kc, |t, k| {
        let k0 = k.mul(kc);
        let a = t.fill(&b[0].at(&[sl(&arow, arow.add(mm.tm)), sl(&k0, k0.add(kc))])?)?;
        let bv = t.fill(&b[1].at(&[sl(&brow, brow.add(mm.tn)), sl(&k0, k0.add(kc))])?)?;
        acc = t.gemm(a, bv, acc)?;
        Ok(())
    })?;
    let c0 = tj.mul(mm.tn);
    t.drain(
        acc,
        &h.at(&[sl(&arow, arow.add(mm.tm)), sl(&c0, c0.add(mm.tn))])?,
    )
}
