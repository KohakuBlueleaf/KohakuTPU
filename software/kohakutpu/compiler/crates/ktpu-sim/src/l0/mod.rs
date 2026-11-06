//! The L0 simulator: a bound package's steps on the machine, its words read
//! back by the ISA's formats (`docs/spec/stack/l0/`).

pub mod card;
mod mover;
pub mod words;

use std::collections::HashMap;
use std::rc::Rc;

use ktpu_isa::{Isa, W256};
use ktpu_l0::format::{F_IMM, F_POSTED, MOVER_SKIP};
use ktpu_l0::{Op, Package, Step, Word, WordOps};

use crate::machine::Machine;
use crate::units::link::Place;
use crate::R;

use mover::Regs;

/// Engine entry codes (L0.DE): mailbox DST, ARG0..3, GO, a WAIT, the
/// mover's and the interlink's register windows, a fetch's relative flag.
const MB_DST: u64 = 0;
const MB_ARG2: u64 = 3;
const MB_ARG3: u64 = 4;
const GO: u64 = 5;
const MOVER_CODES: std::ops::Range<u64> = 8..24;
const WAIT: u64 = 32;
const REL: u64 = 0x40;
const NONE: u64 = 0xFF;
/// A fetch request's DST: typed, MEM_RD_REQ (L0.STEP.044).
const TYPED_FETCH: u64 = 1 << 24;
const WORD_BYTES: u64 = 32;
const REGS: usize = 16;

/// The mesh's units by coordinate, sorted as `vc[i]`, `mg[i]` name them.
#[derive(Clone, Debug)]
pub struct Units {
    pub vc: Vec<(u64, u64)>,
    pub mg: Vec<(u64, u64)>,
    /// The engine's WAIT source of the mover's done count.
    pub src_mover: u64,
    /// `node.engine.counter_bits`: a WAIT compares counts modulo this (L0.DE.006).
    pub counter_bits: u32,
}

struct Node<'a> {
    pkg: &'a Package,
    units: &'a Units,
    m: &'a mut Machine,
    /// Completions before this package, by package unit, and the moves.
    base: Vec<u64>,
    moves_base: u64,
    expected: Vec<u64>,
    sent: Vec<u64>,
    frontiers: HashMap<u64, (u64, u64)>,
    gos: u64,
    regs: [u64; REGS],
    mover: Regs,
}

/// Runs `pkg`, every unit run out at its end.
pub fn run(pkg: &Package, units: &Units, m: &mut Machine) -> R<()> {
    let at = |(x, y): &(u64, u64)| (*x as u32, *y as u32);
    m.places.extend(
        units
            .vc
            .iter()
            .enumerate()
            .map(|(i, c)| (at(c), Place::Vc(i))),
    );
    m.places.extend(
        units
            .mg
            .iter()
            .enumerate()
            .map(|(i, c)| (at(c), Place::Mg(i))),
    );
    let mut node = Node {
        base: vec![0; pkg.units.len()],
        moves_base: m.moves_done,
        expected: vec![0; pkg.units.len()],
        sent: vec![0; pkg.units.len()],
        frontiers: HashMap::new(),
        gos: 0,
        regs: [0; REGS],
        mover: Regs::default(),
        pkg,
        units,
        m,
    };
    for u in 0..pkg.units.len() {
        node.base[u] = node.done_raw(u)?;
    }
    for (i, s) in pkg.steps.iter().enumerate() {
        if s.op == Op::End {
            break;
        }
        node.step(s)
            .map_err(|e| format!("step {i} {}: {e}", s.op.name()))?;
    }
    node.barrier()?;
    node.m.until(Machine::idle)
}

