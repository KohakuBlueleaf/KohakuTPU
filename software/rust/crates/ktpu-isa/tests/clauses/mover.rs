//! `docs/spec/stack/isa/mover.md`.

use super::{isa, machine, refused};
use ktpu_isa::unit::mover::*;
use ktpu_isa::{Level, Move, MoverOp, Walk, W256};

fn mv(mode: MoveMode, src: Option<Walk>, dst: Option<Walk>) -> Move {
    Move {
        mode,
        src,
        dst,
        ewidth: W32,
        flags: FLAG_WCOAL,
        xform: (0, 0),
        imm: None,
        seed: None,
        index: None,
        row: None,
        fp: false,
    }
}

fn w(base: i64, dims: &[(i64, i64)]) -> Walk {
    Walk::new(base, dims)
}

fn regs(writes: &[RegWrite]) -> Vec<u32> {
    writes.iter().map(|(r, _)| *r).collect()
}

#[test]
fn isa_mv_002_forty_bit_addresses() {
    let i = isa();
    let a = (1i64 << 40) - 32;
    let out = i.mover.walker(1, &w(a, &[(1, 32)]), (0, 0)).unwrap();
    assert_eq!(out[0].1 >> BASE_LSB & ((1 << 40) - 1), a as u64);
    refused(
        i.mover.walker(1, &w(1 << 40, &[(1, 32)]), (0, 0)),
        "ISA.MV.014",
    );
}

#[test]
fn isa_mv_003_levels_count_and_stride() {
    let i = isa();
    let levels = machine().mover.levels as usize;
    let six: Vec<(i64, i64)> = vec![(2, 32); levels];
    assert!(i.mover.walker(0, &w(0, &six), (0, 0)).is_ok());
    let seven: Vec<(i64, i64)> = vec![(2, 32); levels + 1];
    refused(i.mover.walker(0, &w(0, &seven), (0, 0)), "ISA.MV.003");
    refused(i.mover.walker(0, &w(0, &[]), (0, 0)), "ISA.MV.003");
    refused(
        i.mover.walker(0, &w(0, &[(1 << 16, 32)]), (0, 0)),
        "ISA.MV.015",
    );
    let neg = i
        .mover
        .walker(0, &w(0, &[(3, -(1 << 31))]), (0, 0))
        .unwrap();
    assert_eq!(neg[1].1 >> 20, 1 << 31);
}

#[test]
fn isa_mv_007_moves_are_cut_at_the_move_bound() {
    let i = isa();
    let words = MOVE_WORDS as i64 + 2;
    let out = i
        .mover
        .writes(&MoverOp::Copy {
            src: 0,
            dst: 0x100000,
            nbytes: words * 32,
        })
        .unwrap();
    let gos: Vec<&RegWrite> = out
        .iter()
        .filter(|(r, v)| *r == R_CTRL && v & GO != 0)
        .collect();
    assert_eq!(gos.len(), 2);
    let counts: Vec<u64> = out
        .iter()
        .filter(|(r, _)| *r == R_DIM)
        .map(|(_, v)| v >> 4 & 0xFFFF)
        .collect();
    assert_eq!(counts, [MOVE_WORDS, MOVE_WORDS, 2, 2]);
}

#[test]
fn isa_mv_010_register_offsets() {
    assert_eq!(
        (R_CTRL, R_HDR, R_DIM, R_AXIS, R_WINDOW, R_IMM),
        (0x00, 0x10, 0x18, 0x20, 0x28, 0x40)
    );
    assert_eq!((R_INDEX, R_SEED, R_ROW), (0x30, 0x38, 0x50));
}

#[test]
fn isa_mv_013_ctrl_with_go_last() {
    let i = isa();
    let mut m = mv(MoveMode::Fill, None, Some(w(0, &[(4, 32)])));
    m.imm = Some(0);
    m.ewidth = W16;
    m.fp = true;
    let out = i.mover.move_writes(&m).unwrap();
    let (reg, ctl) = *out.last().unwrap();
    assert_eq!(reg, R_CTRL);
    assert_eq!(
        (
            ctl & 7,
            ctl >> 3 & 3,
            ctl >> 8 & 0xFF,
            ctl >> 16 & 1,
            ctl >> 17 & 1
        ),
        (4, 1, 8, 1, 1)
    );
}

