//! Contractions (`kernels/matmul.py`): `out = epilogue(A @ B^T, extras)`
//! on the clusters and the vector cores, one kernel (API.OP.005-.007).
//!
//! A and B are MXFP7 forms ([`quant_kernel`]): A [bt * mp, kp], B [N, kp]
//! (or one a batch). The clusters sweep tiles of tm x tn, K in chunks of
//! `mg.kblock * nk`, and drain each tile into a `tile=` scratch; the vector cores
//! read it a band at a time, apply the epilogue (its `Load(0)` the
//! product) and store row-major f16 (API.FORM.011).

use super::vector::{self as v, Kind, Operand, TRIP_BANDS};
use super::{cdiv, prepared, Hw};
use crate::expr::{self, E};
use crate::kernel::{build, layout, Kernel, Port};
use crate::pyrepr::{name, Py};
use crate::trace::buffer::{sl, spec, ALL};
use crate::trace::control::At;
use crate::trace::{fail, Buf, Cond, Idx, Trace, View, R};

mod ring;
use ktpu_graph as g;
use ktpu_machine::{Machine, Pairing};
pub use ring::{resolve as ring_resolve, Ring};
use std::collections::BTreeMap;

/// The widest product tile's rows: stack policy, below the bank bound
/// (`(tm / mg.lanes) * nk <= mg.l1_bank_entries`) the plan also checks.
pub const MAX_TM: i64 = 256;

/// The reverse mutant of the knobs' recording: no knob recorded in a
/// `matmul.plan` or `matmul.mm_kernel` call.
pub const UNRECORDED_MUTANT: &str = "kernels.matmul/unrecorded-knobs";

/// Which call records a knob: the plan's (`matmul.plan`) or the kernel's
/// (`matmul.mm_kernel`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Group {
    Plan,
    Kernel,
}

/// A knob: its argument name, its environment override, the call recording it.
pub struct Knob {
    pub name: &'static str,
    pub env: &'static str,
    pub group: Group,
}

/// Every contraction knob.
pub const KNOBS: &[Knob] = &[
    Knob {
        name: "pad_m",
        env: "KTPU_MM_PAD_M",
        group: Group::Plan,
    },
    Knob {
        name: "n_tiles",
        env: "KTPU_MM_N_TILES",
        group: Group::Plan,
    },
    Knob {
        name: "dense_n",
        env: "KTPU_MM_DENSE_N",
        group: Group::Plan,
    },
    Knob {
        name: "pad_k",
        env: "KTPU_MM_PAD_K",
        group: Group::Plan,
    },
    Knob {
        name: "batch_tiles",
        env: "KTPU_MM_BATCH_TILES",
        group: Group::Kernel,
    },
    Knob {
        name: "ring_depth",
        env: "KTPU_MM_RING_DEPTH",
        group: Group::Kernel,
    },
    Knob {
        name: "split_vc",
        env: "KTPU_MM_SPLIT_VC",
        group: Group::Kernel,
    },
    Knob {
        name: "tail_bands",
        env: "KTPU_MM_TAIL_BANDS",
        group: Group::Kernel,
    },
    Knob {
        name: "activation_fp16",
        env: "KTPU_MM_ACTIVATION_FP16",
        group: Group::Kernel,
    },
    Knob {
        name: "quant_ring",
        env: "KTPU_MM_QUANT_RING",
        group: Group::Kernel,
    },
    Knob {
        name: "expr_cse",
        env: "KTPU_MM_EXPR_CSE",
        group: Group::Kernel,
    },
];

/// A contraction's plan and kernel knobs: the one source the generators,
/// ktpu-plan and Python (`_rs.mm_knobs`) read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Knobs {
    /// Balanced, cost-modelled M tiles with a zero-padded tail.
    pub pad_m: bool,
    /// The least N tile count.
    pub n_tiles: i64,
    /// The widest N tile that divides N (not only powers of two).
    pub dense_n: bool,
    /// K padded to whole block pairs where the machine pairs blocks.
    pub pad_k: bool,
    /// The clusters share out every batch's product tiles at once.
    pub batch_tiles: bool,
    /// Waves of product tiles in flight through an on-chip ring of row
    /// groups (0: the product in DRAM), at most the staging room holds.
    pub ring_depth: i64,
    /// Under the ring, a row group's epilogue rows split over the cores.
    pub split_vc: bool,
    /// Under the ring, the widest epilogue trip and a tail trip, not the
    /// widest dividing the tile.
    pub tail_bands: bool,
    /// A is f16, quantised in the kernel, where its packing is the vector
    /// cores' and its rows whole words.
    pub activation_fp16: bool,
    /// Under the ring with an f16 A: each row tile quantised into `@stage`
    /// slots as the waves reach it (1), or a wave ahead with its finite
    /// check deferred (2); 0 quantises the whole A first.
    pub quant_ring: i64,
    /// The epilogue's structurally equal subexpressions computed once
    /// ([`expr::intern`]).
    pub expr_cse: bool,
}

