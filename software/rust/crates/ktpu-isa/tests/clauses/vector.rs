//! `docs/spec/stack/isa/vector.md`.

use super::{at, isa, machine, refused};
use ktpu_isa::unit::vector::core::*;
use ktpu_isa::unit::vector::dim;
use ktpu_isa::{Coord, VWord, VecCu, VecIsa, W256};

fn enc(w: VWord) -> u64 {
    w.encode(&isa().vector).unwrap()
}

fn with_regs(regs: u32) -> VecIsa {
    let mut cfg = isa().vector.cfg.clone();
    cfg.regs = regs;
    VecIsa::of(cfg)
}

fn math(op: MathOp, vd: u32, va: u32, vb: u32, vc: u32) -> VWord {
    VWord::Math {
        op,
        vd,
        va,
        vb,
        vc,
        sa: Src::V,
        sb: Src::V,
        sc: Src::V,
        om: false,
    }
}

#[test]
fn isa_vc_004_high_register_bits() {
    let v = with_regs(128);
    let w = math(MathOp::Vadd, 127, 64, 33, 100).encode(&v).unwrap();
    assert_eq!(w & 0xFFFF_FFFF >> 27 << 27, 0x03 << 27);
    let (name, f) = v.decode_word(w).unwrap();
    let regs: Vec<i64> = ["vd", "va", "vb", "vc"]
        .iter()
        .map(|n| ktpu_isa::layout::value(&f, n))
        .collect();
    assert_eq!((name, regs), ("VADD", vec![127, 64, 33, 100]));
    let p = VWord::Pack {
        vs: 70,
        off: 1,
        n: 8,
        mode: WalkMode::F16,
        cb: 0,
        rel: false,
        q: 0,
    };
    let (_, f) = v.decode_word(p.encode(&v).unwrap()).unwrap();
    assert_eq!(ktpu_isa::layout::value(&f, "reg"), 70);
    assert_eq!(
        (
            (w >> 22) & 31,
            (w >> 17) & 31,
            (w >> 12) & 31,
            (w >> 7) & 31
        ),
        (31, 0, 1, 4)
    );
    assert_eq!(w >> 32, 3 | 1 << 2 | 2 << 4 | 3 << 6);
    refused(
        math(MathOp::Vadd, 32, 0, 0, 0).encode(&with_regs(32)),
        "ISA.VC.004",
    );
    refused(math(MathOp::Vadd, 128, 0, 0, 0).encode(&v), "ISA.VC.004");
    let unpk = VWord::Unpk {
        vd: 100,
        off: 0,
        n: 8,
        mode: WalkMode::F16,
        cb: 0,
        rel: false,
        q: 0,
    };
    assert_eq!(unpk.encode(&v).unwrap() >> 32, 3 << 6);
    let imem = VecCu::Imem {
        addr: 0,
        word: 1 << 40,
    };
    refused(imem.encode(&v), "ISA.VC.004");
}

#[test]
fn isa_vc_004_a_64_register_core_puts_one_bit_per_field_past_bit_5() {
    let v = with_regs(64);
    let w = math(MathOp::Vfma, 33, 34, 2, 63).encode(&v).unwrap();
    assert_eq!(w >> 32, 0b1101);
    let (name, f) = v.decode_word(w).unwrap();
    let regs: Vec<i64> = ["vd", "va", "vb", "vc"]
        .iter()
        .map(|n| ktpu_isa::layout::value(&f, n))
        .collect();
    assert_eq!((name, regs), ("VFMA", vec![33, 34, 2, 63]));
    refused(math(MathOp::Vfma, 64, 0, 0, 0).encode(&v), "ISA.VC.004");
}

#[test]
fn isa_vc_004_an_unpack_past_register_31_decodes_whole() {
    let v = with_regs(128);
    let w = VWord::Unpk {
        vd: 70,
        off: 0,
        n: 8,
        mode: WalkMode::F16,
        cb: 0,
        rel: false,
        q: 0,
    };
    let (name, f) = v.decode_word(w.encode(&v).unwrap()).unwrap();
    assert_eq!((name, ktpu_isa::layout::value(&f, "reg")), ("VUNPK", 70));
}

