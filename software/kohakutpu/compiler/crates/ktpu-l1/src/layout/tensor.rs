//! Operand layout: MXFP7 packing for the clusters and the drained sub-tile
//! form. Every operand is stored row-major as FP16, `[lanes][K]`, lanes a
//! multiple of `mg.lanes` and K of `mg.kblock`; a B operand is stored
//! transposed.

pub use ktpu_isa::numerics::{f16_saturating, fp16_value, ieee_f16};
use ktpu_machine::Machine;

use crate::error::{Error, Result};

/// Bits of one int7 significand.
const QBITS: usize = 7;
/// Bits of one E5M3 block scale.
const SBITS: usize = 8;

/// One MXFP7 L1 entry's geometry (`mg.lanes`, `mg.kblock`, `mg.entry_bits`,
/// `die.word_bytes`): `lanes` x `kblock` int7 and a scale a lane, over
/// `words` memory words; each word carries `kblock / words` values of every
/// lane and all `lanes` scales.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Mx7 {
    pub lanes: usize,
    pub kblock: usize,
    pub words: usize,
    pub word_bytes: usize,
}

impl Mx7 {
    /// The machine's entry; refuses (L1.BODY.002) one whose facts disagree
    /// (`entry_bits` is not lanes x (kblock x 7 + 8)) or whose words cannot
    /// hold an even share of each lane's values and the scales.
    pub fn of(m: &Machine) -> Result<Mx7> {
        if crate::mutant::on("l1.facts/mx7-fixed") {
            return Ok(Mx7 {
                lanes: 4,
                kblock: 32,
                words: 4,
                word_bytes: 32,
            });
        }
        let (lanes, kblock) = (m.mg.lanes as usize, m.mg.kblock as usize);
        let word_bytes = m.die.word_bytes as usize;
        let entry = m.mg.entry_bits as usize;
        let words = entry.div_ceil(8 * word_bytes);
        let fits = words > 0
            && kblock.is_multiple_of(words)
            && lanes * (kblock / words * QBITS + SBITS) <= 8 * word_bytes;
        if entry != lanes * (kblock * QBITS + SBITS) || !fits {
            return Err(Error::new(
                "L1.BODY.002",
                format!(
                    "no MXFP7 entry of {lanes} lanes x {kblock} values in {entry} bits over \
                     {word_bytes}-byte words"
                ),
            ));
        }
        Ok(Mx7 {
            lanes,
            kblock,
            words,
            word_bytes,
        })
    }

    /// Bytes of one entry.
    pub fn entry_bytes(&self) -> usize {
        self.words * self.word_bytes
    }
}

/// The block scale's exponent bias and widths (E5M3).
pub const SBIAS: i64 = 20;
const SEBITS: i64 = 5;
const QMAX: i64 = 63;
/// round(4096 * 8 / m8) for the eight scale mantissas, as `mx_quant.v` holds.
const RECIP: [i64; 8] = [4096, 3641, 3277, 2979, 2731, 2520, 2341, 2185];
const RECIP_SHIFT: i64 = 12;

/// A contraction operand's zero as quantised: 2^-24, int7 0 under every
/// scale, so only an all-zero block changes, to E=0 from the quantisers' E=20.
/// `mx_acu_fp_pump.v` aligns a K pair to the larger exponent sum with no zero
/// test: an E=20 zero block truncates a live partner below it.
pub const ZERO_LIFT: u16 = 0x0001;

/// [`ZERO_LIFT`]'s value, which a vector core selects for a zero lane
/// before its MXFP7 pack.
pub fn zero_lift_value() -> f64 {
    ieee_f16(ZERO_LIFT)
}

/// `x` with every zero (either sign) as [`ZERO_LIFT`].
pub fn lift(x: &[u16]) -> Vec<u16> {
    x.iter()
        .map(|&b| if b & 0x7FFF == 0 { ZERO_LIFT } else { b })
        .collect()
}

fn sbias() -> i64 {
    match crate::mutant::on("num.mx7/scale-bias-21") {
        true => 21,
        false => SBIAS,
    }
}

/// The E5M3 field `(es, m8)` for a block whose FP16 peak has these bits: the
/// smallest scale keeping peak / scale <= 63.
fn scale_field(peak_bits: i64) -> (i64, i64) {
    let (es, m8) = scale_field_of(peak_bits);
    match crate::mutant::on("num.mx7/scale-one-up") {
        true => (es + 1, m8),
        false => (es, m8),
    }
}

