//! Vector programs and descriptors kept resident in each vector core
//! (L1.LOW.002, .003, .014).
//!
//! A program the core holds is sent as its DESC words and a RUN at its slot;
//! a program placed over words the core already holds is sent as the words
//! that differ; a DESC word writing the value its field holds is dropped.

use std::collections::{BTreeMap, HashMap};

use ktpu_isa::layout::value;
use ktpu_isa::unit::vector::{VecCu, VecIsa, OP_DESC, OP_IMEM, OP_RUN};
use ktpu_l0::{Coord, Word};
use ktpu_machine::Machine;

use crate::error::{Error, Result};

/// A program as `(address, word)` pairs: its key in the slots.
pub type ProgramKey = Vec<(u32, u64)>;

/// One core's instruction memory: which program occupies which words.
#[derive(Clone, Debug, Default)]
pub struct Resident {
    pub words: u32,
    pub run_reach: u32,
    /// Program -> (base, length), oldest use first.
    slots: Vec<(ProgramKey, (u32, u32))>,
    /// Where the last program placed here starts.
    pub last: u32,
    /// (descriptor, field) -> the (value, value_hi) the core holds.
    pub desc: HashMap<(i64, i64), (i64, i64)>,
    /// IMEM address -> the word the core holds there.
    pub mem: HashMap<u32, u64>,
}

impl Resident {
    pub fn new(words: u32, run_reach: u32) -> Resident {
        Resident {
            words,
            run_reach,
            ..Resident::default()
        }
    }

    /// `(base, length)` of each resident program, oldest use first (the
    /// eviction order).
    pub fn slots(&self) -> Vec<(u32, u32)> {
        self.slots.iter().map(|(_, s)| *s).collect()
    }

    /// Assume nothing is resident.
    pub fn forget(&mut self) {
        self.slots.clear();
        self.desc.clear();
        self.mem.clear();
        self.last = 0;
    }

    /// The lowest base with `length` free words a RUN can start at.
    fn free(&self, length: u32) -> Option<u32> {
        let mut taken: Vec<(u32, u32)> = self.slots.iter().map(|(_, s)| *s).collect();
        taken.sort();
        let mut at = 0u32;
        for (base, size) in taken {
            if at >= self.run_reach {
                return None;
            }
            if base >= at && base - at >= length {
                return Some(at);
            }
            at = at.max(base + size);
        }
        (self.words >= at + length && at < self.run_reach).then_some(at)
    }

    /// `(base, loaded)`: where the program runs, and whether it is there.
    pub fn place(&mut self, key: ProgramKey, length: u32) -> Result<(u32, bool)> {
        let (base, loaded) = self.placed(key, length)?;
        if crate::mutant::on("isa.vector/resident") {
            self.mem.clear();
            self.desc.clear();
            return Ok((base, false));
        }
        Ok((base, loaded))
    }

    fn placed(&mut self, key: ProgramKey, length: u32) -> Result<(u32, bool)> {
        if let Some(i) = self.slots.iter().position(|(k, _)| {
            if crate::mutant::on("imr/key-identity") {
                k.iter().map(|(a, _)| a).eq(key.iter().map(|(a, _)| a))
            } else {
                *k == key
            }
        }) {
            let hit = self.slots.remove(i);
            let base = hit.1 .0;
            if crate::mutant::on("l1.lower/hit-evicted-first") {
                self.slots.insert(0, hit);
            } else {
                self.slots.push(hit);
            }
            return Ok((base, true));
        }
        let mut base = self.free(length);
        while base.is_none() && !self.slots.is_empty() {
            self.slots.remove(0);
            base = self.free(length);
        }
        let Some(base) = base else {
            return Err(Error::new(
                "L1.LOW.003",
                format!(
                    "a {length}-word program exceeds the {}-word IMEM",
                    self.words
                ),
            ));
        };
        self.slots.push((key, (base, length)));
        Ok((base, false))
    }
}