/// The two tests above, each failing under the mutant that breaks what it
/// checks: the high register bits' placement, and the walk's rejoin.
#[cfg(feature = "mutants")]
#[test]
fn the_high_register_tests_fail_under_their_mutants() {
    use ktpu_isa::mutant::with;
    let v = with_regs(64);
    let hi = |v: &VecIsa| math(MathOp::Vfma, 33, 34, 2, 63).encode(v).unwrap() >> 32;
    assert_eq!(hi(&v), 0b1101);
    assert_ne!(with("isa.vector/regs", || hi(&v)), 0b1101);
    let v = with_regs(128);
    let w = VWord::Unpk {
        vd: 70,
        off: 0,
        n: 8,
        mode: WalkMode::F16,
        cb: 0,
        rel: false,
        q: 0,
    }
    .encode(&v)
    .unwrap();
    let reg = |v: &VecIsa| ktpu_isa::layout::value(&v.decode_word(w).unwrap().1, "reg");
    assert_eq!(reg(&v), 70);
    assert_eq!(with("isa.vector/rejoin_walk", || reg(&v)), 70 & 31);
}

#[test]
fn isa_vc_012_imem_and_run_reach() {
    let v = isa().vector;
    let m = machine();
    assert_eq!(v.cfg.imem_words(), m.vc.imem_words);
    assert_eq!(1 << v.cfg.addr_bits, m.vc.run_reach);
}

#[test]
fn isa_vc_070_the_machine_names_the_encoder_crossbar_modes() {
    let less = machine()
        .with_fact("vc.crossbar_modes", serde_json::json!(["none", "xor"]))
        .unwrap();
    let refusal = ktpu_isa::unit::vector::VecCfg::of(&less).unwrap_err();
    assert_eq!(refusal.clause, "ISA.VC.070");
    #[cfg(feature = "mutants")]
    assert!(ktpu_isa::mutant::with("isa.vector/any_crossbar", || {
        ktpu_isa::unit::vector::VecCfg::of(&less).is_ok()
    }));
}

#[test]
fn isa_vc_030_cu_opcodes() {
    let v = isa().vector;
    let ops: Vec<u64> = [
        VecCu::Imem { addr: 0, word: 0 },
        VecCu::Desc {
            ad: 0,
            fld: 0,
            value: 0,
        },
        VecCu::Run { pc: 0 },
    ]
    .iter()
    .map(|c| c.encode(&v).unwrap().get(252, 4))
    .collect();
    assert_eq!(ops, [1, 2, 3]);
}

#[test]
fn isa_vc_031_imem_layout() {
    let v = isa().vector;
    at(&v.imem, "addr", 251, 243);
    at(&v.imem, "addr_hi", 242, 242);
    at(&v.imem, "word", 31, 0);
    let p = VecCu::Imem {
        addr: 600,
        word: 0x12_3456_78AB,
    }
    .encode(&v)
    .unwrap();
    assert_eq!(
        (
            p.get(243, 9) | p.get(242, 1) << 9,
            p.get(0, 32),
            p.get(32, 8)
        ),
        (600, 0x3456_78AB, 0x12)
    );
    assert_eq!((v.imem_addr(p), v.imem_word(p)), (600, 0x12_3456_78AB));
    refused(
        VecCu::Imem {
            addr: 1024,
            word: 0,
        }
        .encode(&v),
        "ISA.VC.031",
    );
}

#[test]
fn isa_vc_032_a_32_register_word_runs_on_128() {
    let w = math(MathOp::Vmul, 31, 30, 29, 28);
    assert_eq!(
        w.encode(&with_regs(32)).unwrap(),
        w.encode(&with_regs(128)).unwrap()
    );
    assert_eq!(w.encode(&with_regs(128)).unwrap() >> 32, 0);
}