/// The knobs' defaults.
pub fn defaults() -> Knobs {
    unrecorded()
}

/// The knobs of a call that records none: the contraction before them.
pub fn unrecorded() -> Knobs {
    Knobs {
        pad_m: false,
        n_tiles: 1,
        dense_n: false,
        pad_k: false,
        batch_tiles: false,
        ring_depth: 0,
        split_vc: false,
        tail_bands: false,
        activation_fp16: false,
        quant_ring: 0,
        expr_cse: false,
    }
}

impl Knobs {
    /// The defaults under each set `KTPU_MM_*` override (`1` sets a flag,
    /// anything else clears it; an integer knob's value is an integer).
    pub fn resolve() -> Knobs {
        let mut k = defaults();
        for knob in KNOBS {
            if let Ok(raw) = std::env::var(knob.env) {
                let v = match k.get(knob.name) {
                    serde_json::Value::Bool(_) => serde_json::Value::Bool(raw == "1"),
                    _ => raw
                        .parse::<i64>()
                        .unwrap_or_else(|_| panic!("{}={raw} is not an integer", knob.env))
                        .into(),
                };
                k.set(knob.name, &v).expect("a value of the knob's type");
            }
        }
        k
    }

    fn get(&self, name: &str) -> serde_json::Value {
        match name {
            "pad_m" => self.pad_m.into(),
            "n_tiles" => self.n_tiles.into(),
            "dense_n" => self.dense_n.into(),
            "pad_k" => self.pad_k.into(),
            "batch_tiles" => self.batch_tiles.into(),
            "ring_depth" => self.ring_depth.into(),
            "split_vc" => self.split_vc.into(),
            "tail_bands" => self.tail_bands.into(),
            "activation_fp16" => self.activation_fp16.into(),
            "quant_ring" => self.quant_ring.into(),
            "expr_cse" => self.expr_cse.into(),
            _ => unreachable!("a knob: {name}"),
        }
    }

    fn set(&mut self, name: &str, v: &serde_json::Value) -> R<()> {
        let flag = || match v.as_bool() {
            Some(b) => Ok(b),
            None => fail("API.OP.001", format!("{name} is not a bool")),
        };
        let int = || match v.as_i64() {
            Some(i) => Ok(i),
            None => fail("API.OP.001", format!("{name} is not an int")),
        };
        match name {
            "pad_m" => self.pad_m = flag()?,
            "n_tiles" => self.n_tiles = int()?,
            "dense_n" => self.dense_n = flag()?,
            "pad_k" => self.pad_k = flag()?,
            "batch_tiles" => self.batch_tiles = flag()?,
            "ring_depth" => self.ring_depth = int()?,
            "split_vc" => self.split_vc = flag()?,
            "tail_bands" => self.tail_bands = flag()?,
            "activation_fp16" => self.activation_fp16 = flag()?,
            "quant_ring" => self.quant_ring = int()?,
            "expr_cse" => self.expr_cse = flag()?,
            _ => unreachable!("a knob: {name}"),
        }
        Ok(())
    }

    /// The `group` knobs as the call records them (none under
    /// [`UNRECORDED_MUTANT`]).
    pub fn args(&self, group: Group) -> Vec<(&'static str, serde_json::Value)> {
        if crate::mutants::on(UNRECORDED_MUTANT) {
            return vec![];
        }
        KNOBS
            .iter()
            .filter(|k| k.group == group)
            .map(|k| (k.name, self.get(k.name)))
            .collect()
    }

    /// `self` with each `group` knob the call `a` names.
    pub fn read(&self, a: &serde_json::Value, group: Group) -> R<Knobs> {
        let mut k = *self;
        for knob in KNOBS.iter().filter(|k| k.group == group) {
            if let Some(v) = a.get(knob.name) {
                k.set(knob.name, v)?;
            }
        }
        Ok(k)
    }