#[test]
fn isa_mv_014_header() {
    let i = isa();
    let out = i
        .mover
        .walker(0, &w(0x1000, &[(2, 32), (3, 64)]), (2, 1))
        .unwrap();
    let h = out[0].1;
    assert_eq!(
        (
            h & 1,
            h >> 4 & ((1 << 40) - 1),
            h >> 44 & 7,
            h >> 47 & 15,
            h >> 55 & 15
        ),
        (0, 0x1000, 2, 2, 1)
    );
    refused(i.mover.walker(0, &w(0, &[(1, 32)]), (16, 0)), "ISA.MV.014");
}

#[test]
fn isa_mv_015_dim_then_axis_for_every_level() {
    let i = isa();
    let walk = Walk {
        base: 0,
        dims: vec![
            Level::new(8, 64),
            Level {
                count: 2,
                stride: -32,
                axis: 2,
                astep: -1,
            },
        ],
        windows: [(0, 0); 2],
    };
    let out = i.mover.walker(1, &walk, (0, 0)).unwrap();
    assert_eq!(
        regs(&out),
        [R_HDR, R_DIM, R_AXIS, R_DIM, R_AXIS, R_WINDOW, R_WINDOW]
    );
    let d = out[3].1;
    assert_eq!(
        (d & 1, d >> 1 & 7, d >> 4 & 0xFFFF, (d >> 20) as u32 as i32),
        (1, 1, 2, -32)
    );
    let a = out[4].1;
    assert_eq!((a & 3, (a >> 2) as u16 as i16), (2, -1));
    let bad = Walk {
        dims: vec![Level {
            count: 1,
            stride: 32,
            axis: 3,
            astep: 0,
        }],
        ..walk
    };
    refused(i.mover.walker(1, &bad, (0, 0)), "ISA.MV.015");
}

#[test]
fn isa_mv_016_windows() {
    let i = isa();
    let walk = Walk {
        base: 0,
        dims: vec![Level::new(1, 32)],
        windows: [(-1, 6), (0, 2)],
    };
    let out = i.mover.walker(0, &walk, (0, 0)).unwrap();
    let (w1, w2) = (out[3].1, out[4].1);
    assert_eq!(
        (w1 >> 1 & 1, (w1 >> 2) as u16 as i16, w1 >> 18 & 0xFFFF),
        (0, -1, 6)
    );
    assert_eq!((w2 >> 1 & 1, w2 >> 18 & 0xFFFF), (1, 2));
    let bad = Walk {
        windows: [(0, 1 << 16), (0, 0)],
        ..walk
    };
    refused(i.mover.walker(0, &bad, (0, 0)), "ISA.MV.016");
}

#[test]
fn isa_mv_017_index_row_seed_imm() {
    let i = isa();
    let mut m = mv(
        MoveMode::Gather,
        Some(w(0x1000, &[(1, 32)])),
        Some(w(0, &[(4, 32)])),
    );
    m.index = Some((0x2000, 16));
    m.row = Some((256, 2));
    m.seed = Some(u64::MAX);
    m.imm = Some(0xFFFF_FFFF);
    let out = i.mover.move_writes(&m).unwrap();
    let find = |r| out.iter().find(|(x, _)| *x == r).unwrap().1;
    assert_eq!(find(R_INDEX), 0x2000 | 16 << 40);
    assert_eq!(find(R_ROW), 256 | 2 << 32);
    assert_eq!(find(R_SEED), u64::MAX);
    assert_eq!(find(R_IMM), 0xFFFF_FFFF);
    m.imm = Some(1 << 32);
    refused(i.mover.move_writes(&m), "ISA.MV.017");
}

#[test]
fn isa_mv_018_both_windows_and_the_immediate_are_written() {
    let i = isa();
    let out = i
        .mover
        .writes(&MoverOp::Fill {
            dst: w(0, &[(4, 32)]),
            value: 0,
            ewidth: W32,
        })
        .unwrap();
    assert_eq!(regs(&out).iter().filter(|r| **r == R_WINDOW).count(), 2);
    assert!(regs(&out).contains(&R_IMM));
}

