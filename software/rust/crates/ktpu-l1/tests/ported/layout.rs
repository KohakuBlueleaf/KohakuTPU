//! `compiler/tests/test_layout.py`: MXFP7 packing slot by slot against an
//! independent reference, the drained-tile reading, and batches as slabs.

use super::*;
use ktpu_l1::layout::buffer::Buffer;
use ktpu_l1::layout::tensor::{pack, quantise, Mx7, Quantised, SBIAS};

pub const CHECKS: &[(&str, Check)] = &[
    (
        "test_layout::test_pack_places_every_slot_and_scale",
        pack_places_every_slot_and_scale,
    ),
    (
        "test_layout::test_pack_refuses_a_partial_tile",
        pack_refuses_a_partial_tile,
    ),
    (
        "test_layout::test_a_drained_tile_reads_sub_tile_by_sub_tile",
        a_drained_tile_reads_sub_tile_by_sub_tile,
    ),
    (
        "test_layout::test_a_tiled_buffer_reads_back_what_it_wrote",
        a_tiled_buffer_reads_back_what_it_wrote,
    ),
    (
        "test_layout::test_a_batch_buffer_is_its_slabs_one_after_another",
        a_batch_buffer_is_its_slabs_one_after_another,
    ),
];

/// `n` normal fp16 bit patterns of either sign, exponents 12..=17 (about
/// 0.03 to 8), from a fixed linear congruential stream.
fn fp16s(n: usize, seed: u64) -> Vec<u16> {
    let mut s = seed;
    (0..n)
        .map(|_| {
            s = s
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let r = s >> 33;
            let sign = (r & 1) as u16;
            let exp = 12 + (r >> 1) % 6;
            let frac = (r >> 4) as u16 & 0x3FF;
            sign << 15 | (exp as u16) << 10 | frac
        })
        .collect()
}

/// The v9 MXFP7 entry by slot, written out on its own: lane `l`'s int7 for
/// K `kk` of word `w` at bit `249 - 7*oi`, the lanes' scales at `24 - 8*l`;
/// slot `oi = l*8 + kk` (A) or `kk*4 + l` (B); entries tile, chunk, group,
/// block.
fn by_slot(x: &[u16], rows: usize, k: usize, gt: usize, bc: usize, b: bool) -> Vec<u8> {
    let nk = k / 32;
    let q: Vec<Quantised> = (0..rows)
        .map(|r| quantise(&x[r * k..(r + 1) * k], 32).unwrap())
        .collect();
    let mut out = Vec::new();
    for t in 0..rows / 4 / gt {
        for c in 0..nk / bc {
            for g in 0..gt {
                for blk in 0..bc {
                    let kb = c * bc + blk;
                    for w in 0..4 {
                        let mut bits = [0u8; 32];
                        let mut put = |at: usize, width: usize, v: u64| {
                            for i in 0..width {
                                if v >> i & 1 == 1 {
                                    bits[(at + i) / 8] |= 1 << ((at + i) % 8);
                                }
                            }
                        };
                        for l in 0..4 {
                            let row = (t * gt + g) * 4 + l;
                            let (es, m8) = q[row].1[kb];
                            let field = ((es + SBIAS) & 0x1F) << 3 | ((m8 - 8) & 7);
                            put(24 - 8 * l, 8, field as u64);
                        }
                        for oi in 0..32 {
                            let (l, kk) = if b {
                                (oi % 4, oi / 4)
                            } else {
                                (oi / 8, oi % 8)
                            };
                            let row = (t * gt + g) * 4 + l;
                            let v = q[row].0[kb * 32 + w * 8 + kk] & 0x7F;
                            put(249 - 7 * oi, 7, v as u64);
                        }
                        out.extend_from_slice(&bits);
                    }
                }
            }
        }
    }
    out
}

fn mx7() -> Result<Mx7, String> {
    Mx7::of(&machine()).map_err(|e| e.to_string())
}

/// `pack` equals the slot-by-slot reference for A and B packings, and an
/// all-zero block gets the zero scale.
fn pack_places_every_slot_and_scale() -> Result<(), String> {
    let f = mx7()?;
    for (i, &(rows, k, gt, bc)) in [(8, 64, 1, 1), (16, 128, 2, 2), (32, 64, 4, 1)]
        .iter()
        .enumerate()
    {
        let mut x = fp16s(rows * k, i as u64 + 1);
        x[..32].fill(0);
        for b in [false, true] {
            let got = pack(&x, rows, k, gt, bc, b, &f).map_err(|e| e.to_string())?;
            eq(
                (rows, k, gt, bc, b, got),
                (rows, k, gt, bc, b, by_slot(&x, rows, k, gt, bc, b)),
            )?;
        }
        let (_, scales) = quantise(&x[..32], 32).map_err(|e| e.to_string())?;
        eq((scales[0].0 + SBIAS) & 0x1F, SBIAS)?;
    }
    Ok(())
}