    /// Every knob as a JSON object.
    pub fn json(&self) -> serde_json::Value {
        KNOBS
            .iter()
            .map(|k| (k.name.to_string(), self.get(k.name)))
            .collect::<serde_json::Map<_, _>>()
            .into()
    }
}

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
/// to whole `quantum`s (API.OP.006: N whole `mg.kblock`-column blocks); K
/// blocks paired as `pairing` allows on `mach` (RUL.099). The M tile is the
/// largest that divides the padded rows, or with `knobs.pad_m` the balanced
/// tile (its tail zero-padded) where [`tile_cost`] prices it no higher; the
/// N tile the widest power-of-two block the accumulator holds, halved to at
/// least `knobs.n_tiles` tiles, or with `knobs.dense_n` the widest
/// `mg.kblock` multiple dividing N. K pads to whole blocks, or with
/// `knobs.pad_k` (K past one block, blocks paired) to whole block pairs.
#[allow(clippy::too_many_arguments)]
pub fn plan(
    bt: i64,
    m: i64,
    n: i64,
    k: i64,
    shared: bool,
    quantum: i64,
    pairing: Pairing,
    mach: &Machine,
    knobs: &Knobs,
) -> R<Mm> {
    let hw = Hw::of(mach);
    let (kb, band) = (hw.kblock, hw.band);
    if n % kb != 0 {
        return fail(
            "API.OP.006",
            format!("N = {n}: the product's columns are whole blocks of {kb}"),
        );
    }
    let pairs = mach.pairs_blocks(pairing);
    let k_unit = if knobs.pad_k && pairs && k > kb {
        hw.kstep * kb
    } else {
        kb
    };
    let kp = cdiv(k, k_unit) * k_unit;
    let nk = if pairs && kp % (hw.kstep * kb) == 0 {
        hw.kstep
    } else {
        1
    };
    let mut mp = cdiv(m, quantum) * quantum;
    let max_tm = (hw.bank / nk * band)
        .min(MAX_TM)
        .min(ring_rows(mach).max(quantum))
        .min(mp)
        / quantum
        * quantum;
    let mut tm_div = None;
    let mut t = max_tm;
    while t >= band {
        if mp % t == 0 && t % quantum == 0 {
            tm_div = Some(t);
            break;
        }
        t -= band;
    }
    let Some(tm_div) = tm_div else {
        return fail("API.OP.006", format!("no product tile divides {mp} rows"));
    };
    let tn_of = |tm: i64| -> R<i64> {
        let mut tn = v::block(n, hw.max_block(), &hw)?;
        while (tm / band) * (tn / band) > hw.acc_subtiles {
            tn /= 2;
        }
        if knobs.dense_n {
            return Ok((1..=n.min(hw.max_block()) / kb)
                .rev()
                .map(|x| x * kb)
                .find(|&x| {
                    n % x == 0
                        && (tm / band) * (x / band) <= hw.acc_subtiles
                        && (x / band) * nk <= hw.bank
                        && n / x >= knobs.n_tiles.min(n / kb)
                })
                .unwrap_or(tn));
        }
        while n / tn < knobs.n_tiles && tn / 2 >= kb && n % (tn / 2) == 0 {
            tn /= 2;
        }
        Ok(tn)
    };
    let tm = if knobs.pad_m {
        let tm_bal = cdiv(cdiv(mp, cdiv(mp, max_tm)), quantum) * quantum;
        let cost = |tm: i64| -> R<i64> { Ok(tile_cost(mp, n, kp, nk, tm, tn_of(tm)?, &hw)) };
        if cost(tm_bal)? <= cost(tm_div)? {
            tm_bal
        } else {
            tm_div
        }
    } else {
        tm_div
    };
    mp = cdiv(mp, tm) * tm;
    let tn = tn_of(tm)?;
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

/// The most rows a product tile takes for one ring wave of tiles of the
/// widest block, one a cluster, to fit the staging room ([`ring::resolve`]).
pub fn ring_rows(mach: &Machine) -> i64 {
    let hw = Hw::of(mach);
    mach.stage_room() / (2 * hw.mg * hw.max_block())
}

/// The plan's price of an M tile `tm` (N tile `tn`) over `mp` padded rows:
/// the clusters' issue slots, the A and B fills, the product's drain,
/// epilogue read and store, the padded tail's quantise and epilogue traffic
/// in machine words, and a fixed cost a tile.
#[allow(clippy::too_many_arguments)]
pub fn tile_cost(mp: i64, n: i64, kp: i64, nk: i64, tm: i64, tn: i64, hw: &Hw) -> i64 {
    let (kb, band) = (hw.kblock, hw.band);
    let ntm = cdiv(mp, tm);
    let mp_pad = ntm * tm;
    let ntn = n / tn;
    let issue = (mp_pad / band) * (n / band) * (kp / (kb * nk));
    let fill_bytes = kp * (mp_pad * ntn + n * ntm);
    let out_bytes = 2 * mp_pad * n;
    let pad_bytes = (mp_pad - mp) * (2 * kp + 4 * n);
    issue + (fill_bytes + 3 * out_bytes + pad_bytes) / hw.word_bytes + 64 * ntm * ntn
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

pub const QUANT_SCHEDULE: &[(&str, i64)] = &[
    ("finite_batch", 4),
    ("overlap", 1),
    ("classifier_overlap", 0),
    ("pipeline", 1),
    ("zero_classifier", 1),
    ("stream_axes", 1),
    ("fill_overlap", 1),
    ("zero_reuse", 1),
];

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
    quant_kernel_scheduled(
        src,
        rows,
        cols,
        rp,
        cp,
        side,
        groups,
        nk,
        batches,
        mach,
        finite,
        QUANT_SCHEDULE,
        &prepared::Read::Materialized(vec![]),
        false,
    )
}

/// [`quant_kernel_contract`] under `schedule`; `x` read by `prepared`'s
/// steps (ports `p_*`), `staged` always copying into a zeroed scratch.
#[allow(clippy::too_many_arguments)]
pub fn quant_kernel_scheduled(
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
    schedule: &[(&str, i64)],
    prepared: &prepared::Read,
    staged: bool,
) -> R<Kernel> {
    let hw = Hw::of(mach);
    let dtype = side.dtype();
    let lay = side.layout(groups, nk);
    if let prepared::Read::Composed(steps) = prepared {
        let (r0, c0) = (src.offset / src.pitch, src.offset % src.pitch);
        let qs = [batches * rp, cp];
        let (mut ports, mut given) = prepared::input_ports(prepared, "p_", mach)?;
        ports.push(Port::output("q", &qs, dtype).with_layout(lay.clone()));
        given.push(("q".to_string(), spec(&qs, dtype, lay, g::BufSpace::Dram)?));
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
                Py::Bool(finite),
                Py::Tuple(
                    schedule
                        .iter()
                        .map(|(k, v)| Py::Tuple(vec![Py::str(k), Py::Int(*v)]))
                        .collect(),
                ),
                prepared.py(),
            ],
        );
        let ints = |v: &[i64]| g::Attr::Tuple(v.iter().map(|i| g::Attr::Int(*i)).collect());
        let read = [
            ("read_map", prepared::Read::steps(steps)),
            ("read_shape", ints(&[src.pitch, r0, c0, rows, cols])),
            ("read_batch", ints(&[batches, rp])),
        ];
        return build(&key, mach, ports, given, |t, b| {
            t.quant_mapped(&b[1].whole(), &b[0].whole(), finite, schedule, &read)
        });
    }
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
    // The vector packing pads in the quant (`quant pad=1`, L2.MOV.010) and
    // takes the schedule; the mover packs only whole views.
    let vector = vector_packs(nk, mach);
    let schedule_on: &[(&str, i64)] = if vector { schedule } else { &[] };
    let scratch = staged || (!vector && (rows != rp || cols != cp));
    let mut key = vec![
        src.py(),
        Py::Int(rows),
        Py::Int(cols),
        Py::Int(rp),
        Py::Int(cp),
        Py::str(side.name()),
        Py::Int(groups),
        Py::Int(nk),
        Py::Int(batches),
        Py::Bool(finite),
        Py::Tuple(
            schedule
                .iter()
                .map(|(k, v)| Py::Tuple(vec![Py::str(k), Py::Int(*v)]))
                .collect(),
        ),
    ];
    let any = !matches!(prepared, prepared::Read::Materialized(p) if p.is_empty());
    if any {
        key.push(prepared.py());
    }
    if staged {
        key.push(Py::str("staged"));
    }
    let key = name("quant", key);
    let names: Vec<String> = ports.iter().map(|p| p.name.clone()).collect();
    let made_in: Vec<prepared::MadeInput> = if any {
        vec![prepared::MadeInput {
            port: "x".into(),
            prefix: "p_".into(),
            read: prepared,
        }]
    } else {
        vec![]
    };
    let ((ports, given), shapes) = prepared::made_ports((ports, given), &made_in, mach)?;
    let built: Vec<String> = ports.iter().map(|p| p.name.clone()).collect();
    build(&key, mach, ports, given, |t, b| {
        let b = prepared::made_bufs(t, &names, &built, b, &made_in, &shapes, mach)?;
        let (x, q) = (&b[0], &b[1]);
        let (r0, c0) = (src.offset / src.pitch, src.offset % src.pitch);
        if scratch {
            let z = t.buffer(&[batches * rp, cp], g::Dtype::F16, vec![])?;
            t.fill_value(&z.whole(), 0, None, None)?;
            for bi in 0..batches {
                let dst = z.at(&[sl(bi * rp, bi * rp + rows), sl(0, cols)])?;
                let lo = r0 + bi * rows;
                let from = x.at(&[sl(lo, lo + rows), sl(c0, c0 + cols)])?;
                t.copy(&dst, &from, None, None)?;
            }
            return t.quant_scheduled(&q.whole(), &z.whole(), finite, schedule_on, false);
        }
        // Unpadded row blocks stack contiguously: one quant over the stack.
        let blocks = if rows == rp { 1 } else { batches };
        let span = batches / blocks;
        for b in 0..blocks {
            let dst = q.at(&[sl(b * span * rp, (b + 1) * span * rp), ALL])?;
            let lo = r0 + b * span * rows;
            let from = x.at(&[sl(lo, lo + span * rows), sl(c0, c0 + cols)])?;
            t.quant_scheduled(&dst, &from, finite, schedule_on, vector)?;
        }
        Ok(())
    })
}

