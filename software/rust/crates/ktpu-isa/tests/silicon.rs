//! The encoders against the words the silicon ran (`compiler/tests/
//! test_isa.py`'s `SILICON` and `CORE_SILICON`, frozen when the hand-packed
//! encoders were retired): the cluster and vector-CU payloads, every V2 core
//! word, the decoder's format for each, and the refusals beside them. The
//! words are the reference, not a Python dump.

use ktpu_isa::unit::vector::core::{Chunk, MathOp, PtrReg, Src, StrideReg, VWord, WalkMode};
use ktpu_isa::{Coord, Isa, Machine, VecCu, VecIsa, W256};

/// The board the silicon words ran on.
fn isa() -> Isa {
    Isa::new(&Machine::named("multimesh_v9t6").unwrap()).unwrap()
}

fn w(hex: &str) -> W256 {
    W256::from_hex(hex).unwrap()
}

/// `(case, format, fields)`: `test_isa.py`'s `CASES` as format fields.
type Case = (
    &'static str,
    &'static str,
    &'static [(&'static str, i64)],
    &'static str,
);

const CLUSTER: &[Case] = &[
    (
        "fill_a",
        "FILL",
        &[("addr", 0x1234_5678), ("n", 64), ("sel", 0)],
        "1048D159E0010000000128000000000000000000000000000000000000000000",
    ),
    (
        "fill_b",
        "FILL",
        &[("addr", 0x3_FFFF_FFFF), ("n", 255), ("sel", 1)],
        "1FFFFFFFFC03FE00000128000000000000000000000000000000000000000000",
    ),
    (
        "fill_off",
        "FILL",
        &[
            ("addr", 0x40),
            ("n", 7),
            ("sel", 0),
            ("eoff", 200),
            ("abank", 1),
            ("fbank", 1),
        ],
        "1000000100001C00000128000000190000050000000000000000000000000000",
    ),
    (
        "gemm",
        "GEMM",
        &[("gm", 16), ("gn", 32), ("nk", 25)],
        "2000000000000010201928000000000000000000000000000000000000000000",
    ),
    (
        "gemm_acc",
        "GEMM",
        &[
            ("gm", 4),
            ("gn", 4),
            ("nk", 1),
            ("acc", 1),
            ("aoff", 8),
            ("boff", 12),
            ("bbank", 1),
        ],
        "2000000000000104040128000000000101820000000000000000000000000000",
    ),
    (
        "gemm_emit",
        "GEMM",
        &[("gm", 8), ("gn", 8), ("nk", 3), ("emit", 1), ("fuse", 1)],
        "2000000000000008080328000000000000180000000000000000000000000000",
    ),
    (
        "drain",
        "DRAIN",
        &[("addr", 0xDEAD_0000), ("n", 512)],
        "337AB40000080000000128000000000000000000000000000000000000000000",
    ),
    (
        "drain_node",
        "DRAIN",
        &[
            ("addr", 0),
            ("n", 16),
            ("dnode", 1),
            ("dst_x", 2),
            ("dst_y", 3),
            ("dbuf", 2),
            ("dflags", 1),
            ("dack_x", 1),
            ("dack_y", 0),
        ],
        "3000000000004000000128000000000000009181008080000000000000000000",
    ),
];

/// Each cluster case's word, or the refusal, as hex.
pub fn cluster_facts() -> Vec<String> {
    let i = isa();
    CLUSTER
        .iter()
        .map(|(_, fmt, fields, _)| {
            let f = match *fmt {
                "FILL" => &i.cluster.fill,
                "GEMM" => &i.cluster.gemm,
                _ => &i.cluster.drain,
            };
            f.encode(fields)
                .map_or_else(|r| r.to_string(), |x| x.to_hex())
        })
        .collect()
}

#[test]
fn cluster_isa_is_the_silicon_payload() {
    for ((case, .., want), got) in CLUSTER.iter().zip(cluster_facts()) {
        assert_eq!(w(&got), w(want), "{case}");
    }
}

