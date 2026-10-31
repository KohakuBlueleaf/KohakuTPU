//! L0.DE.040: before a lowered package is sent, its ENGINE entries are read
//! back as the node reads them and held against the bound package they were
//! lowered from. Written from the spec text alone (L0.DE.002-006, L0.DE.023,
//! L0.DE.024, L0.DE.029, L0.DE.032, L0.STEP.044, L0.STEP.045) with its own
//! table: it shares no constant or function with `engine`, so a fault in the
//! lowering's constants cannot hide in both halves.

use std::collections::VecDeque;

use crate::error::Result;
use crate::facts::Facts;
use crate::format::{Op, Package};
use crate::mutant;
use crate::refuse;
use crate::word::{Word, WordOps};

const CLAUSE: &str = "L0.DE.040";
/// L0.DE.002: codes 0-31 write a register; 32 waits.
const C_WAIT: u64 = 32;
/// L0.DE.004: the mailbox codes.
const C_DST: u64 = 0;
const C_ARG3: u64 = 4;
const C_GO: u64 = 5;
/// L0.DE.005, L0.DE.031: a RING's interlink write.
const C_RING: u64 = 26;
/// L0.DE.024: the code bit the node turns into a package-relative address.
const C_REL: u64 = 1 << 6;
/// L0.STEP.044: a typed MEM_RD_REQ's DST above its port, and ARG3's middle.
const DST_READ: u64 = 1 << 4;
const ARG3_MID: u64 = 0x60;
/// L0.STEP.045: ARG3's count field; a run steps one 256-bit payload
/// (L0.PKG) per word.
const RUN_WORDS: u64 = 255;
const PAYLOAD_BYTES: u64 = 32;

/// What a unit is still owed, in order.
#[derive(Debug, Clone)]
enum Owed {
    /// Payloads `[next, end)` (a DISPATCH).
    Words { next: u64, end: u64 },
    /// `left` words of payloads the lowering appended (a REPEAT's).
    Appended { left: u64 },
    /// `left` words from absolute address `at` (a FETCH).
    Absolute { at: u64, left: u64 },
}

struct Node<'a> {
    low: &'a Package,
    first_new: u64,
    owed: Vec<VecDeque<Owed>>,
    sent: Vec<u64>,
    /// The want of the last WAIT on each unit since the last GO.
    waited: Vec<Option<u64>>,
    regs: [Option<(u64, bool)>; 5],
    mask: u64,
    depth: u64,
    /// RING entries since the last WAIT (RUL.046).
    rings: usize,
}

/// Refuses `low` (L0.DE.040) unless its ENGINE entries carry exactly what
/// `src`, the bound package it was lowered from, dispatches and fetches,
/// each unit's runs waited for as L0.STEP.045 bounds them, and every WAIT
/// names a unit of the package or the mover; refuses (RUL.046) more than
/// `node.ring_depth` RING entries between waits. A package without ENGINE
/// steps is not checked.
pub fn check(src: &Package, low: &Package, f: &Facts) -> Result<()> {
    if mutant::on("l0.check/off") || !low.steps.iter().any(|s| s.op == Op::Engine) {
        return Ok(());
    }
    let n = src.units.len();
    let mut node = Node {
        low,
        first_new: src.payloads.len() as u64,
        owed: vec![VecDeque::new(); n],
        sent: vec![0; n],
        waited: vec![None; n],
        regs: [None; 5],
        mask: (1u64 << f.counter_bits) - 1,
        depth: f.inst_depth,
        rings: 0,
    };
    for s in &src.steps {
        let u = s.unit as usize;
        let owed = match s.op {
            Op::End => break,
            Op::Dispatch => Owed::Words {
                next: s.arg,
                end: s.arg + s.count,
            },
            Op::Repeat => Owed::Appended {
                left: s.count * ((s.arg >> 32) & 0xFFFF),
            },
            Op::Fetch => Owed::Absolute {
                at: s.arg,
                left: s.count,
            },
            _ => continue,
        };
        let words = match owed {
            Owed::Words { next, end } => end - next,
            Owed::Appended { left } | Owed::Absolute { left, .. } => left,
        };
        if words == 0 {
            continue;
        }
        if u >= n {
            return refuse!(CLAUSE, "step names unit {u} of {n}");
        }
        node.owed[u].push_back(owed);
    }
    for (i, s) in low.steps.iter().enumerate() {
        if s.op != Op::Engine {
            // A firmware-path wait (L0.STEP.034, L0.STEP.035) holds too.
            if matches!(s.op, Op::WaitBell | Op::WaitSlot) {
                node.rings = 0;
            }
            continue;
        }
        let words = s.count.div_ceil(3);
        if s.arg + words > low.payloads.len() as u64 {
            return refuse!(CLAUSE, "ENGINE step {i}'s entries run past the payloads");
        }
        for k in 0..s.count {
            let w = low.payloads[(s.arg + k / 3) as usize];
            let j = (k % 3) as u32;
            node.entry(w.get(8 * j, 8), w.limb(64 * (j + 1)), f)?;
        }
    }
    for (u, q) in node.owed.iter().enumerate() {
        if let Some(o) = q.front() {
            return refuse!(CLAUSE, "unit {u} is never sent {o:?}");
        }
    }
    Ok(())
}