fn scale_field_of(peak_bits: i64) -> (i64, i64) {
    let ep_f = (peak_bits >> 10) & 0x1F;
    let frac = peak_bits & 0x3FF;
    let mut sig = if ep_f == 0 { frac } else { 1 << 10 | frac };
    let mut ep = if ep_f == 0 { 1 } else { ep_f };
    for _ in 0..11 {
        if sig != 0 && sig < 1024 {
            sig *= 2;
            ep -= 1;
        }
    }
    if sig == 0 {
        return (0, 8);
    }
    let mut m8 = (sig + 125) / 126;
    let es = if m8 >= 16 { ep - 20 } else { ep - 21 };
    m8 = match m8 {
        17 => 9,
        16 => 8,
        m => m,
    };
    (es.clamp(-sbias(), (1 << SEBITS) - 1 - sbias()), m8)
}

/// The real number a scale field stands for: `2^es * m8 / 8`.
pub fn scale_value(es: i64, m8: i64) -> f64 {
    match crate::mutant::on("num.mx7/scale-power-of-two") {
        true => 2f64.powi(es as i32),
        false => m8 as f64 / 8.0 * 2f64.powi(es as i32),
    }
}

/// The values the clusters' operand holds after quantisation
/// (`value_fp16`): each int7 times its block's scale.
pub fn value(row: &[u16], kblock: usize) -> Result<Vec<f64>> {
    let (q, scales) = quantise(row, kblock)?;
    Ok(q.iter()
        .enumerate()
        .map(|(i, v)| {
            let (es, m8) = scales[i / kblock];
            *v as f64 * scale_value(es, m8)
        })
        .collect())
}

/// Int7 significands and each block's `(es, m8)`.
pub type Quantised = (Vec<i64>, Vec<(i64, i64)>);

/// `quantise_fp16`: one row's FP16 bits to int7 significands and each
/// `kblock`-value block's `(es, m8)`, as `mx_quant.v` does.
pub fn quantise(row: &[u16], kblock: usize) -> Result<Quantised> {
    if kblock == 0 || !row.len().is_multiple_of(kblock) {
        return Err(Error::new(
            "L1.BODY.002",
            format!("K={} is not a multiple of {kblock}", row.len()),
        ));
    }
    let mut q = Vec::with_capacity(row.len());
    let mut scales = Vec::with_capacity(row.len() / kblock);
    for block in row.chunks(kblock) {
        let peak = block.iter().map(|b| (*b & 0x7FFF) as i64).max().unwrap();
        let (es, m8) = scale_field(peak);
        let recip = match crate::mutant::on("num.mx7/e8m0-reciprocal") {
            true => 4096,
            false => RECIP[(m8 - 8) as usize],
        };
        let recip_shift = match crate::mutant::on("num.mx7/recip-shift-13") {
            true => 13,
            false => RECIP_SHIFT,
        };
        for &b in block {
            let b = b as i64;
            let sign = (b >> 15) & 1;
            let e_f = (b >> 10) & 0x1F;
            let frac = b & 0x3FF;
            let sig = if e_f == 0 { frac } else { 1 << 10 | frac };
            let e = if e_f == 0 { 1 } else { e_f };
            let prod = sig * recip;
            let sh = 25 + recip_shift + es - e;
            let safe = sh.clamp(0, 62);
            let mag = if sh > 40 {
                0
            } else {
                (prod >> safe) + ((prod >> (safe - 1).max(0)) & 1)
            };
            let mag = mag.min(QMAX);
            q.push(if sign == 1 { -mag } else { mag });
        }
        scales.push((es, m8));
    }
    Ok((q, scales))
}

/// `v`'s low `width` bits at bit `at` of the little-endian `word`.
fn put(word: &mut [u8], at: usize, width: usize, v: u64) {
    for i in 0..width {
        if v >> i & 1 == 1 {
            word[(at + i) / 8] |= 1 << ((at + i) % 8);
        }
    }
}

