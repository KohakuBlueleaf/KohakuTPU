//! Spec-vector mutants (`software/tests/spec/runners/isa.py`, `@mutant`):
//! with the `mutants` feature a thread-local switch names the one active
//! mutant and each mutated point asks [`on`]; without it [`on`] is `false`
//! and the points compile out. A layout mutant edits the field tables each
//! unit's `new` builds (`vector`, `cluster`, `mover`, `sru`), so every caller
//! of a unit ISA sees it; the rest are read where the value is made (an
//! encoder, a config, a machine-derived parameter).

#[cfg(feature = "mutants")]
use std::cell::Cell;

#[cfg(feature = "mutants")]
thread_local! {
    static ACTIVE: Cell<Option<&'static str>> = const { Cell::new(None) };
}

/// Whether mutant `name` is active on this thread.
#[inline(always)]
pub fn on(name: &str) -> bool {
    #[cfg(feature = "mutants")]
    {
        ACTIVE.with(|a| a.get() == Some(name))
    }
    #[cfg(not(feature = "mutants"))]
    {
        let _ = name;
        false
    }
}

/// `f()` with mutant `name` active on this thread.
#[cfg(feature = "mutants")]
pub fn with<T>(name: &'static str, f: impl FnOnce() -> T) -> T {
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            ACTIVE.with(|a| a.set(None));
        }
    }
    ACTIVE.with(|a| a.set(Some(name)));
    let _reset = Reset;
    f()
}

/// Every mutant ktpu-isa carries, by its Python name.
pub const NAMES: &[&str] = &[
    "rul.014/profile-reloaded",
    "rul.053/fault-dropped",
    "rul.048/fill-wraps",
    "rul.049/reserved-copy",
    "rul.050/unaligned",
    "isa.vector/math_fields",
    "isa.vector/math_regs",
    "isa.vector/math_sel",
    "isa.vector/math_om",
    "isa.vector/opcodes",
    "isa.vector/cfg_sel",
    "isa.vector/cfg_fields",
    "isa.vector/chkvl_vl",
    "isa.vector/walk_fields",
    "isa.vector/walk_ops",
    "isa.vector/modes",
    "isa.vector/sync_fields",
    "isa.vector/ctl_fields",
    "isa.vector/ctl_ops",
    "isa.vector/dma_fields",
    "isa.vector/dma_ops",
    "isa.vector/decoder",
    "isa.vector/e8m15",
    "num.e8m15/subnormals",
    "num.e8m15/truncate",
    "num.e8m15/significand-17",
    "num.e8m15/no-overflow",
    "num.mover/quant-id",
    "num.mover/gt4-id",
    "num.mover/quant-b-dropped",
    "num.fp16/no-saturation",
    "isa.vector/cu_imem",
    "isa.vector/cu_desc",
    "isa.vector/cu_split",
    "isa.vector/cu_run",
    "isa.vector/dim",
    "isa.vector/regs",
    "isa.vector/consts",
    "isa.vector/any_crossbar",
    "isa.vector/isa_cfg",
    "isa.vector/model_queues",
    "isa.vector/src_sel",
    "isa.vector/e8m15_f16",
    "isa.vector/dim_word",
    "isa.vector/xbar_c",
    "isa.vector/lower",
    "isa.mover/regs",
    "isa.mover/ctrl",
    "isa.mover/hdr",
    "isa.mover/fields",
    "isa.mover/modes",
    "isa.mover/limits",
    "isa.mover/xform",
    "isa.cluster/fields",
    "isa.cluster/opcodes",
    "isa.cluster/cfg",
    "isa.cluster/split",
    "isa.cluster/coord",
    "isa.cluster/nk",
    "isa.cluster/plan",
    "isa.cluster/bank",
    "isa.cluster/limits",
    "isa.sru/formats",
    "isa.sru/map",
    "isa.sru/schedule",
    "machine/fact_read",
];