/// The vector CU cases (IMEM, DESC, RUN) and their silicon words.
fn cu_cases() -> Vec<(VecCu, &'static str)> {
    vec![
        (
            VecCu::Imem {
                addr: 0,
                word: 0xDEAD_BEEF,
            },
            "10000000000000000000000000000000000000000000000000000000DEADBEEF",
        ),
        (
            VecCu::Imem {
                addr: 511,
                word: 0xFFFF_FFFF,
            },
            "1FF80000000000000000000000000000000000000000000000000000FFFFFFFF",
        ),
        (
            VecCu::Desc {
                ad: 3,
                fld: 5,
                value: 0x1_2345_6789,
            },
            "2752345678900000000000000000000000000000000000000000000000000000",
        ),
        (
            VecCu::Desc {
                ad: 7,
                fld: 7,
                value: (1 << 34) - 1,
            },
            "2FFFFFFFFFF00000000000000000000000000000000000000000000000000000",
        ),
        (
            VecCu::Run { pc: 0 },
            "3000000000000000000000000000000000000000000000000000000000000000",
        ),
        (
            VecCu::Run { pc: 300 },
            "3960000000000000000000000000000000000000000000000000000000000000",
        ),
    ]
}

pub fn cu_facts() -> Vec<String> {
    let v = isa().vector;
    cu_cases()
        .iter()
        .map(|(c, _)| c.encode(&v).map_or_else(|r| r.to_string(), |x| x.to_hex()))
        .collect()
}

#[test]
fn vector_isa_is_the_silicon_payload() {
    for ((c, want), got) in cu_cases().iter().zip(cu_facts()) {
        assert_eq!(w(&got), w(want), "{c:?}");
    }
}

fn math(op: MathOp, r: [u32; 4], s: [Src; 3], om: bool) -> VWord {
    VWord::Math {
        op,
        vd: r[0],
        va: r[1],
        vb: r[2],
        vc: r[3],
        sa: s[0],
        sb: s[1],
        sc: s[2],
        om,
    }
}

const V: Src = Src::V;

fn ch(base: u32, stride: u32) -> Chunk {
    Chunk(base, stride)
}

