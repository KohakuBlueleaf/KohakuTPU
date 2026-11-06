//! The memory mover (`docs/spec/stack/isa/mover.md`): one move at a time.

use crate::mem::{Dram, Word, WORD};
use crate::num::mx7;
use crate::R;

/// A walker level (ISA.MV.015, .026).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Level {
    pub count: i64,
    pub stride: i64,
    pub axis: i64,
    pub astep: i64,
}

/// Levels outermost first, windows `(base, extent)` of axes 1 and 2.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Walk {
    pub base: i64,
    pub levels: Vec<Level>,
    pub windows: [(i64, i64); 2],
}

impl Walk {
    /// Each element's address, and whether its window holds it (ISA.MV.026).
    fn elements(&self) -> Vec<(i64, bool)> {
        let mut out = vec![(self.base, [self.windows[0].0, self.windows[1].0])];
        for l in &self.levels {
            let n = l.count.max(1);
            let mut next = Vec::with_capacity(out.len() * n as usize);
            for (at, axes) in &out {
                for i in 0..n {
                    let mut a = *axes;
                    if l.axis > 0 {
                        a[l.axis as usize - 1] += i * l.astep;
                    }
                    next.push((at + i * l.stride, a));
                }
            }
            out = next;
        }
        let inside = |axes: &[i64; 2]| {
            (0..2).all(|k| self.windows[k].1 == 0 || (0..self.windows[k].1).contains(&axes[k]))
        };
        out.into_iter().map(|(at, a)| (at, inside(&a))).collect()
    }
}