/// Mutants no spec vector names, witnessed by this crate's own tests: a
/// field value too wide for its field wraps instead of being refused; the
/// decoder leaves a walk's `reg` without its bits past 5.
pub const LOCAL: &[&str] = &["isa.layout/fit_wraps", "isa.vector/rejoin_walk"];

#[cfg(feature = "mutants")]
pub(crate) use layouts::{cluster, mover, sru, vector};
#[cfg(not(feature = "mutants"))]
pub(crate) use off::{cluster, mover, sru, vector};

#[cfg(not(feature = "mutants"))]
mod off {
    #[inline(always)]
    pub(crate) fn vector(_: &mut crate::VecIsa) {}
    #[inline(always)]
    pub(crate) fn cluster(_: &mut crate::ClusterIsa) {}
    #[inline(always)]
    pub(crate) fn mover(_: &mut crate::MoverIsa) {}
    #[inline(always)]
    pub(crate) fn sru(_: &mut crate::SruIsa) {}
}

#[cfg(feature = "mutants")]
mod layouts {
    use super::on;
    use crate::layout::{Format, InstSet};
    use crate::unit::cluster::{ClusterCfg, ClusterIsa};
    use crate::unit::mover::MoverIsa;
    use crate::unit::sru::SruIsa;
    use crate::unit::vector::core::{
        MathOp, VBAR, VDRAIN, VFILL, VLOOP, VPACK, VSETI, VSETVL, VSKIPZ, VSYNC, VUNPK,
    };
    use crate::unit::vector::{VecCfg, VecIsa, OP_IMEM};

    /// `f` with fields `a` and `b` trading places.
    fn swap(f: &Format, a: &str, b: &str) -> Format {
        let mut fields = f.fields.clone();
        let i = fields.iter().position(|x| x.name == a).unwrap();
        let j = fields.iter().position(|x| x.name == b).unwrap();
        fields.swap(i, j);
        Format::new(f.name, f.width, f.clause, fields)
    }

    /// `f` with fixed field `name` holding `v`.
    fn konst(f: &Format, name: &str, v: u64) -> Format {
        let mut fields = f.fields.clone();
        fields.iter_mut().find(|x| x.name == name).unwrap().konst = Some(v);
        Format::new(f.name, f.width, f.clause, fields)
    }

    /// `f` with field `name` `width` bits wide.
    fn resize(f: &Format, name: &str, width: u32) -> Format {
        let mut fields = f.fields.clone();
        fields.iter_mut().find(|x| x.name == name).unwrap().width = width;
        Format::new(f.name, f.width, f.clause, fields)
    }

    fn cluster_each(c: &mut ClusterIsa, f: impl Fn(&Format) -> Format) {
        c.fill = f(&c.fill);
        c.gemm = f(&c.gemm);
        c.drain = f(&c.drain);
        c.post = f(&c.post);
    }