/// `test_isa.py`'s `CORE_SILICON`: each word, the format a decoder must
/// find for it, and the silicon word.
fn core_cases() -> Vec<(VWord, &'static str, u64)> {
    use MathOp::*;
    let unpk = |vd, off, n, mode, cb, rel, q| VWord::Unpk {
        vd,
        off,
        n,
        mode,
        cb,
        rel,
        q,
    };
    let pack = |vs, off, n, mode, cb, rel, q| VWord::Pack {
        vs,
        off,
        n,
        mode,
        cb,
        rel,
        q,
    };
    vec![
        (
            math(Vmul, [16, 0, 0, 0], [V, Src::S, V], false),
            "VMUL",
            0x2C00_0008,
        ),
        (
            math(Vadd, [16, 16, 0, 1], [V, V, Src::K], false),
            "VADD",
            0x1C20_0086,
        ),
        (
            math(Vfma, [31, 30, 29, 28], [Src::K, Src::S, Src::K], true),
            "VFMA",
            0x37FD_DE6F,
        ),
        (
            math(Vexp2d, [5, 0, 6, 0], [Src::K, V, V], false),
            "VEXP2D",
            0x9140_6060,
        ),
        (
            math(Vrsqrt, [1, 2, 0, 0], [V, V, V], false),
            "VRSQRT",
            0x8844_0000,
        ),
        (
            VWord::Chk {
                a: ch(7, 1),
                b: ch(6, 0),
                c: ch(5, 1),
                d: ch(4, 0),
            },
            "CHK",
            0x9800_8BCF,
        ),
        (
            VWord::Chkvl {
                vl: 128,
                a: ch(0, 1),
                b: ch(0, 1),
                c: ch(0, 1),
                d: ch(0, 1),
            },
            "CHKVL",
            0x9D7F_1111,
        ),
        (
            VWord::Chkvl {
                vl: 1,
                a: ch(0, 1),
                b: ch(2, 0),
                c: ch(4, 1),
                d: ch(7, 0),
            },
            "CHKVL",
            0x9D00_E941,
        ),
        (
            VWord::Chkvl {
                vl: 64,
                a: ch(0, 1),
                b: ch(4, 1),
                c: ch(0, 1),
                d: ch(4, 1),
            },
            "CHKVL",
            0x9D3F_9191,
        ),
        (
            VWord::Xb {
                xm: 0,
                xk: 0,
                sh: 0,
                xc: false,
                pm: 0,
                pr: 0,
            },
            "XB",
            0x9880_0000,
        ),
        (
            VWord::Xb {
                xm: 4,
                xk: 4,
                sh: 0,
                xc: true,
                pm: 0,
                pr: 0,
            },
            "XB",
            0x9880_0424,
        ),
        (
            VWord::Xb {
                xm: 3,
                xk: 15,
                sh: 7,
                xc: true,
                pm: 3,
                pr: 3,
            },
            "XB",
            0x9880_7FFB,
        ),
        (
            VWord::Ptr {
                reg: PtrReg::Uptr,
                value: 8191,
            },
            "UPTR",
            0x9900_1FFF,
        ),
        (
            VWord::Ptr {
                reg: PtrReg::Pinc,
                value: 77,
            },
            "PINC",
            0x9B00_004D,
        ),
        (
            VWord::Stride {
                reg: StrideReg::Ustr,
                s0: 1,
                s1: 4,
            },
            "USTR",
            0x9A00_0801,
        ),
        (
            VWord::Stride {
                reg: StrideReg::Pstr,
                s0: 511,
                s1: 256,
            },
            "PSTR",
            0x9B82_01FF,
        ),
        (
            VWord::Stride {
                reg: StrideReg::Ustr,
                s0: -1,
                s1: -4,
            },
            "USTR",
            0x9A03_F9FF,
        ),
        (VWord::Ainc { ad: 6, inc: -6144 }, "AINC", 0x9CE3_E800),
        (VWord::Ainc { ad: 0, inc: 4096 }, "AINC", 0x9C80_1000),
        (VWord::Ainc { ad: 7, inc: 131071 }, "AINC", 0x9CF1_FFFF),
        (VWord::Getstk { sreg: 3 }, "GETSTK", 0x9C00_0003),
        (VWord::Getstk { sreg: 15 }, "GETSTK", 0x9C00_000F),
        (
            unpk(4, 96, 2, WalkMode::F16, 2, true, 0),
            "VUNPK",
            0xA102_A060,
        ),
        (
            unpk(5, 8, 8, WalkMode::Gt4, 0, false, 1),
            "VUNPK",
            0xA15E_1008,
        ),
        (
            unpk(31, 4095, 1, WalkMode::F32, 7, true, 1),
            "VUNPK",
            0xA7E1_FFFF,
        ),
        (
            pack(6, 256, 8, WalkMode::F16, 0, false, 0),
            "VPACK",
            0xA98E_0100,
        ),
        (
            pack(9, 17, 4, WalkMode::Gt4, 4, false, 1),
            "VPACK",
            0xAA57_1011,
        ),
        (
            VWord::PackMx7 {
                vs: 6,
                off: 256,
                b_layout: true,
                rel: false,
                q: 0,
            },
            "VPACK",
            0xA9BE_4100,
        ),
        (
            VWord::PackMx7 {
                vs: 2,
                off: 0,
                b_layout: false,
                rel: true,
                q: 1,
            },
            "VPACK",
            0xA8BE_3000,
        ),
        (
            VWord::Sync {
                waiter: 6,
                on: 0,
                slack: 0,
                mark: Some(5),
                q: 0,
            },
            "VSYNC",
            0xB600_6800,
        ),
        (
            VWord::Sync {
                waiter: 1,
                on: 3,
                slack: 63,
                mark: None,
                q: 1,
            },
            "VSYNC",
            0xB17F_8400,
        ),
        (
            VWord::Sync {
                waiter: 3,
                on: 6,
                slack: 0,
                mark: None,
                q: 0,
            },
            "VSYNC",
            0xB3C0_0000,
        ),
        (
            VWord::Mark {
                mark: 5,
                on: 6,
                slack: 1,
            },
            "VMARK",
            0xB5C0_A800,
        ),
        (
            VWord::Mark {
                mark: 7,
                on: 0,
                slack: 63,
            },
            "VMARK",
            0xB51F_B800,
        ),
        (VWord::Skipz { sreg: 3, n: 2 }, "VSKIPZ", 0xB8C0_0002),
        (VWord::Skipz { sreg: 15, n: 255 }, "VSKIPZ", 0xBBC0_00FF),
        (VWord::Setvl { sreg: 9 }, "VSETVL", 0xC012_0000),
        (
            VWord::Seti {
                sreg: 0,
                to_k: false,
            },
            "VSETI",
            0xD000_0000,
        ),
        (
            VWord::Seti {
                sreg: 15,
                to_k: true,
            },
            "VSETI",
            0xD3C0_0001,
        ),
        (VWord::Loop { sreg: 5, body: 4 }, "VLOOP", 0xD80A_0200),
        (
            VWord::Loop {
                sreg: 15,
                body: 1023,
            },
            "VLOOP",
            0xD81F_FF80,
        ),
        (VWord::Bar { q: 0 }, "VBAR", 0xE000_0000),
        (VWord::Bar { q: 1 }, "VBAR", 0xE000_0400),
        (VWord::Halt, "VHALT", 0xF800_0000),
        (
            VWord::Fill {
                ad: 0,
                l1off: 0,
                rel: true,
            },
            "VFILL",
            0xEC00_0000,
        ),
        (
            VWord::Fill {
                ad: 7,
                l1off: 16383,
                rel: false,
            },
            "VFILL",
            0xE8E0_3FFF,
        ),
        (
            VWord::Drain {
                ad: 2,
                l1off: 256,
                rel: true,
                node: Some(Coord(1, 2)),
                buf_id: 1,
                signal: true,
            },
            "VDRAIN",
            0xF742_4300,
        ),
        (
            VWord::Drain {
                ad: 7,
                l1off: 511,
                rel: false,
                node: None,
                buf_id: 0,
                signal: false,
            },
            "VDRAIN",
            0xF0E0_01FF,
        ),
        (
            VWord::Drain {
                ad: 3,
                l1off: 0,
                rel: false,
                node: Some(Coord(15, 15)),
                buf_id: 15,
                signal: false,
            },
            "VDRAIN",
            0xF17F_FE00,
        ),
    ]
}