#[test]
fn isa_vc_034_desc_layout() {
    let v = isa().vector;
    at(&v.desc, "ad", 251, 249);
    at(&v.desc, "fld", 248, 246);
    at(&v.desc, "value", 245, 212);
    at(&v.desc, "value_hi", 68, 63);
    let a = 0xAB_CDEF_0120u64;
    let p = VecCu::Desc {
        ad: 7,
        fld: 0,
        value: a,
    }
    .encode(&v)
    .unwrap();
    assert_eq!(p.get(212, 34) | p.get(63, 6) << 34, a);
    assert_eq!(v.addresses(p)[0].value, a);
    let d = dim(-576, 5).unwrap();
    assert_eq!((d >> 16, d & 0xFFFF), ((-576i64 as u64) & 0x3FFFF, 5));
    let p = VecCu::Desc {
        ad: 1,
        fld: 2,
        value: d,
    }
    .encode(&v)
    .unwrap();
    assert_eq!(p.get(212, 34), d);
    assert!(v.addresses(p).is_empty());
    refused(
        VecCu::Desc {
            ad: 1,
            fld: 0,
            value: 1 << 40,
        }
        .encode(&v),
        "ISA.VC.034",
    );
    refused(
        VecCu::Desc {
            ad: 1,
            fld: 1,
            value: 1 << 34,
        }
        .encode(&v),
        "ISA.VC.034",
    );
    refused(dim(32, 1 << 16), "ISA.VC.034");
}

#[test]
fn isa_vc_035_run_pc() {
    let v = isa().vector;
    at(&v.run, "pc", 251, 243);
    assert_eq!(VecCu::Run { pc: 511 }.encode(&v).unwrap().get(243, 9), 511);
    refused(VecCu::Run { pc: 512 }.encode(&v), "ISA.VC.035");
}

#[test]
fn isa_vc_040_opcodes() {
    let ops = [
        (enc(VWord::Vcfg { sel: 0, payload: 0 }), VCFG),
        (
            enc(VWord::Unpk {
                vd: 0,
                off: 0,
                n: 8,
                mode: WalkMode::F16,
                cb: 0,
                rel: false,
                q: 0,
            }),
            VUNPK,
        ),
        (
            enc(VWord::Pack {
                vs: 0,
                off: 0,
                n: 8,
                mode: WalkMode::F16,
                cb: 0,
                rel: false,
                q: 0,
            }),
            VPACK,
        ),
        (
            enc(VWord::Sync {
                waiter: 0,
                on: 0,
                slack: 0,
                mark: None,
                q: 0,
            }),
            VSYNC,
        ),
        (
            enc(VWord::Seti {
                sreg: 0,
                to_k: false,
            }),
            VSETI,
        ),
        (enc(VWord::Loop { sreg: 0, body: 1 }), VLOOP),
        (enc(VWord::Bar { q: 0 }), VBAR),
        (
            enc(VWord::Fill {
                ad: 0,
                l1off: 0,
                rel: false,
            }),
            VFILL,
        ),
        (
            enc(VWord::Drain {
                ad: 0,
                l1off: 0,
                rel: false,
                node: None,
                buf_id: 0,
                signal: false,
            }),
            VDRAIN,
        ),
        (enc(VWord::Halt), VHALT),
    ];
    for (w, code) in ops {
        assert_eq!(w >> 27, code, "{w:#x}");
    }
    assert_eq!(
        (VCFG, VUNPK, VPACK, VSYNC, VSETI, VLOOP, VBAR, VFILL, VDRAIN, VHALT),
        (0x13, 0x14, 0x15, 0x16, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F)
    );
}

