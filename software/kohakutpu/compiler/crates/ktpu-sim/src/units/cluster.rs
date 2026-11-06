//! A matmul cluster (`docs/spec/stack/isa/cluster.md`): operand L1 and the
//! accumulator, one word at a time.

use super::link::{Data, Target};
use crate::mem::{Dram, Word as MemWord, WORD};
use crate::num::acc;
use crate::num::mx7::{Entry, ENTRY_BYTES, LANES};
use crate::R;

const ENTRY_WORDS: usize = ENTRY_BYTES / WORD;

pub const BANK_ENTRIES: usize = 256;
const SUBTILES: usize = 4096;
const SUBTILE: usize = LANES * LANES;
/// K blocks a pumped sweep issue carries (`mg.kstep`).
const KSTEP: usize = 2;

/// A unit-output walk, shared by emitting sweeps and plain drains.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Output {
    pub to: Target,
    pub buf: u32,
    pub at: usize,
    pub n: usize,
    pub count: bool,
    pub credit: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Word {
    /// ISA.MG.026, .033: FP16 results emitted by the enclosed sweep.
    GemmTo {
        sweep: Box<Word>,
        output: Output,
    },
    /// ISA.MG.032, .050: resident results under granule credits.
    DrainCreditTo {
        output: Output,
    },
    Fill {
        /// Side B when true.
        b: bool,
        bank: usize,
        eoff: usize,
        addr: i64,
        n: usize,
    },
    Gemm {
        gm: usize,
        gn: usize,
        nk: usize,
        abank: usize,
        bbank: usize,
        aoff: usize,
        boff: usize,
        acc: bool,
        anchor: i64,
        emit: Option<i64>,
    },
    Drain {
        addr: i64,
        n: usize,
    },
    /// Collects an emitting sweep, whose sub-tiles have left.
    Fused,
    /// Waits for `n` entries landed on a side by COUNT bursts (L1.MG.005).
    FillRecv {
        b: bool,
        n: usize,
    },
    /// `n` sub-tiles to a unit's buffer (L1.MG.010); the machine carries them.
    DrainTo {
        to: Target,
        buf: u32,
        at: usize,
        n: usize,
        count: bool,
    },
}

/// A burst's `buf` that names the accumulator.
pub const BUF_ACC: u32 = 2;

pub struct Cluster {
    sides: [Vec<Entry>; 2],
    acc: Vec<[f64; SUBTILE]>,
    /// Words COUNT bursts landed on each side and no `recv` took yet.
    landed: [usize; 2],
    assembly: [u8; ENTRY_BYTES],
}

impl Default for Cluster {
    fn default() -> Cluster {
        Cluster {
            sides: [
                vec![Entry::empty(); 2 * BANK_ENTRIES],
                vec![Entry::empty(); 2 * BANK_ENTRIES],
            ],
            acc: vec![[0.0; SUBTILE]; SUBTILES],
            landed: [0; 2],
            assembly: [0; ENTRY_BYTES],
        }
    }
}

impl Cluster {
    /// Whether word `w` can start.
    pub fn ready(&self, w: &Word) -> bool {
        match *w {
            Word::FillRecv { b, n } => self.landed[usize::from(b)] >= n * ENTRY_WORDS,
            _ => true,
        }
    }

    /// The first `n` sub-tiles as a burst to `buf`.
    pub fn out(&self, buf: u32, n: usize) -> Data {
        if buf == BUF_ACC {
            return Data::Acc(self.acc[..n].to_vec());
        }
        Data::Words(self.words(n))
    }

    /// The first `n` sub-tiles as f16 words.
    pub fn words(&self, n: usize) -> Vec<MemWord> {
        (0..n)
            .map(|t| {
                let mut word = [0u8; WORD];
                for (e, x) in self.acc[t].iter().enumerate() {
                    word[e * 2..e * 2 + 2].copy_from_slice(&acc::drain(*x).to_le_bytes());
                }
                word
            })
            .collect()
    }