/// `batches` [rows, cols] blocks walked from `x` (`src_bytes` bytes) by
/// `levels` from byte `at`, staged and quantised in one package into
/// [batches * rp, cp] as [`quant_kernel_scheduled`]: the walk materialises
/// logical values, never padding.
#[allow(clippy::too_many_arguments)]
pub fn quant_walk_kernel(
    src_bytes: i64,
    levels: &[(i64, i64)],
    at: i64,
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
    schedule: &[(&str, i64)],
) -> R<Kernel> {
    let hw = Hw::of(mach);
    let dtype = side.dtype();
    let lay = side.layout(groups, nk);
    let xs = [cdiv(src_bytes, hw.word_bytes), hw.lanes];
    let qs = [batches * rp, cp];
    let ports = vec![
        Port::input("x", &xs, g::Dtype::F16),
        Port::output("q", &qs, dtype).with_layout(lay.clone()),
    ];
    let given = vec![
        (
            "x".to_string(),
            spec(&xs, g::Dtype::F16, vec![], g::BufSpace::Dram)?,
        ),
        ("q".to_string(), spec(&qs, dtype, lay, g::BufSpace::Dram)?),
    ];
    let key = name(
        "quant",
        vec![
            Py::str("walk"),
            Py::Int(src_bytes),
            Py::Tuple(levels.iter().map(|(n, s)| Py::ints(&[*n, *s])).collect()),
            Py::Int(at),
            Py::Int(rows),
            Py::Int(cols),
            Py::Int(rp),
            Py::Int(cp),
            Py::str(side.name()),
            Py::Int(groups),
            Py::Int(nk),
            Py::Int(batches),
            Py::Bool(finite),
            Py::Tuple(
                schedule
                    .iter()
                    .map(|(k, v)| Py::Tuple(vec![Py::str(k), Py::Int(*v)]))
                    .collect(),
            ),
        ],
    );
    let vector = vector_packs(nk, mach);
    let schedule_on: &[(&str, i64)] = if vector { schedule } else { &[] };
    build(&key, mach, ports, given, |t, b| {
        let (x, q) = (&b[0], &b[1]);
        let z = t.buffer(&[batches * rows, cols], g::Dtype::F16, vec![])?;
        let dlevels = [(batches * rows * cols / hw.lanes, hw.word_bytes)];
        t.copy(
            &z.whole(),
            &x.whole(),
            Some((levels, Some(at))),
            Some((&dlevels, Some(0))),
        )?;
        if !vector && (rows != rp || cols != cp) {
            let padded = t.buffer(&[batches * rp, cp], g::Dtype::F16, vec![])?;
            t.fill_value(&padded.whole(), 0, None, None)?;
            for bi in 0..batches {
                let dst = padded.at(&[sl(bi * rp, bi * rp + rows), sl(0, cols)])?;
                let from = z.at(&[sl(bi * rows, (bi + 1) * rows), ALL])?;
                t.copy(&dst, &from, None, None)?;
            }
            return t.quant_scheduled(&q.whole(), &padded.whole(), finite, &[], false);
        }
        for bi in 0..batches {
            let dst = q.at(&[sl(bi * rp, (bi + 1) * rp), ALL])?;
            let from = z.at(&[sl(bi * rows, (bi + 1) * rows), ALL])?;
            t.quant_scheduled(&dst, &from, finite, schedule_on, vector)?;
        }
        Ok(())
    })
}