#[test]
fn isa_mv_023_whole_words() {
    let i = isa();
    refused(i.mover.walker(0, &w(16, &[(1, 32)]), (0, 0)), "ISA.MV.023");
    refused(i.mover.walker(0, &w(0, &[(1, 48)]), (0, 0)), "ISA.MV.023");
}

#[test]
fn isa_mv_030_copy_counts() {
    let i = isa();
    let out = i
        .mover
        .writes(&MoverOp::Copy {
            src: 0,
            dst: 0x8000,
            nbytes: 4096,
        })
        .unwrap();
    let counts: Vec<u64> = out
        .iter()
        .filter(|(r, _)| *r == R_DIM)
        .map(|(_, v)| v >> 4 & 0xFFFF)
        .collect();
    assert_eq!(counts, [128, 128]);
    assert_eq!(out.last().unwrap().1 & 7, machine().mover.modes.copy as u64);
}

#[test]
fn isa_mv_041_element_width() {
    let i = isa();
    let mut m = mv(MoveMode::Fill, None, Some(w(0, &[(1, 32)])));
    m.ewidth = 3;
    refused(i.mover.move_writes(&m), "ISA.MV.041");
}

#[test]
fn isa_mv_042_flags() {
    let i = isa();
    assert_eq!(i.mover.flag_byte(true, 0).unwrap(), 0x08);
    assert_eq!(i.mover.flag_byte(true, 3).unwrap(), 0x08 | 3 << 5);
    refused(i.mover.flag_byte(false, 8), "ISA.MV.042");
}

#[test]
fn isa_mv_053_054_index_lists() {
    let i = isa();
    let max = machine().mover.max_index as i64;
    let gather = |base: i64, count: i64| MoverOp::Gather {
        src_base: 0,
        index: (base, count),
        dst: w(0, &[(1, 32)]),
        row: (32, 1),
        fp: false,
    };
    assert!(i.mover.writes(&gather(0x2000, max)).is_ok());
    refused(i.mover.writes(&gather(0x2000, max + 1)), "ISA.MV.053");
    refused(i.mover.writes(&gather(0x2000, 0)), "ISA.MV.053");
    refused(i.mover.writes(&gather(0x2010, 1)), "ISA.MV.054");
}

#[test]
fn isa_mv_061_062_063_transform_moves() {
    let i = isa();
    let out = i
        .mover
        .writes(&MoverOp::Quantise {
            src: 0,
            dst: 0x8000,
            entries: 24,
            b: true,
        })
        .unwrap();
    let hdr = out[0].1;
    assert_eq!((hdr >> 47 & 15, hdr >> 55 & 15), (XF_QUANT as u64, 1));
    let counts: Vec<u64> = out
        .iter()
        .filter(|(r, _)| *r == R_DIM)
        .map(|(_, v)| v >> 4 & 0xFFFF)
        .collect();
    assert_eq!(counts, [24 * 8, 24]);
    let ctl = out.last().unwrap().1;
    assert_eq!(
        (ctl & 7, ctl >> 3 & 3),
        (machine().mover.modes.transform as u64, W16 as u64)
    );
    let gt4 = i
        .mover
        .writes(&MoverOp::Transpose4 {
            src: 0,
            dst: 0x8000,
            groups: 100,
        })
        .unwrap();
    assert_eq!(gt4[0].1 >> 47 & 15, XF_GT4 as u64);
}

#[test]
fn isa_mv_090_payload_addresses() {
    let i = isa();
    let hdr = i
        .mover
        .walker(1, &w(0x12_3456_7800, &[(1, 32)]), (0, 0))
        .unwrap()[0];
    let p = W256([R_INDEX as u64, 0x2000 | 4 << 40, hdr.0 as u64, hdr.1]);
    let got: Vec<(u32, u64)> = i
        .mover
        .addresses(p)
        .iter()
        .map(|a| (a.segments[0].bit, a.value))
        .collect();
    assert_eq!(got, [(64, 0x2000), (128 + 64 + 4, 0x12_3456_7800)]);
}