/// `words` for one RUN-terminated vector kernel, against what `memory` holds.
pub fn rewrite(isa: &VecIsa, words: &[Word], memory: &mut Resident) -> Result<Vec<Word>> {
    let shift = isa.cfg.payload_bits - 4;
    let mut out = Vec::new();
    let mut prog: Vec<(u32, u64)> = Vec::new();
    let mut base = memory.last;
    for w in words {
        let p = *w;
        let code = p.get(shift, 4);
        if code == OP_IMEM {
            prog.push((isa.imem_addr(p), isa.imem_word(p)));
            continue;
        }
        if code == OP_DESC {
            let f = isa.desc.decode(p)?;
            let key = (value(&f, "ad"), value(&f, "fld"));
            let v = (value(&f, "value"), value(&f, "value_hi"));
            if memory.desc.get(&key) == Some(&v) && !crate::mutant::on("l1.lower/desc-always") {
                continue;
            }
            memory.desc.insert(key, v);
        }
        if !prog.is_empty() {
            let length = prog.iter().map(|(a, _)| *a).max().unwrap() + 1;
            let (at, loaded) = memory.place(prog.clone(), length)?;
            base = at;
            memory.last = base;
            if !loaded {
                for (addr, wd) in &prog {
                    let to = base + addr;
                    if memory.mem.get(&to) != Some(wd) {
                        memory.mem.insert(to, *wd);
                        out.push(
                            VecCu::Imem {
                                addr: to,
                                word: *wd,
                            }
                            .encode(isa)?,
                        );
                    }
                }
            }
            prog.clear();
        }
        if code == OP_RUN {
            let pc = value(&isa.run.decode(p)?, "pc") as u32;
            out.push(VecCu::Run { pc: base + pc }.encode(isa)?);
        } else {
            out.push(*w);
        }
    }
    if !prog.is_empty() {
        return Err(Error::new(
            "L1.LOW.014",
            "IMEM words with no RUN after them",
        ));
    }
    Ok(out)
}

/// Every vector core's `Resident`, made on first use: the state the vector
/// core's lowering hook keeps across packages.
#[derive(Clone, Debug)]
pub struct Cores {
    pub isa: VecIsa,
    pub words: u32,
    pub run_reach: u32,
    /// Sampled parameter words cannot establish constant residency.
    pub parameter_images: bool,
    pub cores: BTreeMap<Coord, Resident>,
    /// The first refusal a rewrite met inside a build.
    pub error: Option<Error>,
}

impl Cores {
    pub fn new(m: &Machine) -> Result<Cores> {
        Ok(Cores {
            isa: VecIsa::new(m)?,
            words: m.vc.imem_words,
            run_reach: m.vc.run_reach,
            parameter_images: false,
            cores: BTreeMap::new(),
            error: None,
        })
    }

    /// The refusal a build's rewrite met, if any.
    pub fn take_error(&mut self) -> Result<()> {
        match self.error.take() {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}

impl ktpu_l0::Resident for Cores {
    /// The vector core's stream rewritten; a refusal is kept in `error` (its
    /// clause and message) for the build's caller.
    fn lower(
        &mut self,
        unit_type: &str,
        coord: Coord,
        words: Vec<Word>,
    ) -> ktpu_l0::Result<Vec<Word>> {
        if unit_type != "VC" {
            return Ok(words);
        }
        let (n, reach) = (self.words, self.run_reach);
        let memory = self
            .cores
            .entry(coord)
            .or_insert_with(|| Resident::new(n, reach));
        if self.parameter_images && !crate::mutant::on("l1.img/parameter-elision") {
            memory.forget();
        }
        rewrite(&self.isa, &words, memory).map_err(|e| {
            let msg = e.message.clone();
            self.error = Some(e);
            ktpu_l0::Refusal::new("L1.LOW.003", msg)
        })
    }
}