/// Each core case's word (or 0 for a refusal).
pub fn core_facts() -> Vec<u64> {
    let v = isa().vector;
    core_cases()
        .iter()
        .map(|(c, ..)| c.encode(&v).unwrap_or(0))
        .collect()
}

#[test]
fn core_word_is_the_silicon_word() {
    for ((c, _, want), got) in core_cases().iter().zip(core_facts()) {
        assert_eq!(got, *want, "{c:?}: {got:#010x}, silicon {want:#010x}");
    }
}

/// For each silicon core word: the format the decoder finds, and whether
/// re-encoding what it decodes gives the word back.
pub fn decoder_facts() -> Vec<(String, bool)> {
    let v = isa().vector;
    core_cases()
        .iter()
        .map(
            |(_, _, word)| match v.core.set.find(W256::from_u64(*word)) {
                Some(f) => {
                    let back = f
                        .decode(W256::from_u64(*word))
                        .and_then(|d| f.encode(&d))
                        .map(|x| x.low_u64());
                    (f.name.to_string(), back == Ok(*word))
                }
                None => ("<none>".into(), false),
            },
        )
        .collect()
}

#[test]
fn a_decoder_finds_the_encoders_format() {
    for ((c, name, word), got) in core_cases().iter().zip(decoder_facts()) {
        assert_eq!(got, (name.to_string(), true), "{c:?} {word:#010x}");
    }
}