    /// A burst into operand L1 or the accumulator.
    pub fn land(&mut self, buf: u32, at: usize, data: &Data, count: bool) -> R<()> {
        match (buf, data) {
            (BUF_ACC, Data::Acc(tiles)) => {
                for (t, x) in tiles.iter().enumerate() {
                    for (e, v) in x.iter().enumerate() {
                        let slot = &mut self.acc[at / 2 + t][e];
                        *slot = acc::round(*slot + v);
                    }
                }
            }
            (0 | 1, Data::Words(words)) => {
                let side = buf as usize;
                let capacity = self.sides[side].len() * ENTRY_WORDS;
                if at > capacity || words.len() > capacity - at {
                    return Err("operand burst outside L1".into());
                }
                for (i, word) in words.iter().enumerate() {
                    let off = at + i;
                    let part = off % ENTRY_WORDS;
                    self.assembly[part * WORD..(part + 1) * WORD].copy_from_slice(word);
                    if part + 1 == ENTRY_WORDS {
                        let slot = off / ENTRY_WORDS;
                        self.sides[side][slot] = Entry::read(&self.assembly, buf == 1);
                        if count {
                            self.landed[side] += ENTRY_WORDS;
                        }
                    }
                }
            }
            _ => return Err(format!("a burst to cluster buffer {buf} of the wrong kind")),
        }
        Ok(())
    }

    /// Canonical packed words of resident operand entries (ISA.MG.061).
    pub fn operand_words(&self, b: bool, bank: usize, at: usize, n: usize) -> Vec<MemWord> {
        let start = bank * BANK_ENTRIES + at;
        self.sides[usize::from(b)][start..start + n]
            .iter()
            .flat_map(|entry| entry.write(b))
            .collect()
    }

    /// An entry placed on a side's bank, as a burst would.
    pub fn place(&mut self, b: bool, bank: usize, at: usize, e: Entry) {
        self.sides[usize::from(b)][bank * BANK_ENTRIES + at] = e;
    }

    fn drain(&self, addr: i64, n: usize, mem: &mut Dram) -> R<()> {
        for (t, word) in self.words(n).iter().enumerate() {
            mem.set_word(addr + (t * WORD) as i64, word)?;
        }
        Ok(())
    }

    pub fn run(&mut self, w: &Word, mem: &mut Dram) -> R<()> {
        match *w {
            Word::Fill {
                b,
                bank,
                eoff,
                addr,
                n,
            } => {
                if let Some(reach) = &mut mem.reach {
                    reach.request("mg_fill", addr, n * ENTRY_BYTES);
                }
                for i in 0..n {
                    let bytes = mem.read(addr + (i * ENTRY_BYTES) as i64, ENTRY_BYTES)?;
                    let at = bank * BANK_ENTRIES + (eoff + i) % BANK_ENTRIES;
                    self.sides[usize::from(b)][at] = Entry::read(bytes, b);
                }
            }
            Word::Gemm {
                gm,
                gn,
                nk,
                abank,
                bbank,
                aoff,
                boff,
                acc: keep,
                anchor,
                emit,
            } => {
                let side = |s: usize, bank: usize, at: usize| {
                    &self.sides[s][bank * BANK_ENTRIES + at % BANK_ENTRIES]
                };
                let issue = if nk >= KSTEP { KSTEP } else { 1 };
                let mut out = Vec::new();
                for (i, kb) in (0..nk).step_by(issue).enumerate() {
                    for g in 0..gm {
                        for h in 0..gn {
                            let blocks: Vec<(&Entry, &Entry)> = (kb..kb + issue)
                                .map(|k| {
                                    (
                                        side(0, abank, aoff + g * nk + k),
                                        side(1, bbank, boff + h * nk + k),
                                    )
                                })
                                .collect();
                            let add = keep || i > 0;
                            out.push((g * gn + h, add, acc::subtile(&blocks, anchor)));
                        }
                    }
                }
                for (t, add, x) in out {
                    for (e, v) in x.iter().enumerate() {
                        let slot = &mut self.acc[t][e];
                        *slot = if add { acc::round(*slot + v) } else { *v };
                    }
                }
                if let Some(addr) = emit {
                    self.drain(addr, gm * gn, mem)?;
                }
            }
            Word::Drain { addr, n } => self.drain(addr, n, mem)?,
            Word::Fused => {}
            Word::FillRecv { b, n } => self.landed[usize::from(b)] -= n * ENTRY_WORDS,
            Word::DrainTo { .. } | Word::GemmTo { .. } | Word::DrainCreditTo { .. } => {
                unreachable!("the machine carries unit output")
            }
        }
        Ok(())
    }
}