/// A `(lanes, k)` FP16 operand as MXFP7 entries, tile-major: entries run
/// (tile, K-chunk, group, block), tiles of `groups` lane groups, chunks of
/// `blocks` K-blocks. With `per = kblock / words`, word w of an entry
/// carries K `[w*per, (w+1)*per)` as `lanes * per` int7 at slot
/// `lane*per + kk` (B packing: `kk*lanes + lane`) from its top bit down, and
/// the lanes' E5M3 scales in its low bits, lane 0 highest; little-endian.
/// Entries of K pairs (`blocks` >= 2) quantise zeros as [`ZERO_LIFT`].
pub fn pack(
    x: &[u16],
    lanes: usize,
    k: usize,
    groups: usize,
    blocks: usize,
    blayout: bool,
    f: &Mx7,
) -> Result<Vec<u8>> {
    let (g_total, nk_total) = (lanes / f.lanes, k / f.kblock);
    let per = f.kblock / f.words;
    let word_bits = 8 * f.word_bytes;
    let whole = lanes.is_multiple_of(f.lanes)
        && k.is_multiple_of(f.kblock)
        && groups > 0
        && blocks > 0
        && g_total.is_multiple_of(groups)
        && nk_total.is_multiple_of(blocks)
        && x.len() == lanes * k;
    if !whole && !crate::mutant::on("l1.layout/partial-tile") {
        return Err(Error::new(
            "L1.BODY.002",
            "pad the operand to whole tiles before packing",
        ));
    }
    let x = match blocks >= 2 && !crate::mutant::on("num.mx7/unit-scale-zero") {
        true => lift(x),
        false => x.to_vec(),
    };
    let mut q = vec![0i64; lanes * k];
    let mut field = vec![0i64; lanes * nk_total];
    for r in 0..lanes {
        let (qr, sr) = quantise(&x[r * k..(r + 1) * k], f.kblock)?;
        q[r * k..(r + 1) * k].copy_from_slice(&qr);
        for (b, (es, m8)) in sr.into_iter().enumerate() {
            field[r * nk_total + b] = ((es + sbias()) & 0x1F) << 3 | ((m8 - 8) & 7);
        }
    }
    let mut out = Vec::with_capacity(g_total * nk_total * f.entry_bytes());
    let (chunks, group_major) = (
        nk_total / blocks,
        crate::mutant::on("num.layout/entries-group-major"),
    );
    for t in 0..g_total / groups {
        for o in 0..chunks * groups {
            let (c, g) = match group_major {
                true => (o % chunks, o / chunks),
                false => (o / groups, o % groups),
            };
            {
                for b in 0..blocks {
                    let kb = c * blocks + b;
                    let row = |l: usize| (t * groups + g) * f.lanes + l;
                    for w in 0..f.words {
                        let mut word = vec![0u8; f.word_bytes];
                        for l in 0..f.lanes {
                            let s = field[row(l) * nk_total + kb] as u64;
                            put(&mut word, SBITS * (f.lanes - 1 - l), SBITS, s);
                        }
                        for s in 0..f.lanes * per {
                            let (l, kk) = if blayout ^ crate::mutant::on("l1.layout/slot-order") {
                                (s % f.lanes, s / f.lanes)
                            } else {
                                (s / per, s % per)
                            };
                            let v = (q[row(l) * k + kb * f.kblock + w * per + kk] & 0x7F) as u64;
                            put(&mut word, word_bits - QBITS * (s + 1), QBITS, v);
                        }
                        if crate::mutant::on("num.layout/words-big-endian") {
                            word.reverse();
                        }
                        out.extend_from_slice(&word);
                    }
                }
            }
        }
    }
    Ok(out)
}

/// The clusters' A packing: tiles of `gm` lane groups, `nk` K-blocks a chunk.
pub fn pack_a(x: &[u16], lanes: usize, k: usize, gm: usize, nk: usize, f: &Mx7) -> Result<Vec<u8>> {
    let b = crate::mutant::on("num.layout/a-as-b");
    pack(x, lanes, k, gm, nk, b, f)
}

/// The clusters' B packing: tiles of `gn` lane groups, `nk` K-blocks a chunk.
pub fn pack_b(x: &[u16], lanes: usize, k: usize, gn: usize, nk: usize, f: &Mx7) -> Result<Vec<u8>> {
    let b = !crate::mutant::on("num.layout/b-as-a");
    pack(x, lanes, k, gn, nk, b, f)
}

/// FP16 bit patterns as float64 as `consumer` reads them (RUL.057): the
/// vector unpack reads IEEE fp16 (subnormals, infinities, NaN); the
/// clusters and the SRU the accumulator's binade form ([`fp16_value`]).
pub fn fp16_values(bits: &[u16], consumer: &str) -> Result<Vec<f64>> {
    let consumer = if crate::mutant::on("num.fp16/consumer-ignored") {
        "sru"
    } else {
        consumer
    };
    match consumer {
        "vector" => Ok(bits.iter().map(|b| ieee_f16(*b)).collect()),
        "cluster" | "sru" => Ok(bits.iter().map(|b| fp16_value(*b)).collect()),
        other => Err(Error::new(
            "L1.BODY.002",
            format!("no fp16 consumer {other}: vector, cluster or sru"),
        )),
    }
}