/// Which of `test_isa.py`'s too-wide words are refused: a 33rd register on a
/// 32-register core, descriptor 8, VL 129.
pub fn too_wide_facts() -> [bool; 3] {
    let i = isa();
    let mut cfg = i.vector.cfg.clone();
    cfg.regs = 32;
    let v32 = VecIsa::of(cfg);
    [
        math(MathOp::Vadd, [32, 0, 0, 0], [V; 3], false)
            .encode(&v32)
            .is_err(),
        VWord::Fill {
            ad: 8,
            l1off: 0,
            rel: false,
        }
        .encode(&i.vector)
        .is_err(),
        VWord::Chkvl {
            vl: 129,
            a: Chunk::default(),
            b: Chunk::default(),
            c: Chunk::default(),
            d: Chunk::default(),
        }
        .encode(&i.vector)
        .is_err(),
    ]
}

#[test]
fn a_word_too_wide_for_its_field_is_refused() {
    assert_eq!(too_wide_facts(), [true; 3]);
}

/// A two-word program's flits and the last one's disassembly.
pub fn program_facts() -> (usize, String) {
    let v = isa().vector;
    let flits = v.program(&[0x1111, 0x2222], 0).unwrap();
    (flits.len(), v.set.disasm(*flits.last().unwrap()))
}

#[test]
fn vector_program_ends_in_run() {
    let (n, last) = program_facts();
    assert_eq!(n, 3);
    assert!(last.starts_with("RUN"), "{last}");
}

/// The silicon GEMM word's disassembly.
pub fn gemm_disasm() -> String {
    let gemm = CLUSTER.iter().find(|c| c.0 == "gemm").unwrap().3;
    isa().cluster.set.disasm(w(gemm))
}

#[test]
fn disassembly_names_the_opcode() {
    assert!(gemm_disasm().starts_with("GEMM"), "{}", gemm_disasm());
}

/// Each test above fails under a mutant of the point it witnesses.
#[cfg(feature = "mutants")]
mod mutants {
    use super::*;
    use ktpu_isa::mutant::with;
    use std::panic::{catch_unwind, AssertUnwindSafe};

    fn breaks<T: PartialEq + std::fmt::Debug>(name: &'static str, facts: fn() -> T) {
        let want = facts();
        if let Ok(got) = catch_unwind(AssertUnwindSafe(|| with(name, facts))) {
            assert_ne!(got, want, "mutant {name} does not break the facts");
        }
    }

    #[test]
    fn cluster_payloads_break_under_fields() {
        breaks("isa.cluster/fields", cluster_facts);
    }

    #[test]
    fn cu_payloads_break_under_imem_desc_run() {
        for m in [
            "isa.vector/cu_imem",
            "isa.vector/cu_desc",
            "isa.vector/cu_run",
        ] {
            breaks(m, cu_facts);
        }
    }

    #[test]
    fn core_words_break_under_each_format_group() {
        for m in [
            "isa.vector/math_fields",
            "isa.vector/cfg_fields",
            "isa.vector/walk_fields",
            "isa.vector/sync_fields",
            "isa.vector/ctl_fields",
            "isa.vector/dma_fields",
        ] {
            breaks(m, core_facts);
        }
    }

    #[test]
    fn decoder_breaks_under_decoder() {
        breaks("isa.vector/decoder", decoder_facts);
    }

    #[test]
    fn too_wide_breaks_under_fit_wraps() {
        breaks("isa.layout/fit_wraps", too_wide_facts);
    }

    #[test]
    fn program_breaks_under_cu_run() {
        breaks("isa.vector/cu_run", program_facts);
    }

    #[test]
    fn disasm_breaks_under_opcodes() {
        breaks("isa.cluster/opcodes", gemm_disasm);
    }
}