/// A move's word width of its immediate, replicated (ISA.MV.041).
fn replicate(value: u64, ew: u32) -> Word {
    let mut w = [0u8; WORD];
    let bytes = (ew / 8) as usize;
    let v = value.to_le_bytes();
    for (i, b) in w.iter_mut().enumerate() {
        *b = v[i % bytes];
    }
    w
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Move {
    Copy {
        src: Walk,
        dst: Walk,
        pad: u64,
        ew: u32,
    },
    Fill {
        dst: Walk,
        value: u64,
        ew: u32,
    },
    Quant {
        src: Walk,
        dst: Walk,
        b: bool,
    },
    Gt4 {
        src: Walk,
        dst: Walk,
    },
    Gather {
        src: i64,
        dst: Walk,
        index: Index,
    },
    Scatter {
        src: Walk,
        dst: i64,
        index: Index,
    },
    Gen {
        dst: Walk,
        seed: u64,
    },
}

/// A generated word: keyed by the seed and its address alone (L1.MOV.006).
fn generated(seed: u64, at: i64) -> Word {
    let mut w = [0u8; WORD];
    for (i, chunk) in w.chunks_mut(8).enumerate() {
        let mut z = seed ^ (at as u64 + i as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        chunk.copy_from_slice(&(z ^ (z >> 31)).to_le_bytes());
    }
    w
}

/// An index list: where, how many, the row pitch and words, fp32 values.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Index {
    pub at: i64,
    pub n: i64,
    pub pitch: i64,
    pub words: i64,
    pub fp: bool,
}

impl Index {
    /// The list's values (ISA.MV.050-.051).
    fn read(&self, mem: &Dram) -> R<Vec<i64>> {
        let bytes = mem.read(self.at, self.n as usize * 4)?;
        bytes
            .chunks(4)
            .map(|c| {
                let b = u32::from_le_bytes(c.try_into().expect("4 bytes"));
                if !self.fp {
                    return Ok(i64::from(b));
                }
                let f = f32::from_bits(b);
                if f < 0.0 || f.fract() != 0.0 || f >= 2147483648.0 {
                    return Err(format!("fp32 index {f} faults F_INDEX"));
                }
                Ok(f as i64)
            })
            .collect()
    }
}

/// One move, whole.
pub fn run(m: &Move, mem: &mut Dram) -> R<()> {
    if mem.reach.is_some() {
        trace_reads(m, mem)?;
    }
    match m {
        Move::Copy { src, dst, pad, ew } => {
            let padding = replicate(*pad, *ew);
            let from = src.elements();
            let to = dst.elements();
            let mut read = from.iter().cycle();
            let mut words = Vec::with_capacity(to.len());
            for _ in &to {
                let (at, inside) = read.next().expect("a source element");
                words.push(if *inside { mem.word(*at)? } else { padding });
            }
            for ((at, inside), w) in to.iter().zip(&words) {
                if *inside {
                    mem.set_word(*at, w)?;
                }
            }
        }
        Move::Fill { dst, value, ew } => {
            let w = replicate(*value, *ew);
            for (at, inside) in dst.elements() {
                if inside {
                    mem.set_word(at, &w)?;
                }
            }
        }
        Move::Gen { dst, seed } => {
            for (at, inside) in dst.elements() {
                if inside {
                    mem.set_word(at, &generated(*seed, at))?;
                }
            }
        }
        Move::Quant { src, dst, b } => {
            let from = src.elements();
            let to = dst.elements();
            if from.len() % 8 != 0 || from.len() / 8 != to.len() {
                return Err(format!(
                    "quant of {} words to {} entries",
                    from.len(),
                    to.len()
                ));
            }
            for (group, (at, _)) in from.chunks(8).zip(&to) {
                let mut x = [[0u16; mx7::KBLOCK]; mx7::LANES];
                for (l, row) in x.iter_mut().enumerate() {
                    for w in 0..2 {
                        let word = mem.word(group[2 * l + w].0)?;
                        for e in 0..16 {
                            row[w * 16 + e] = u16::from_le_bytes([word[e * 2], word[e * 2 + 1]]);
                        }
                    }
                }
                for (i, w) in mx7::entry(&x).write(*b).iter().enumerate() {
                    mem.set_word(at + (i * WORD) as i64, w)?;
                }
            }
        }
        Move::Gt4 { src, dst } => {
            let from = src.elements();
            let to = dst.elements();
            if from.len() % 4 != 0 || from.len() / 4 != to.len() {
                return Err(format!(
                    "gt4 of {} words to {} groups",
                    from.len(),
                    to.len()
                ));
            }
            for (group, (at, _)) in from.chunks(4).zip(&to) {
                let mut words = [[0u8; WORD]; 4];
                for (g, (a, _)) in group.iter().enumerate() {
                    let w = mem.word(*a)?;
                    for i in 0..4 {
                        words[i][g * 8..g * 8 + 8].copy_from_slice(&w[i * 8..i * 8 + 8]);
                    }
                }
                for (i, w) in words.iter().enumerate() {
                    mem.set_word(at + (i * WORD) as i64, w)?;
                }
            }
        }
        Move::Gather { src, dst, index } => {
            let list = index.read(mem)?;
            let to = dst.elements();
            for (r, row) in to.chunks(index.words as usize).enumerate() {
                let Some(i) = list.get(r) else { break };
                for (w, (at, inside)) in row.iter().enumerate() {
                    if *inside {
                        let word = mem.word(src + i * index.pitch + (w * WORD) as i64)?;
                        mem.set_word(*at, &word)?;
                    }
                }
            }
        }
        Move::Scatter { src, dst, index } => {
            let list = index.read(mem)?;
            let from = src.elements();
            for (r, row) in from.chunks(index.words as usize).enumerate() {
                let Some(i) = list.get(r) else { break };
                for (w, (at, _)) in row.iter().enumerate() {
                    let word = mem.word(*at)?;
                    mem.set_word(dst + i * index.pitch + (w * WORD) as i64, &word)?;
                }
            }
        }
    }
    Ok(())
}

/// Read addresses before numeric execution, including index-data reads.
fn trace_reads(m: &Move, mem: &mut Dram) -> R<()> {
    let mut streams: Vec<(&str, Vec<Option<i64>>)> = Vec::new();
    match m {
        Move::Copy { src, dst, .. } => {
            let from = src.elements();
            streams.push((
                "mover_copy",
                from.iter()
                    .cycle()
                    .take(dst.elements().len())
                    .map(|&(at, inside)| inside.then_some(at))
                    .collect(),
            ));
        }
        Move::Quant { src, .. } | Move::Gt4 { src, .. } => {
            streams.push((
                "mover_transform",
                src.elements()
                    .into_iter()
                    .map(|(at, inside)| inside.then_some(at))
                    .collect(),
            ));
        }
        Move::Gather { src, dst, index } => {
            let list = index.read(mem)?;
            streams.push((
                "mover_index",
                (0..((index.n + 7) / 8))
                    .map(|i| Some(index.at + i * 32))
                    .collect(),
            ));
            let to = dst.elements();
            let mut reads = Vec::new();
            for (row, destinations) in to.chunks(index.words as usize).enumerate() {
                let Some(i) = list.get(row) else { break };
                for (word, (_, inside)) in destinations.iter().enumerate() {
                    reads.push(inside.then_some(src + i * index.pitch + word as i64 * 32));
                }
            }
            streams.push(("mover_gather", reads));
        }
        Move::Scatter { src, index, .. } => {
            streams.push((
                "mover_index",
                (0..((index.n + 7) / 8))
                    .map(|i| Some(index.at + i * 32))
                    .collect(),
            ));
            streams.push((
                "mover_scatter",
                src.elements()
                    .into_iter()
                    .map(|(at, inside)| inside.then_some(at))
                    .collect(),
            ));
        }
        Move::Fill { .. } | Move::Gen { .. } => {}
    }
    if let Some(reach) = &mut mem.reach {
        for (class, addresses) in streams {
            reach.mover(class, addresses);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn walk(base: i64, levels: &[(i64, i64)]) -> Walk {
        Walk {
            base,
            levels: levels
                .iter()
                .map(|(count, stride)| Level {
                    count: *count,
                    stride: *stride,
                    axis: 0,
                    astep: 0,
                })
                .collect(),
            windows: [(0, 0); 2],
        }
    }

    #[test]
    fn a_walk_is_outermost_first() {
        let got: Vec<i64> = walk(64, &[(2, 320), (3, 32)])
            .elements()
            .into_iter()
            .map(|e| e.0)
            .collect();
        assert_eq!(got, [64, 96, 128, 384, 416, 448]);
    }

    #[test]
    fn gt4_is_its_own_inverse() {
        let mut mem = Dram::new(1024);
        for (i, b) in mem.bytes[..128].iter_mut().enumerate() {
            *b = i as u8;
        }
        let want = mem.bytes[..128].to_vec();
        let once = Move::Gt4 {
            src: walk(0, &[(4, 32)]),
            dst: walk(256, &[(1, 128)]),
        };
        run(&once, &mut mem).unwrap();
        assert_ne!(mem.bytes[256..384], want[..]);
        let back = Move::Gt4 {
            src: walk(256, &[(4, 32)]),
            dst: walk(512, &[(1, 128)]),
        };
        run(&back, &mut mem).unwrap();
        assert_eq!(mem.bytes[512..640], want[..]);
    }

    #[test]
    fn a_source_outside_its_window_reads_the_pad() {
        let mut mem = Dram::new(256);
        mem.bytes[..32].fill(7);
        let mut src = walk(0, &[(2, 32)]);
        src.levels[0].axis = 1;
        src.levels[0].astep = 1;
        src.windows[0] = (0, 1);
        let m = Move::Copy {
            src,
            dst: walk(64, &[(2, 32)]),
            pad: 0x0102,
            ew: 16,
        };
        run(&m, &mut mem).unwrap();
        assert_eq!(mem.bytes[64], 7);
        assert_eq!(mem.bytes[96..98], [2, 1]);
    }
}