#[test]
fn isa_vc_041_field_layouts() {
    let c = &isa().vector.core;
    let m = c.math(MathOp::Vfma);
    for (n, hi, lo) in [
        ("vd", 26, 22),
        ("va", 21, 17),
        ("vb", 16, 12),
        ("vc", 11, 7),
        ("sa", 6, 5),
        ("sb", 4, 3),
        ("sc", 2, 1),
        ("om", 0, 0),
    ] {
        at(m, n, hi, lo);
    }
    at(&c.cfg, "sel", 26, 23);
    at(&c.cfg, "payload", 22, 0);
    for f in [&c.unpk, &c.pack] {
        for (n, hi, lo) in [
            ("reg", 26, 22),
            ("mode", 21, 20),
            ("n1", 19, 17),
            ("cb", 16, 14),
            ("rel", 13, 13),
            ("q", 12, 12),
            ("off", 11, 0),
        ] {
            at(f, n, hi, lo);
        }
    }
    for (n, hi, lo) in [
        ("waiter", 26, 24),
        ("on", 23, 21),
        ("slack", 20, 15),
        ("marked", 14, 14),
        ("mark", 13, 11),
        ("q", 10, 10),
    ] {
        at(&c.sync, n, hi, lo);
    }
    at(&c.seti, "sreg", 25, 22);
    at(&c.seti, "to_k", 0, 0);
    at(&c.lp, "sreg", 20, 17);
    at(&c.lp, "body", 16, 7);
    at(&c.bar, "q", 10, 10);
    at(&c.fill, "rel", 26, 26);
    at(&c.fill, "ad", 23, 21);
    at(&c.fill, "off", 13, 0);
    for (n, hi, lo) in [
        ("rel", 26, 26),
        ("signal", 25, 25),
        ("node", 24, 24),
        ("ad", 23, 21),
        ("dst_x", 20, 17),
        ("dst_y", 16, 13),
        ("buf_id", 12, 9),
        ("off", 8, 0),
    ] {
        at(&c.drain, n, hi, lo);
    }
}

#[test]
fn isa_vc_042_unused_bits_are_zero() {
    assert_eq!(enc(VWord::Halt), 0x1F << 27);
    assert_eq!(enc(VWord::Bar { q: 1 }), 0x1C << 27 | 1 << 10);
    let w = enc(VWord::Getstk { sreg: 15 });
    assert_eq!(w, 0x13 << 27 | 8 << 23 | 15);
}

#[test]
fn isa_vc_043_too_wide_is_refused() {
    let v = isa().vector;
    refused(
        VWord::Unpk {
            vd: 0,
            off: 4096,
            n: 8,
            mode: WalkMode::F16,
            cb: 0,
            rel: false,
            q: 0,
        }
        .encode(&v),
        "ISA.VC.043",
    );
    refused(
        VWord::Wait {
            waiter: 1,
            counter: 0,
            n: 1024,
            q: 0,
        }
        .encode(&v),
        "ISA.VC.043",
    );
    refused(
        VWord::Ainc {
            ad: 0,
            inc: 1 << 17,
        }
        .encode(&v),
        "ISA.VC.043",
    );
    refused(
        VWord::Post {
            dst: Coord(16, 0),
            n: 1,
        }
        .encode(&v),
        "ISA.VC.043",
    );
}

#[test]
fn isa_vc_044_wait_mark_post_skipz() {
    let c = &isa().vector.core;
    at(&c.wait, "n", 20, 11);
    at(&c.wait, "q", 10, 10);
    at(&c.wait, "counter", 9, 9);
    at(&c.post, "dst_x", 26, 23);
    at(&c.post, "dst_y", 22, 19);
    at(&c.post, "n", 15, 0);
    at(&c.skipz, "sreg", 25, 22);
    at(&c.skipz, "skip", 7, 0);
    let mark = enc(VWord::Mark {
        mark: 3,
        on: K_FILL,
        slack: 2,
    });
    assert_eq!(mark, VSYNC << 27 | (W_MK as u64) << 24 | 2 << 15 | 3 << 11);
    let wait = enc(VWord::Wait {
        waiter: W_U,
        counter: RX,
        n: 8,
        q: 0,
    });
    assert_eq!(
        wait,
        VSYNC << 27 | (W_U as u64) << 24 | (K_COUNT as u64) << 21 | 8 << 11
    );
    assert_eq!(
        enc(VWord::Post {
            dst: Coord(1, 2),
            n: 5
        }) >> 27,
        VPOST
    );
    assert_eq!(enc(VWord::Skipz { sreg: 14, n: 3 }) >> 27, VSKIPZ);
}