impl Node<'_> {
    fn entry(&mut self, code: u64, value: u64, f: &Facts) -> Result<()> {
        let rel = code & C_REL != 0;
        let code = code & !C_REL;
        if rel && code != C_ARG3 {
            return refuse!(CLAUSE, "code {code} carries REL; only ARG3 does");
        }
        match code {
            C_WAIT => {
                self.rings = 0;
                let src = (value >> 56) & 0x1F;
                if (src as usize) < self.sent.len() {
                    self.waited[src as usize] = Some(value & self.mask);
                } else if src as usize != f.engine_units {
                    return refuse!(CLAUSE, "a WAIT on source {src}: no unit or mover");
                }
            }
            C_DST..=C_ARG3 => self.regs[code as usize] = Some((value, rel)),
            C_GO => {
                if value != 1 {
                    return refuse!(CLAUSE, "GO {value}");
                }
                self.flit()?;
                self.waited.iter_mut().for_each(|w| *w = None);
            }
            C_RING => {
                self.rings += 1;
                if self.rings > f.ring_depth {
                    return refuse!(
                        "RUL.046",
                        "RING entry {} since the last wait; the interlink queues {}",
                        self.rings,
                        f.ring_depth
                    );
                }
            }
            6..=31 => {}
            _ => return refuse!(CLAUSE, "entry code {code} past WAIT"),
        }
        Ok(())
    }

    fn reg(&self, i: usize) -> Result<(u64, bool)> {
        match self.regs[i] {
            Some(r) => Ok(r),
            None => refuse!(CLAUSE, "GO before mailbox register {i} is written"),
        }
    }

    /// A GO: the mailbox's flit, a fetch request or one word.
    fn flit(&mut self) -> Result<()> {
        let (dst, _) = self.reg(0)?;
        if dst >> 20 == DST_READ {
            return self.request(dst & 0xF_FFFF);
        }
        let (x, y) = (dst & 0xFF, (dst >> 8) & 0xFF);
        let Some(u) = self.unit(x, y) else {
            return refuse!(CLAUSE, "a word to ({x}, {y}): no unit of the package");
        };
        if self.low.units[u].has_port() {
            return refuse!(CLAUSE, "a mailbox word to unit {u}, which has a fetch port");
        }
        let mut word = Word::ZERO;
        for j in 0..4 {
            word.set(64 * j as u32, 64, self.reg(1 + j)?.0);
        }
        self.run(u, 1, self.credit(u))?;
        match self.owed[u].front_mut() {
            Some(Owed::Words { next, end }) => {
                if self.low.payloads.get(*next as usize) != Some(&word) {
                    return refuse!(CLAUSE, "unit {u} is sent a word other than payload {next}");
                }
                *next += 1;
                if next == end {
                    self.owed[u].pop_front();
                }
            }
            Some(Owed::Appended { left }) => {
                *left -= 1;
                if *left == 0 {
                    self.owed[u].pop_front();
                }
            }
            other => return refuse!(CLAUSE, "unit {u} is sent a word; it is owed {other:?}"),
        }
        Ok(())
    }

    /// A MEM_RD_REQ to fetch port `port` (L0.STEP.044).
    fn request(&mut self, port: u64) -> Result<()> {
        let (a0, a1) = (self.reg(1)?.0, self.reg(2)?.0);
        let (a2, (a3, rel)) = (self.reg(3)?.0, self.reg(4)?);
        let (x, y) = ((a2 >> 40) & 0xF, (a2 >> 44) & 0xF);
        if a0 != 0 || a1 != 0 || a2 != ((y << 4 | x) << 40 | 1 << 30) {
            return refuse!(CLAUSE, "a fetch request's ARG0-ARG2 are not L0.STEP.044's");
        }
        let Some(u) = self.unit(x, y) else {
            return refuse!(CLAUSE, "a fetch for ({x}, {y}): no unit of the package");
        };
        let unit = self.low.units[u];
        if !unit.has_port() || port != unit.fetch & 0xFFFF {
            return refuse!(
                CLAUSE,
                "unit {u}'s fetch goes to port {port:#x}, not its own"
            );
        }
        let (n, mid, at) = (a3 & 0xFF, (a3 >> 8) & 0xFFFF, a3 >> 24);
        if mid != ARG3_MID || n == 0 || n > RUN_WORDS {
            return refuse!(
                CLAUSE,
                "a fetch request's ARG3 {a3:#x} is not L0.STEP.044's"
            );
        }
        let room = self.credit(u).min(self.depth);
        if n > room {
            return refuse!(CLAUSE, "a run of {n} words past unit {u}'s room {room}");
        }
        self.run(u, n, room)?;
        let total = self.low.payloads.len() as u64;
        match self.owed[u].front_mut() {
            Some(Owed::Words { next, end }) => {
                if !rel || at != *next * PAYLOAD_BYTES || *next + n > *end {
                    return refuse!(
                        CLAUSE,
                        "unit {u}'s run (REL {rel}, at {at:#x}, {n}) is not payloads {next}..{end}"
                    );
                }
                *next += n;
                if next == end {
                    self.owed[u].pop_front();
                }
            }
            Some(Owed::Appended { left }) => {
                let i = at / PAYLOAD_BYTES;
                if !rel
                    || at % PAYLOAD_BYTES != 0
                    || i < self.first_new
                    || i + n > total
                    || n > *left
                {
                    return refuse!(CLAUSE, "unit {u}'s run is not of appended payloads");
                }
                *left -= n;
                if *left == 0 {
                    self.owed[u].pop_front();
                }
            }
            Some(Owed::Absolute { at: want, left }) => {
                if rel || at != *want || n > *left {
                    return refuse!(CLAUSE, "unit {u}'s FETCH run is not at {want:#x}");
                }
                *want += n * PAYLOAD_BYTES;
                *left -= n;
                if *left == 0 {
                    self.owed[u].pop_front();
                }
            }
            None => return refuse!(CLAUSE, "unit {u} is sent a run it is not owed"),
        }
        Ok(())
    }

    /// `n` words to `u`: its run's WAIT asks for exactly `sent + n - room`
    /// when positive (L0.STEP.045, L0.DE.029).
    fn run(&mut self, u: usize, n: u64, room: u64) -> Result<()> {
        let need = (self.sent[u] + n) as i128 - room as i128;
        if need > 0 {
            let want = need as u64 & self.mask;
            if self.waited[u] != Some(want) {
                return refuse!(
                    CLAUSE,
                    "unit {u}'s run of {n} after {} sent waits for {:?}, not {want}",
                    self.sent[u],
                    self.waited[u]
                );
            }
        }
        self.sent[u] += n;
        Ok(())
    }

    fn credit(&self, u: usize) -> u64 {
        match self.low.units[u].credit {
            0 => u64::MAX,
            c => c,
        }
    }

    fn unit(&self, x: u64, y: u64) -> Option<usize> {
        self.low.units.iter().position(|u| u.x == x && u.y == y)
    }
}