impl Node<'_> {
    fn unit_at(&self, xy: (u64, u64)) -> R<Target> {
        if let Some(i) = self.units.vc.iter().position(|c| *c == xy) {
            return Ok(Target::Vc(i));
        }
        if let Some(i) = self.units.mg.iter().position(|c| *c == xy) {
            return Ok(Target::Mg(i));
        }
        Err(format!("no unit at {xy:?}"))
    }

    fn done_raw(&self, u: usize) -> R<u64> {
        let unit = self.pkg.units[u];
        Ok(match self.unit_at((unit.x, unit.y))? {
            Target::Vc(i) => self.m.vc[i].done,
            Target::Mg(i) => self.m.mg[i].done,
        })
    }

    fn wait_unit(&mut self, u: usize, want: u64) -> R<()> {
        let unit = self.pkg.units[u];
        let upto = self.base[u] + want;
        match self.unit_at((unit.x, unit.y))? {
            Target::Vc(i) => self.m.until(|m| m.vc[i].done >= upto),
            Target::Mg(i) => self.m.until(|m| m.mg[i].done >= upto),
        }
    }

    fn wait_moves(&mut self, want: u64) -> R<()> {
        let upto = self.moves_base + want;
        self.m.until(|m| m.moves_done >= upto)
    }

    fn isa(&self) -> R<Rc<Isa>> {
        self.m.isa.clone().ok_or("no ISA".into())
    }

    /// An engine `WAIT`: holds until `(count(src) - want) mod 2^bits` is below
    /// half the range.
    fn engine_wait(&mut self, src: u64, want: u64) -> R<()> {
        let bits = self.units.counter_bits;
        let reached =
            move |count: u64| (count.wrapping_sub(want) & ((1 << bits) - 1)) >> (bits - 1) == 0;
        if src == self.units.src_mover {
            let base = self.moves_base;
            return self.m.until(|m| reached(m.moves_done - base));
        }
        let unit = self.pkg.units[src as usize];
        let base = self.base[src as usize];
        match self.unit_at((unit.x, unit.y))? {
            Target::Vc(i) => self.m.until(|m| reached(m.vc[i].done - base)),
            Target::Mg(i) => self.m.until(|m| reached(m.mg[i].done - base)),
        }
    }

    fn deliver(&mut self, xy: (u64, u64), w: W256) -> R<()> {
        let isa = self.isa()?;
        match self.unit_at(xy)? {
            Target::Vc(i) => self.m.send_vc(i, words::vector(&isa, w)?),
            Target::Mg(i) => self.m.send_mg(i, words::cluster(&isa, w)?),
        }
    }

    fn barrier(&mut self) -> R<()> {
        for u in 0..self.pkg.units.len() {
            let want = self.sent[u].max(self.expected[u]);
            self.wait_unit(u, want)?;
        }
        self.wait_moves(self.gos)
    }

    fn payload(&self, i: u64) -> R<Word> {
        self.pkg
            .payloads
            .get(i as usize)
            .copied()
            .ok_or(format!("payload {i}"))
    }

    fn write_mover(&mut self, reg: u64, value: u64) -> R<()> {
        let isa = self.isa()?;
        if let Some(mv) = self.mover.write(reg, value, &isa)? {
            self.m.post_move(mv);
            self.gos += 1;
        }
        Ok(())
    }

    fn sent_to(&mut self, xy: (u64, u64), n: u64) {
        let at = self.pkg.units.iter().position(|u| (u.x, u.y) == xy);
        if let Some(u) = at {
            self.sent[u] += n;
        }
    }

    fn engine(&mut self, s: &Step) -> R<()> {
        let mut mb = [0u64; 5];
        let mut rel = false;
        for k in 0..s.count {
            let w = self.payload(s.arg + k / 3)?;
            let j = (k % 3) as u32;
            let (code, value) = (w.get(8 * j, 8), w.limb(64 * (j + 1)));
            match code {
                NONE => {}
                c if c == WAIT => {
                    let bits = self.units.counter_bits;
                    self.engine_wait(value >> 56, value & ((1 << bits) - 1))?;
                }
                GO => {
                    let dst = mb[MB_DST as usize];
                    if dst & TYPED_FETCH != 0 {
                        let yx = (mb[MB_ARG2 as usize] >> 40) & 0xFF;
                        let xy = (yx & 0xF, yx >> 4);
                        let arg3 = mb[MB_ARG3 as usize];
                        let (at, n) = (arg3 >> 24, arg3 & 0xFF);
                        for i in 0..n {
                            let word = if rel {
                                self.payload(at / WORD_BYTES + i)?
                            } else {
                                let bytes = self.m.mem.word((at + i * WORD_BYTES) as i64)?;
                                Word(std::array::from_fn(|l| {
                                    let limb = bytes[8 * l..8 * l + 8].try_into();
                                    u64::from_le_bytes(limb.expect("eight bytes"))
                                }))
                            };
                            self.deliver(xy, W256(word.0))?;
                        }
                        self.sent_to(xy, n);
                    } else {
                        let xy = (dst & 0xFF, (dst >> 8) & 0xFF);
                        self.deliver(xy, W256([mb[1], mb[2], mb[3], mb[4]]))?;
                        self.sent_to(xy, 1);
                    }
                }
                c if c & !REL <= MB_ARG3 => {
                    rel = c & REL != 0 || (rel && c & !REL != MB_ARG3);
                    mb[(c & !REL) as usize] = value;
                }
                c if MOVER_CODES.contains(&c) => self.write_mover((c - 8) << 3, value)?,
                c => return Err(format!("engine code {c:#x} in this simulator")),
            }
        }
        Ok(())
    }

    fn step(&mut self, s: &Step) -> R<()> {
        match s.op {
            Op::Engine => self.engine(s),
            Op::Dispatch => {
                let unit = self.pkg.units[s.unit as usize];
                for k in 0..s.count {
                    let w = self.payload(s.arg + k)?;
                    self.deliver((unit.x, unit.y), W256(w.0))?;
                }
                self.sent[s.unit as usize] += s.count;
                Ok(())
            }
            Op::Frontier => {
                if s.flags == 2 {
                    if s.unit != 0
                        || s.arg > self.pkg.payloads.len() as u64
                        || s.count > self.pkg.payloads.len() as u64 - s.arg
                    {
                        return Err("invalid FRONTIER clear".into());
                    }
                    self.frontiers
                        .retain(|k, _| !(s.arg..s.arg + s.count).contains(k));
                    return Ok(());
                }
                let u = s.unit as usize;
                if u >= self.sent.len() || s.count >= self.pkg.payloads.len() as u64 || s.arg != 0 {
                    return Err("invalid FRONTIER fields".into());
                }
                match s.flags {
                    0 => {
                        self.frontiers.insert(s.count, (s.unit, self.sent[u]));
                        Ok(())
                    }
                    1 => {
                        let &(_, target) = self
                            .frontiers
                            .get(&s.count)
                            .filter(|(unit, _)| *unit == s.unit)
                            .ok_or_else(|| "FRONTIER wait has no saved target".to_string())?;
                        self.expected[u] = self.expected[u].max(target);
                        self.wait_unit(u, self.expected[u])
                    }
                    _ => Err("invalid FRONTIER flags".into()),
                }
            }
            Op::Await => {
                let u = s.unit as usize;
                self.expected[u] = if s.flags == F_IMM {
                    self.sent[u]
                } else {
                    self.expected[u] + s.count
                };
                self.wait_unit(u, self.expected[u])
            }
            Op::Barrier => self.barrier(),
            Op::Mover => {
                for k in 0..s.count {
                    let w = self.payload(s.arg + k)?;
                    for half in [0, 128] {
                        let (reg, value) = (w.limb(half), w.limb(half + 64));
                        if reg != MOVER_SKIP {
                            self.write_mover(reg, value)?;
                        }
                    }
                }
                if s.flags & F_POSTED == 0 {
                    self.wait_moves(self.gos)?;
                }
                Ok(())
            }
            Op::Mwait => self.wait_moves(s.count),
            Op::Load => {
                let w = self.payload(s.arg)?.limb(0);
                let reg = s.unit as usize;
                self.regs[reg] = if s.flags == F_IMM {
                    w
                } else if s.flags == 0 {
                    self.m.mem.u64(w as i64)?
                } else {
                    return Err(format!("LOAD flags {}", s.flags));
                };
                Ok(())
            }
            Op::AssertZero => match self.regs[s.unit as usize] {
                0 => Ok(()),
                v => Err(format!("r{} = {v:#x} fails contract {}", s.unit, s.count)),
            },
            op => Err(format!("{} in this simulator", op.name())),
        }
    }
}

enum Target {
    Vc(usize),
    Mg(usize),
}