#[test]
fn isa_vc_045_setvl() {
    let c = &isa().vector.core;
    at(&c.setvl, "sreg", 20, 17);
    assert_eq!(enc(VWord::Setvl { sreg: 3 }), VSETVL << 27 | 3 << 17);
}

#[test]
fn isa_vc_046_decoder_rejects_a_nonzero_unused_bit() {
    let c = &isa().vector.core;
    let halt = W256::from_u64(0x1F << 27);
    assert_eq!(c.set.decode(halt).unwrap().0, "VHALT");
    assert!(c.set.decode(W256::from_u64(0x1F << 27 | 1)).is_err());
}

#[test]
fn isa_vc_050_operand_selectors() {
    let w = enc(VWord::Math {
        op: MathOp::Vmul,
        vd: 20,
        va: 0,
        vb: 4,
        vc: 0,
        sa: Src::S,
        sb: Src::K,
        sc: Src::V,
        om: false,
    });
    assert_eq!(((w >> 5) & 3, (w >> 3) & 3, (w >> 1) & 3), (1, 3, 0));
}

#[test]
fn isa_vc_054_opcodes_of_the_math_ops() {
    let want = [
        (MathOp::Vmov, 0x00),
        (MathOp::Vadd, 0x03),
        (MathOp::Vmul, 0x05),
        (MathOp::Vmax, 0x08),
        (MathOp::Vsel, 0x0A),
        (MathOp::Vexp2, 0x0E),
        (MathOp::Vlog2, 0x0F),
        (MathOp::Vinv, 0x10),
        (MathOp::Vrsqrt, 0x11),
        (MathOp::Vsub, 0x04),
        (MathOp::Vfma, 0x06),
        (MathOp::Vfnma, 0x07),
        (MathOp::Vexp2d, 0x12),
        (MathOp::Vneg, 0x01),
        (MathOp::Vabs, 0x02),
        (MathOp::Vmin, 0x09),
        (MathOp::Vcmplt, 0x0B),
        (MathOp::Vcmpgt, 0x0C),
        (MathOp::Vcmpeq, 0x0D),
    ];
    for (op, code) in want {
        assert_eq!(enc(math(op, 1, 0, 0, 0)) >> 27, code, "{op:?}");
        assert_eq!(MathOp::parse(op.mnemonic()), Some(op));
    }
}

#[test]
fn isa_vc_070_078_xb_payload() {
    let w = enc(VWord::Xb {
        xm: 3,
        xk: 5,
        sh: 3,
        xc: true,
        pm: 1,
        pr: 2,
    });
    let p = w & 0x7F_FFFF;
    assert_eq!((w >> 23) & 15, CFG_XB);
    assert_eq!(
        (
            p & 7,
            (p >> 3) & 15,
            (p >> 7) & 7,
            (p >> 10) & 1,
            (p >> 11) & 3,
            (p >> 13) & 3
        ),
        (3, 5, 3, 1, 1, 2)
    );
}

#[test]
fn isa_vc_090_selectors() {
    let sel = |w: VWord| (enc(w) >> 23) & 15;
    let ch = Chunk::default();
    assert_eq!(
        sel(VWord::Chk {
            a: ch,
            b: ch,
            c: ch,
            d: ch
        }),
        0
    );
    assert_eq!(
        sel(VWord::Stride {
            reg: StrideReg::Ustr,
            s0: 1,
            s1: 4
        }),
        4
    );
    assert_eq!(
        sel(VWord::Stride {
            reg: StrideReg::Pstr,
            s0: 1,
            s1: 4
        }),
        7
    );
    assert_eq!(sel(VWord::Ainc { ad: 0, inc: 0 }), 9);
    assert_eq!(
        sel(VWord::Chkvl {
            vl: 1,
            a: ch,
            b: ch,
            c: ch,
            d: ch
        }),
        10
    );
    assert_eq!(
        sel(VWord::Ptr {
            reg: PtrReg::Uptr,
            value: 0
        }),
        2
    );
    assert_eq!(
        sel(VWord::Ptr {
            reg: PtrReg::Uinc,
            value: 0
        }),
        3
    );
    assert_eq!(sel(VWord::Getstk { sreg: 0 }), 8);
    assert_eq!(
        sel(VWord::Ptr {
            reg: PtrReg::Pptr,
            value: 0
        }),
        5
    );
    assert_eq!(
        sel(VWord::Ptr {
            reg: PtrReg::Pinc,
            value: 0
        }),
        6
    );
}