/// Whether a quant into `nk` K blocks a chunk packs on the vector cores
/// (L2.MOV.018): pairs always, else as the machine prefers.
pub fn vector_packs(nk: i64, mach: &Machine) -> bool {
    nk >= 2 || mach.conversion.fp16_mx7.preferred_backend == ktpu_machine::PackingBackend::VectorMx7
}

/// Whether a contraction over `k` (`nk` K blocks a chunk) takes A as f16
/// under `knobs` (`activation_fp16`): its quant packs on the vector cores,
/// which pad rows and columns in place, and A's rows are whole words.
pub fn takes_f16(k: i64, nk: i64, knobs: &Knobs, mach: &Machine) -> bool {
    knobs.activation_fp16 && vector_packs(nk, mach) && k % Hw::of(mach).lanes == 0
}

/// The checked quant of f16 `src` ([rows, K], rows whole words) into the
/// [`rp`, kp] A packing `dst`, the rest zero; its finite status deferred
/// into `status` where given.
fn quant_a(t: &mut Trace, dst: &View, src: &View, padded: bool, status: Option<&View>) -> R<()> {
    match status {
        Some(s) => t.quant_deferred(dst, src, s, QUANT_SCHEDULE, padded),
        None => t.quant_scheduled(dst, src, false, QUANT_SCHEDULE, padded),
    }
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
/// `in1..` the epilogue's operands (`Load(i)`), `out`. A batch's product
/// tiles, then its epilogue, a batch at a time; with `knobs.batch_tiles`
/// every batch's tiles at once, then each batch's epilogue; with a ring
/// ([`ring::resolve`]) through `@stage` slots. Where it takes A as f16
/// ([`takes_f16`]) port `a` is the f16 [bt * m, K], quantised in the kernel.
/// With `knobs.expr_cse` the epilogue is `e` interned ([`takes_cse`]).
pub fn mm_kernel(
    mm: &Mm,
    e: &E,
    extras: &[Operand],
    out: &Operand,
    knobs: &Knobs,
    mach: &Machine,
) -> R<Kernel> {
    mm_kernel_made(mm, e, extras, out, knobs, mach, &BTreeMap::new())
}

/// [`mm_kernel`] with epilogue operand i of `made` read inside the kernel
/// by its [`prepared::Read`] (ports `p{i}_*` for `in{i}`).
pub fn mm_kernel_made(
    mm: &Mm,
    e: &E,
    extras: &[Operand],
    out: &Operand,
    knobs: &Knobs,
    mach: &Machine,
    made: &BTreeMap<usize, prepared::Read>,
) -> R<Kernel> {
    let cse = takes_cse(e, knobs);
    let e = &computed(e, knobs);
    let hw = Hw::of(mach);
    let (clusters, cores) = (hw.mg, hw.vc);
    let (ntm, ntn) = (mm.mp / mm.tm, mm.n / mm.tn);
    let tiles = ntm * ntn;
    let batched = knobs.batch_tiles && mm.bt > 1;
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
    let ring = ring::resolve(mm, e, out, knobs, mach);
    let (segs, rows) = if ring.depth > 0 {
        let segs = ring::segments(mm, &ring, &operands, out, wc, &hw)?;
        let rows = (mm.bt - 1) * mm.mp + ring::cover(mm, &ring, &segs);
        (segs, rows)
    } else {
        (vec![], mm.bt * pl.rp)
    };
    let f16 = takes_f16(mm.k, mm.nk, knobs, mach);
    let (sa, sb) = ([mm.bt * mm.mp, mm.kp], [nb_rows, mm.kp]);
    let (pa, a_dtype, a_lay) = if f16 {
        ([mm.bt * mm.m, mm.k], g::Dtype::F16, vec![])
    } else {
        (sa, g::Dtype::Mx7a, lay_a.clone())
    };
    let mut ports = vec![
        Port::input("a", &pa, a_dtype).with_layout(a_lay.clone()),
        Port::input("b", &sb, g::Dtype::Mx7b).with_layout(lay_b.clone()),
    ];
    let mut given = vec![
        (
            "a".to_string(),
            spec(&pa, a_dtype, a_lay, g::BufSpace::Dram)?,
        ),
        (
            "b".to_string(),
            spec(&sb, g::Dtype::Mx7b, lay_b, g::BufSpace::Dram)?,
        ),
    ];
    for (i, o) in extras.iter().enumerate() {
        let shape = v::port_shape(o, rows, mm.n, &hw)?;
        let n = format!("in{}", i + 1);
        ports.push(Port::input(&n, &shape, g::Dtype::F16));
        given.push((n, spec(&shape, o.dtype, vec![], g::BufSpace::Dram)?));
    }
    let os = v::port_shape(out, rows, mm.n, &hw)?;
    ports.push(Port::output("out", &os, g::Dtype::F16));
    given.push((
        "out".to_string(),
        spec(&os, out.dtype, vec![], g::BufSpace::Dram)?,
    ));
    let mut key_args = vec![
        mm.py(),
        Py::Str(expr::py(e).repr()),
        Py::Tuple(extras.iter().map(Operand::py).collect()),
        out.py(),
        Py::Int(clusters),
        Py::Int(cores),
    ];
    if ring.depth > 0 {
        key_args.push(ring.py());
    } else if batched {
        key_args.push(Py::str("batch_tiles"));
    }
    if f16 {
        key_args.push(Py::str("activation_fp16"));
    }
    if cse {
        key_args.push(Py::str("expr_cse"));
    }
    if !made.is_empty() {
        key_args.push(prepared::made_key(made));
    }
    let key = name("mm", key_args);
    let names: Vec<String> = ports.iter().map(|p| p.name.clone()).collect();
    let made_in = prepared::made_inputs(made);
    let ((ports, given), shapes) = prepared::made_ports((ports, given), &made_in, mach)?;
    let built: Vec<String> = ports.iter().map(|p| p.name.clone()).collect();
    // The product tiles of `nb` batches from batch `bt` (`nb` > 1: from
    // batch 0) over the clusters.
    let products = |t: &mut Trace, b: &[Buf], h: &Buf, bt: &Idx, nb: i64| -> R<()> {
        let all = nb * tiles;
        let ncl = clusters.min(all).max(1);
        let per = cdiv(all, ncl);
        t.par(ncl, g::UnitKind::Mg, At::Var, |t, u| {
            t.range(per, |t, i| {
                let tile = u.add(i.mul(ncl));
                let (bt, at) = if nb == 1 {
                    (bt.clone(), tile.clone())
                } else {
                    (tile.div(tiles), tile.rem(tiles))
                };
                let (ti, tj) = (at.div(ntn), at.rem(ntn));
                if per * ncl != all {
                    t.when(Cond::new(&tile, g::CmpOp::Lt, all), |t| {
                        product_tile(t, b, h, mm, &bt, &ti, &tj, &hw)
                    })
                } else {
                    product_tile(t, b, h, mm, &bt, &ti, &tj, &hw)
                }
            })
        })
    };
    let epilogue = |t: &mut Trace, b: &[Buf], h: &Buf, bt: &Idx| -> R<()> {
        let mut ins = vec![h.clone()];
        ins.extend_from_slice(&b[2..2 + extras.len()]);
        let bufs = v::Bufs {
            ins,
            out: b[2 + extras.len()].clone(),
            maps: prepared::mapped(made),
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
        let canonical = prepared::made_bufs(t, &names, &built, b, &made_in, &shapes, mach)?;
        // An f16 A outside a quant ring: each batch quantised first.
        let mut quantised = canonical.clone();
        if f16 && ring.quant == 0 {
            let qa = t.buffer(&sa, g::Dtype::Mx7a, lay_a.clone())?;
            let padded = mm.m < mm.mp || mm.k < mm.kp;
            for bi in 0..mm.bt {
                let dst = qa.at(&[sl(bi * mm.mp, (bi + 1) * mm.mp), ALL])?;
                let src = canonical[0].at(&[sl(bi * mm.m, (bi + 1) * mm.m), ALL])?;
                quant_a(t, &dst, &src, padded, None)?;
            }
            quantised[0] = qa;
        }
        let b = &quantised[..];
        if ring.depth > 0 {
            return ring::schedule(t, b, mm, e, extras, out, &ring, &segs, mach);
        }
        let tile = vec![(
            "tile".to_string(),
            g::LayoutValue::Ints(vec![mm.tm / hw.band, mm.tn / hw.band]),
        )];
        let h = t.buffer(&[mm.bt * mm.mp, mm.n], g::Dtype::F16, tile)?;
        let batch = |t: &mut Trace, bt: &Idx| -> R<()> {
            products(t, b, &h, bt, 1)?;
            epilogue(t, b, &h, bt)
        };
        if batched {
            products(t, b, &h, &Idx::Int(0), mm.bt)?;
            t.range(mm.bt, |t, bt| epilogue(t, b, &h, &bt))
        } else if mm.bt == 1 {
            batch(t, &Idx::Int(0))
        } else {
            t.range(mm.bt, |t, bt| batch(t, &bt))
        }
    })
}

/// `knobs` as the kernel for `mm`, epilogue `e` into `out` takes them on
/// `mach`: a kernel knob that leaves the kernel unchanged is off.
pub fn effective(mm: &Mm, e: &E, out: &Operand, knobs: &Knobs, mach: &Machine) -> Knobs {
    let r = ring::resolve(mm, &computed(e, knobs), out, knobs, mach);
    Knobs {
        batch_tiles: knobs.batch_tiles && mm.bt > 1 && r.depth == 0,
        ring_depth: r.depth,
        split_vc: r.parts > 1,
        tail_bands: r.tail_bands,
        activation_fp16: takes_f16(mm.k, mm.nk, knobs, mach),
        quant_ring: r.quant,
        expr_cse: takes_cse(e, knobs),
        ..*knobs
    }
}

/// Whether the kernel interns epilogue `e` under `knobs`: `expr_cse` and
/// some two of its nodes structurally equal.
pub fn takes_cse(e: &E, knobs: &Knobs) -> bool {
    knobs.expr_cse && expr::intern::merges(e)
}

/// The epilogue the kernel computes for `e` under `knobs`.
fn computed(e: &E, knobs: &Knobs) -> E {
    if takes_cse(e, knobs) {
        expr::intern::intern(e)
    } else {
        e.clone()
    }
}

/// Tile (`ti`, `tj`) of batch `bt` into the [bt * mp, N] scratch `h`.
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
    let arow = bt.mul(mm.mp).add(ti.mul(mm.tm));
    let c0 = tj.mul(mm.tn);
    let dst = h.at(&[sl(&arow, arow.add(mm.tm)), sl(&c0, c0.add(mm.tn))])?;
    product(t, b, mm, bt, &arow, tj, &dst, hw)
}

/// One tile, A rows from `arow`, B's N tile `tj` of batch `bt`: zero, a gemm
/// a K chunk, drained into `dst`.
#[allow(clippy::too_many_arguments)]
fn product(
    t: &mut Trace,
    b: &[Buf],
    mm: &Mm,
    bt: &Idx,
    arow: &Idx,
    tj: &Idx,
    dst: &View,
    hw: &Hw,
) -> R<()> {
    let acc = t.zero(mm.tm, mm.tn)?;
    let brow = if mm.shared {
        tj.mul(mm.tn)
    } else {
        bt.mul(mm.n).add(tj.mul(mm.tn))
    };
    let kc = mm.kc(hw);
    let mut acc = acc;
    t.range(mm.kp / kc, |t, k| {
        let k0 = k.mul(kc);
        let a = t.fill(&b[0].at(&[sl(arow, arow.add(mm.tm)), sl(&k0, k0.add(kc))])?)?;
        let bv = t.fill(&b[1].at(&[sl(&brow, brow.add(mm.tn)), sl(&k0, k0.add(kc))])?)?;
        acc = t.gemm(a, bv, acc)?;
        Ok(())
    })?;
    t.drain(acc, dst)
}