    /// The active vector layout mutant applied to the tables `VecIsa::new`
    /// built.
    pub(crate) fn vector(isa: &mut VecIsa) {
        let c = &mut isa.core;
        let maths = |c: &mut crate::unit::vector::CoreFormats, a: &str, b: &str| {
            for (_, f) in c.maths.iter_mut() {
                *f = swap(f, a, b);
            }
        };
        if on("isa.vector/math_fields") {
            maths(c, "vb", "vc");
        } else if on("isa.vector/math_regs") {
            maths(c, "vd", "va");
        } else if on("isa.vector/math_sel") {
            maths(c, "sa", "sb");
        } else if on("isa.vector/math_om") {
            maths(c, "sc", "om");
        } else if on("isa.vector/opcodes") {
            use MathOp::*;
            for (a, b) in [
                (Vadd, Vsub),
                (Vmul, Vmax),
                (Vexp2, Vlog2),
                (Vinv, Vrsqrt),
                (Vfma, Vfnma),
                (Vcmpgt, Vcmplt),
                (Vmov, Vneg),
                (Vsel, Vmin),
                (Vexp2d, Vabs),
            ] {
                let (i, j) = (a.code() as usize, b.code() as usize);
                let fa = c.maths[i].1.clone();
                c.maths[i].1 = c.maths[j].1.clone();
                c.maths[j].1 = fa;
            }
        } else if on("isa.vector/cfg_sel") {
            c.chk = konst(&c.chk, "sel", 11);
            c.chkvl = konst(&c.chkvl, "sel", 11);
            c.xb = konst(&c.xb, "sel", 2);
            c.getstk = konst(&c.getstk, "sel", 9);
            c.ainc = konst(&c.ainc, "sel", 8);
            c.ustr = konst(&c.ustr, "sel", 5);
            c.pstr = konst(&c.pstr, "sel", 6);
            c.uptr = konst(&c.uptr, "sel", 3);
            c.uinc = konst(&c.uinc, "sel", 2);
        } else if on("isa.vector/cfg_fields") {
            c.chk = swap(&c.chk, "a_base", "d_base");
            c.chkvl = swap(&c.chkvl, "a_base", "d_base");
            c.xb = swap(&swap(&c.xb, "pm", "pr"), "xm", "xc");
            c.ustr = swap(&c.ustr, "s0", "s1");
            c.pstr = swap(&c.pstr, "s0", "s1");
            c.uptr = swap(&c.uptr, "z", "ptr");
            c.uinc = swap(&c.uinc, "z", "ptr");
            c.ainc = swap(&c.ainc, "ad", "z");
            c.getstk = swap(&c.getstk, "z", "sreg");
        } else if on("isa.vector/walk_fields") {
            c.unpk = swap(&c.unpk, "n1", "cb");
            c.pack = swap(&c.pack, "mode", "rel");
        } else if on("isa.vector/walk_ops") {
            c.unpk = konst(&c.unpk, "op", VPACK);
            c.pack = konst(&c.pack, "op", VUNPK);
        } else if on("isa.vector/sync_fields") {
            c.sync = swap(&c.sync, "waiter", "on");
            c.mark = swap(&c.mark, "on", "mark");
            c.wait = swap(&c.wait, "q", "counter");
        } else if on("isa.vector/ctl_fields") {
            c.seti = swap(&c.seti, "z1", "sreg");
            c.lp = swap(&c.lp, "sreg", "z1");
            c.skipz = swap(&c.skipz, "z", "skip");
            c.bar = swap(&c.bar, "z1", "q");
            c.halt = konst(&c.halt, "op", VBAR);
            c.post = swap(&c.post, "dst_x", "dst_y");
            c.setvl = swap(&c.setvl, "z1", "sreg");
        } else if on("isa.vector/ctl_ops") {
            c.seti = konst(&c.seti, "op", VLOOP);
            c.lp = konst(&c.lp, "op", VSETI);
            c.skipz = konst(&c.skipz, "op", VSETVL);
            c.post = konst(&c.post, "op", VSKIPZ);
            c.sync = konst(&c.sync, "op", VBAR);
            c.mark = konst(&c.mark, "op", VBAR);
            c.wait = konst(&c.wait, "op", VBAR);
            c.bar = konst(&c.bar, "op", VSYNC);
        } else if on("isa.vector/dma_fields") {
            c.fill = swap(&c.fill, "rel", "z1");
            c.drain = swap(&swap(&c.drain, "dst_x", "dst_y"), "signal", "node");
        } else if on("isa.vector/dma_ops") {
            c.fill = konst(&c.fill, "op", VDRAIN);
            c.drain = konst(&c.drain, "op", VFILL);
        } else if on("isa.vector/cu_imem") {
            let alt = VecIsa::of(VecCfg {
                addr_bits: 10,
                addr_hi_bits: 1,
                ..isa.cfg.clone()
            });
            isa.imem = alt.imem;
        } else if on("isa.vector/cu_desc") {
            let d = &isa.desc;
            let pad = d.field("_pad").unwrap().width;
            isa.desc = swap(&resize(d, "_pad", pad - 1), "ad", "fld");
        } else if on("isa.vector/cu_run") {
            isa.run = konst(&resize(&isa.run, "pc", 10), "op", OP_IMEM);
        } else if on("isa.vector/isa_cfg") {
            isa.cfg.vlmax = 64;
            isa.cfg.lanes = 8;
            isa.cfg.word_bytes = 16;
        }
        isa.set = InstSet::new(
            "kohakutpu-vec",
            vec![isa.imem.clone(), isa.desc.clone(), isa.run.clone()],
        );
        let c = &mut isa.core;
        c.set = InstSet::new("kohakutpu-v2", c.formats());
        if on("isa.vector/decoder") {
            let mut all = c.formats();
            all.retain(|f| f.name != "VSYNC" && f.name != "VCFG");
            let mut order = vec![c.sync.clone(), c.cfg.clone()];
            order.extend(all);
            c.set = InstSet::new("kohakutpu-v2", order);
        }
    }