#[test]
fn isa_vc_091_chk_and_chkvl_payload() {
    let w = enc(VWord::Chkvl {
        vl: 64,
        a: Chunk(1, 0),
        b: Chunk(7, 1),
        c: Chunk(4, 0),
        d: Chunk(2, 1),
    });
    let p = w & 0x7F_FFFF;
    assert_eq!(p >> 16, 63);
    assert_eq!(
        p & 0xFFFF,
        2 << 13 | 1 << 12 | 4 << 9 | 7 << 5 | 1 << 4 | 1 << 1
    );
    let reset = enc(VWord::Chk {
        a: Chunk::default(),
        b: Chunk::default(),
        c: Chunk::default(),
        d: Chunk::default(),
    });
    assert_eq!(reset & 0xFFFF, 0x1111);
}

#[test]
fn isa_vc_092_098_strides() {
    let w = enc(VWord::Stride {
        reg: StrideReg::Ustr,
        s0: 8,
        s1: 1,
    });
    assert_eq!((w & 0x1FF, (w >> 9) & 0x1FF), (8, 1));
    let w = enc(VWord::Stride {
        reg: StrideReg::Pstr,
        s0: -1,
        s1: -32,
    });
    assert_eq!((w & 0x1FF, (w >> 9) & 0x1FF), (511, 480));
}

#[test]
fn isa_vc_093_099_pointer_payload() {
    let w = enc(VWord::Ptr {
        reg: PtrReg::Uptr,
        value: 8191,
    });
    assert_eq!(w & 0x7F_FFFF, 8191);
    refused(
        VWord::Ptr {
            reg: PtrReg::Pinc,
            value: 8192,
        }
        .encode(&isa().vector),
        "ISA.VC.043",
    );
}

#[test]
fn isa_vc_094_ainc_payload() {
    let w = enc(VWord::Ainc { ad: 5, inc: -2048 });
    assert_eq!(
        ((w >> 20) & 7, w & 0x3FFFF),
        (5, (-2048i64 as u64) & 0x3FFFF)
    );
}

#[test]
fn isa_vc_095_getstk_payload() {
    assert_eq!(enc(VWord::Getstk { sreg: 14 }) & 0x7F_FFFF, 14);
}

#[test]
fn isa_vc_100_walk_fields() {
    let w = enc(VWord::Unpk {
        vd: 3,
        off: 4095,
        n: 4,
        mode: WalkMode::Gt4,
        cb: 4,
        rel: true,
        q: 1,
    });
    assert_eq!(
        (
            (w >> 17) & 7,
            (w >> 20) & 3,
            (w >> 14) & 7,
            (w >> 13) & 1,
            (w >> 12) & 1,
            w & 0xFFF
        ),
        (3, 1, 4, 1, 1, 4095)
    );
}

#[test]
fn isa_vc_110_gt4_walks_whole_groups() {
    let v = isa().vector;
    refused(
        VWord::Pack {
            vs: 0,
            off: 0,
            n: 6,
            mode: WalkMode::Gt4,
            cb: 0,
            rel: false,
            q: 0,
        }
        .encode(&v),
        "ISA.VC.110",
    );
}