/// A packing of a partial tile is refused.
fn pack_refuses_a_partial_tile() -> Result<(), String> {
    eq(
        clause(pack(&[0u16; 12 * 64], 12, 64, 2, 1, false, &mx7()?))?,
        "L1.BODY.002".into(),
    )
}

fn buffer(ty: &str) -> Result<Buffer, String> {
    let text = format!("target ktpu.v9\nfn f.l1(p: {ty})\n    halt\n");
    let m = read(&text, "<t>").map_err(|e| e.to_string())?;
    let body = m.body("f", "l1").map_err(|e| e.to_string())?;
    Buffer::of(&body.params[0].1).map_err(|e| e.to_string())
}

fn le(bits: &[u16]) -> Vec<u8> {
    bits.iter().flat_map(|b| b.to_le_bytes()).collect()
}

/// The clusters' fp16: exponent 0 is a signed zero, 31 an ordinary one.
fn cluster(b: u16) -> f64 {
    let e = ((b >> 10) & 0x1F) as i32;
    let v = match e {
        0 => 0.0,
        _ => (1.0 + (b & 0x3FF) as f64 / 1024.0) * 2f64.powi(e - 15),
    };
    if b & 0x8000 != 0 {
        -v
    } else {
        v
    }
}

/// A drained `[16, 32]` of 2x4 tiles: 32-byte word w is sub-tile
/// `(w / 8, w % 8)`, element `e` its `(e / 4, e % 4)`; values the clusters'
/// fp16.
fn a_drained_tile_reads_sub_tile_by_sub_tile() -> Result<(), String> {
    let b = buffer("f16[16, 32] @dram(tile=2x4)")?;
    let mut bits = fp16s(512, 7);
    bits[..8].copy_from_slice(&[
        0x0000, 0x8000, 0x0001, 0x83FF, 0x7C00, 0xFC00, 0x7FFF, 0x3C00,
    ]);
    let got = b.unpack(&le(&bits)).map_err(|e| e.to_string())?;
    let mut want = vec![0.0f64; 512];
    for (w, sub) in bits.chunks(16).enumerate() {
        let (tile, s) = (w / 8, w % 8);
        let (ti, tj, si, sj) = (tile / 2, tile % 2, s / 4, s % 4);
        for (e, &h) in sub.iter().enumerate() {
            let (i, j) = (e / 4, e % 4);
            want[(8 * ti + 4 * si + i) * 32 + 16 * tj + 4 * sj + j] = cluster(h);
        }
    }
    let as_bits = |v: &[f64]| v.iter().map(|x| x.to_bits()).collect::<Vec<_>>();
    eq(as_bits(&got), as_bits(&want))
}

/// A `tile=` buffer reads back the fp16 values it was written.
fn a_tiled_buffer_reads_back_what_it_wrote() -> Result<(), String> {
    let b = buffer("f16[16, 32] @dram(tile=2x2)")?;
    let x = fp16s(512, 9);
    let raw = b.pack(&x, &machine()).map_err(|e| e.to_string())?;
    let got = b.unpack(&raw).map_err(|e| e.to_string())?;
    eq(got, x.iter().map(|&h| cluster(h)).collect())
}

/// A batch buffer is its slabs one after another: the slab's host shape,
/// the slabs' packings concatenated, as many bytes as `bytes`, and an f16
/// batch reads back.
fn a_batch_buffer_is_its_slabs_one_after_another() -> Result<(), String> {
    let m = machine();
    let cases = [
        (
            "mx7a[3, 32, 64] @dram(gm=8, nk=1)",
            "mx7a[32, 64] @dram(gm=8, nk=1)",
        ),
        (
            "mx7b[2, 64, 128] @dram(gn=16, nk=2, t=1)",
            "mx7b[64, 128] @dram(gn=16, nk=2, t=1)",
        ),
        (
            "f16[2, 16, 32] @dram(tile=2x2)",
            "f16[16, 32] @dram(tile=2x2)",
        ),
        ("f16[2, 16, 32] @dram", "f16[16, 32] @dram"),
    ];
    for (bt, st) in cases {
        let (b, s) = (buffer(bt)?, buffer(st)?);
        eq((bt, b.host_shape()[1..].to_vec()), (bt, s.host_shape()))?;
        let n: i128 = b.shape.iter().product();
        let x = fp16s(n as usize, n as u64);
        let packed = b.pack(&x, &m).map_err(|e| e.to_string())?;
        let per = x.len() / b.shape[0] as usize;
        let mut slabs = Vec::new();
        for c in x.chunks(per) {
            slabs.extend(s.pack(c, &m).map_err(|e| e.to_string())?);
        }
        eq(
            (bt, packed.len() as u64),
            (bt, b.bytes(&m).map_err(|e| e.to_string())?),
        )?;
        eq((bt, &packed), (bt, &slabs))?;
        if b.dtype == "f16" {
            let back = b.unpack(&packed).map_err(|e| e.to_string())?;
            eq((bt, back), (bt, x.iter().map(|&h| cluster(h)).collect()))?;
        }
    }
    Ok(())
}