    /// The active cluster layout mutant applied to the tables
    /// `ClusterIsa::new` built.
    pub(crate) fn cluster(isa: &mut ClusterIsa) {
        if on("isa.cluster/fields") {
            cluster_each(isa, |f| {
                [
                    ("n", "sel"),
                    ("gm", "gn"),
                    ("eoff", "aoff"),
                    ("emit", "fuse"),
                    ("dst_x", "dst_y"),
                    ("addr_hi", "dfin"),
                    ("abank", "bbank"),
                    ("dflags", "dbuf"),
                    ("recv", "npeer"),
                ]
                .iter()
                .fold(f.clone(), |f, (a, b)| swap(&f, a, b))
            });
        } else if on("isa.cluster/opcodes") {
            isa.fill = konst(&isa.fill, "op", 2);
            isa.gemm = konst(&isa.gemm, "op", 1);
            isa.drain = konst(&isa.drain, "op", 4);
            isa.post = konst(&isa.post, "op", 3);
        } else if on("isa.cluster/cfg") {
            *isa = ClusterIsa::of(ClusterCfg {
                anchor: 41,
                max_fill: 256,
                max_peers: 4,
                kstep: 1,
                ..isa.cfg.clone()
            });
        } else if on("isa.cluster/nk") {
            isa.cfg.kstep = 1;
        }
        isa.set = InstSet::new(
            "kohakutpu-cu",
            vec![
                isa.fill.clone(),
                isa.gemm.clone(),
                isa.drain.clone(),
                isa.post.clone(),
            ],
        );
    }

    /// The active mover layout mutant applied to the tables `MoverIsa::new`
    /// built.
    pub(crate) fn mover(m: &mut MoverIsa) {
        if on("isa.mover/limits") {
            m.levels = 7;
            m.move_words = 1 << 13;
            m.max_index = 4096;
            m.word_bytes = 16;
            m.addr_bits = 41;
        } else if on("isa.mover/modes") {
            m.ewidths = [1, 0, 3];
            let m = &mut m.modes;
            m.copy = 7;
            m.fill = 3;
            m.generate = 4;
            m.gather = 6;
            m.scatter = 2;
            m.transform = 1;
        }
    }

    /// The active SRU layout mutant applied to the tables `SruIsa::new`
    /// built.
    pub(crate) fn sru(s: &mut SruIsa) {
        if on("isa.sru/formats") {
            s.cfg = konst(&s.cfg, "op", crate::unit::sru::OP_LOAD);
            s.load = swap(&s.load, "first", "what");
            s.run = swap(&swap(&s.run, "amem", "peer"), "px", "py");
            s.store = swap(&s.store, "_pad", "first");
            s.post = swap(&s.post, "x", "y");
        }
        s.set = InstSet::new(
            "kohakutpu-sru",
            vec![
                s.cfg.clone(),
                s.load.clone(),
                s.run.clone(),
                s.store.clone(),
                s.post.clone(),
            ],
        );
    }
}