#[test]
fn isa_vc_112_117_mx7_pack() {
    let a = enc(VWord::PackMx7 {
        vs: 5,
        off: 64,
        b_layout: false,
        rel: false,
        q: 0,
    });
    let b = enc(VWord::PackMx7 {
        vs: 5,
        off: 64,
        b_layout: true,
        rel: false,
        q: 0,
    });
    assert_eq!(
        (a >> 27, (a >> 20) & 3, (a >> 17) & 7, (a >> 14) & 7),
        (VPACK, 3, 7, 0)
    );
    assert_eq!((b >> 14) & 7, 1);
}

#[test]
fn isa_vc_143_155_waiters_and_kinds() {
    assert_eq!((W_FE, W_U, W_M, W_P, W_A, W_MK, W_D), (0, 1, 2, 3, 4, 5, 6));
    assert_eq!(
        (K_FILL, K_UNPACK, K_MATH, K_PACK, K_DRAIN, K_UNPACK1, K_PACK1, K_COUNT),
        (0, 1, 2, 3, 4, 5, 6, 7)
    );
    let w = enc(VWord::Sync {
        waiter: W_D,
        on: K_DRAIN,
        slack: 63,
        mark: None,
        q: 0,
    });
    assert_eq!(((w >> 24) & 7, (w >> 21) & 7, (w >> 15) & 63), (6, 4, 63));
}

#[test]
fn isa_vc_146_marked_sync() {
    let w = enc(VWord::Sync {
        waiter: W_U,
        on: 0,
        slack: 0,
        mark: Some(7),
        q: 1,
    });
    assert_eq!(((w >> 14) & 1, (w >> 11) & 7, (w >> 10) & 1), (1, 7, 1));
}

#[test]
fn isa_vc_153_vpost() {
    let w = enc(VWord::Post {
        dst: Coord(1, 2),
        n: 65535,
    });
    assert_eq!(((w >> 23) & 15, (w >> 19) & 15, w & 0xFFFF), (1, 2, 65535));
}

#[test]
fn isa_vc_160_seti() {
    let w = enc(VWord::Seti {
        sreg: 15,
        to_k: true,
    });
    assert_eq!(((w >> 22) & 15, w & 1), (15, 1));
    assert_eq!(e8m15(1.0), 0x3F8000);
    assert_eq!(e8m15(-1.0), 0xBF8000);
}

#[test]
fn isa_vc_162_vloop() {
    let w = enc(VWord::Loop {
        sreg: 15,
        body: 1023,
    });
    assert_eq!(((w >> 17) & 15, (w >> 7) & 1023), (15, 1023));
}

#[test]
fn isa_vc_165_vskipz() {
    let w = enc(VWord::Skipz { sreg: 14, n: 255 });
    assert_eq!(((w >> 22) & 15, w & 255), (14, 255));
}

#[test]
fn isa_vc_130_peer_drain() {
    let w = enc(VWord::Drain {
        ad: 3,
        l1off: 8,
        rel: true,
        node: Some(Coord(2, 1)),
        buf_id: 15,
        signal: true,
    });
    assert_eq!(
        (
            (w >> 24) & 1,
            (w >> 25) & 1,
            (w >> 17) & 15,
            (w >> 13) & 15,
            (w >> 9) & 15,
            w & 511
        ),
        (1, 1, 2, 1, 15, 8)
    );
}

#[test]
fn isa_vc_167_168_vl_range() {
    let v = isa().vector;
    let ch = Chunk::default();
    let vlmax = machine().vc.vlmax;
    assert_eq!(
        enc(VWord::Chkvl {
            vl: vlmax,
            a: ch,
            b: ch,
            c: ch,
            d: ch
        }) >> 16
            & 127,
        vlmax as u64 - 1
    );
    refused(
        VWord::Chkvl {
            vl: 0,
            a: ch,
            b: ch,
            c: ch,
            d: ch,
        }
        .encode(&v),
        "ISA.VC.168",
    );
    refused(
        VWord::Chkvl {
            vl: vlmax + 1,
            a: ch,
            b: ch,
            c: ch,
            d: ch,
        }
        .encode(&v),
        "ISA.VC.168",
    );
}
