//! Packages lowered for a node's dispatch engine (docs/spec/stack/l0/
//! dispatch-engine.md): every step the engine carries becomes the
//! control-register writes and WAITs it stands for, three entries a payload,
//! in ENGINE steps; the steps it cannot carry stay steps.

use std::collections::HashMap;

use crate::error::Result;
use crate::facts::Facts;
use crate::format::{Op, Package, Step, CREDIT_UNBOUNDED, F_POSTED, MOVER_SKIP, PAYLOAD_BYTES};
use crate::mutant;
use crate::refuse;
use crate::word::{mask64, Word, WordOps};

/// The entry code of a WAIT (L0.DE.002).
pub const WAIT: u64 = 32;
/// A code flag: the value is a fetch request's ARG3 and the node adds the
/// package's payload address, shifted 24 (L0.DE.024).
pub const REL: u64 = 0x40;
/// The code of an unused entry slot (L0.DE.023).
pub const NONE: u64 = 0xFF;
/// Mailbox registers DST, ARG0..ARG3, GO (L0.DE.004).
pub const DST: u64 = 0;
pub const ARG0: u64 = 1;
pub const ARG1: u64 = 2;
pub const ARG2: u64 = 3;
pub const ARG3: u64 = 4;
pub const GO: u64 = 5;
/// The interlink's doorbell register, from the control region's 0xC0 window.
pub const IL_RING: u64 = 0x10;
/// A fetch request's DST: typed, MEM_RD_REQ (L0.STEP.044).
pub const TYPED_MEM_RD_REQ: u64 = 1 << 24;
/// The most words one fetch run carries: ARG3's 8-bit count.
pub const RUN_MAX: u64 = 255;

/// The entry code of mover register `reg` (L0.DE.004).
pub fn mover_code(reg: u64) -> u64 {
    if mutant::on("l0.lower/mover_code") {
        return 8 + reg;
    }
    8 + (reg >> 3)
}

/// The entry code of interlink register `reg` (L0.DE.005).
pub fn il_code(reg: u64) -> u64 {
    24 + (reg >> 3)
}

/// The interlink doorbell register a RING writes.
fn il_ring() -> u64 {
    if mutant::on("l0.lower/ring_code") {
        0x18
    } else {
        IL_RING
    }
}

/// The entry code a WAIT is lowered with (L0.DE.002).
pub(crate) fn wait_code() -> u64 {
    if mutant::on("l0.lower/wait_code") {
        31
    } else {
        WAIT
    }
}

/// The code flag a fetch run's ARG3 carries (L0.DE.024).
pub(crate) fn rel() -> u64 {
    if mutant::on("l0.lower/no_rel") {
        0
    } else {
        REL
    }
}

/// The WAIT source of the mover's done count.
pub(crate) fn src_mover(f: &Facts) -> u64 {
    if mutant::on("l0.lower/src_mover") {
        15
    } else {
        f.src_mover()
    }
}

/// A RING step's interlink write: mesh, slot, amount, fence (L0.STEP.031).
pub fn ring_value(s: &Step) -> u64 {
    let fence = if mutant::on("l0.lower/ring_value") {
        0
    } else {
        (s.arg >> 16 & 1) << 32
    };
    (s.unit & 3) | (s.count & 0xFF) << 8 | (s.arg & 0xFFFF) << 16 | fence
}

/// The entries of one package, cut into ENGINE steps.
struct Stream<'a> {
    pkg: &'a Package,
    f: &'a Facts,
    payloads: Vec<Word>,
    steps: Vec<Step>,
    entries: Vec<(u64, u64)>,
    sent: Vec<i128>,
    exp: Vec<i128>,
    gos: i128,
    mb: HashMap<u64, u64>,
}

impl<'a> Stream<'a> {
    fn new(pkg: &'a Package, f: &'a Facts) -> Stream<'a> {
        Stream {
            pkg,
            f,
            payloads: pkg.payloads.clone(),
            steps: Vec::new(),
            entries: Vec::new(),
            sent: vec![0; pkg.units.len()],
            exp: vec![0; pkg.units.len()],
            gos: 0,
            mb: HashMap::new(),
        }
    }

    fn put(&mut self, code: u64, value: u64) {
        self.entries.push((code, value));
    }

    fn payload(&self, i: u64) -> Result<Word> {
        match self.payloads.get(i as usize) {
            Some(w) => Ok(*w),
            None => refuse!(
                "L0.PKG.032",
                "a step reads payload {i} of {}",
                self.payloads.len()
            ),
        }
    }

    /// A WAIT on `src` for `want`, left out when `want` is not positive
    /// (L0.DE.006, L0.DE.030).
    fn wait(&mut self, src: u64, want: i128) {
        let code = wait_code();
        if want > 0 || mutant::on("l0.lower/wait_always") {
            self.put(code, src << 56 | (want as u64 & self.f.ctr_mask()));
        }
    }

    /// A mailbox write, left out when the register holds the value from an
    /// earlier entry of this ENGINE step (L0.DE.021).
    fn mailbox(&mut self, reg: u64, value: u64) {
        if self.mb.get(&reg) != Some(&value) || mutant::on("l0.lower/no_dedupe") {
            self.mb.insert(reg, value);
            self.put(reg, value);
        }
    }

    /// The entries so far as one ENGINE step (L0.DE.023).
    fn cut(&mut self) {
        if self.entries.is_empty() {
            return;
        }
        let first = self.payloads.len();
        for group in self.entries.chunks(3) {
            let mut word = Word::ZERO;
            let none = if mutant::on("l0.lower/pad_code") {
                0
            } else {
                NONE
            };
            for j in 0..3 {
                let (code, value) = group.get(j).copied().unwrap_or((none, 0));
                word = word
                    | Word::placed(code, 8 * j as u32)
                    | Word::placed(value, 64 * (j as u32 + 1));
            }
            self.payloads.push(word);
        }
        self.steps.push(Step::new(
            Op::Engine,
            0,
            self.entries.len() as u64,
            first as u64,
            0,
        ));
        self.entries.clear();
        if !mutant::on("l0.lower/dedupe_across_steps") {
            self.mb.clear();
        }
    }

    fn credit(&self, u: usize) -> i128 {
        match self.pkg.units[u].credit {
            CREDIT_UNBOUNDED => self.f.unbounded as i128,
            c => c as i128,
        }
    }

    /// `count` words from `at` by `u`'s fetch port: a payload offset under
    /// `rel` REL, an absolute address under 0 (L0.STEP.044, L0.STEP.045).
    fn fetch(&mut self, u: usize, mut at: u64, mut count: u64, rel: u64) {
        let unit = self.pkg.units[u];
        let depth = if mutant::on("l0.lower/fetch_depth") {
            256
        } else {
            self.f.inst_depth as i128
        };
        let room = self.credit(u).min(depth);
        let mut most = room.min(RUN_MAX as i128).max(1) as u64;
        if mutant::on("l0.lower/run_cap") {
            most = count.max(1);
        }
        let typed = if mutant::on("l0.lower/untyped_dst") {
            0
        } else {
            TYPED_MEM_RD_REQ
        };
        while count != 0 {
            let n = count.min(most);
            self.wait(u as u64, self.sent[u] + n as i128 - room);
            self.mailbox(DST, typed | (unit.fetch & 0xFFFF));
            self.mailbox(ARG0, 0);
            self.mailbox(ARG1, 0);
            self.mailbox(ARG2, ((unit.y << 4) | unit.x) << 40 | 1 << 30);
            self.mb.remove(&ARG3);
            self.put(ARG3 | rel, at << 24 | 0x60 << 8 | (n & 0xFF));
            self.put(GO, 1);
            self.sent[u] += n as i128;
            at = at.wrapping_add(n * PAYLOAD_BYTES as u64);
            count -= n;
        }
    }

    /// DISPATCH words: fetch runs from the package for a unit with a port
    /// (L0.DE.028), else one mailbox flit each (L0.DE.029).
    fn words(&mut self, u: usize, first: u64, count: u64) -> Result<()> {
        let unit = self.pkg.units[u];
        if unit.has_port() {
            self.fetch(u, first.wrapping_mul(PAYLOAD_BYTES as u64), count, rel());
            return Ok(());
        }
        for k in 0..count {
            let w = self.payload(first + k)?;
            self.wait(u as u64, self.sent[u] + 1 - self.credit(u));
            self.mailbox(DST, unit.y << 8 | unit.x);
            for j in 0..4 {
                self.mailbox(ARG0 + j, w.limb(64 * j as u32));
            }
            self.put(GO, 1);
            self.sent[u] += 1;
        }
        Ok(())
    }

    /// A REPEAT's words written out as payloads and sent as a DISPATCH
    /// (L0.DE.032).
    fn repeat(&mut self, s: &Step) -> Result<()> {
        let first = s.arg & 0xFFFF_FFFF;
        let repeats = (s.arg >> 32) & 0xFFFF;
        let nincs = s.arg >> 48;
        let mut incs = Vec::with_capacity(nincs as usize);
        for k in 0..nincs {
            let w = self.payload(first + s.count + k / 2)?;
            let at = 128 * (k % 2) as u32;
            let (head, delta) = (w.limb(at), w.limb(at + 64));
            incs.push((
                head & 0xFFFF_FFFF,
                ((head >> 32) & 0xFF) as u32,
                ((head >> 40) & 0xFF) as u32,
                delta,
            ));
        }
        let at = self.payloads.len() as u64;
        for r in 0..repeats {
            for j in 0..s.count {
                let mut w = self.payload(first + j)?;
                for &(index, bit, width, delta) in &incs {
                    if index == j {
                        let m = mask64(width);
                        let field = w.get(bit, width).wrapping_add(r.wrapping_mul(delta)) & m;
                        w.set(bit, width, field);
                    }
                }
                self.payloads.push(w);
            }
        }
        self.words(s.unit as usize, at, repeats * s.count)
    }

    /// WAITs for everything sent and awaited, and every move (L0.DE.030).
    fn barrier(&mut self) {
        if mutant::on("l0.lower/no_barrier") {
            return;
        }
        for u in 0..self.pkg.units.len() {
            let want = self.sent[u].max(self.exp[u]);
            self.wait(u as u64, want);
        }
        self.wait(src_mover(self.f), self.gos);
    }

    /// One entry per mover pair, then a WAIT on the moves unless posted
    /// (L0.DE.030, refusal L0.STEP.029).
    fn mover(&mut self, s: &Step) -> Result<()> {
        let gos = self.gos;
        self.mover_entries(s)?;
        if mutant::on("l0.lower/gos_ignored") {
            self.gos = gos;
        }
        Ok(())
    }

    fn mover_entries(&mut self, s: &Step) -> Result<()> {
        for k in 0..s.count {
            let w = self.payload(s.arg + k)?;
            for half in [0u32, 128] {
                let (reg, val) = (w.limb(half), w.limb(half + 64));
                if reg == MOVER_SKIP {
                    continue;
                }
                if reg >= self.f.mover_span || reg & 7 != 0 {
                    return refuse!("L0.STEP.029", "mover register {reg:#x} is not one");
                }
                self.put(mover_code(reg), val);
                if reg == 0 && val >> 16 & 1 != 0 {
                    self.gos += 1;
                }
            }
        }
        let posted = if mutant::on("l0.lower/posted_flag") {
            0
        } else {
            F_POSTED
        };
        if s.flags & posted == 0 {
            self.wait(src_mover(self.f), self.gos);
        }
        Ok(())
    }
}

/// `pkg` for a node with a dispatch engine (L0.DE.022). A package that jumps
/// or patches comes back as it is (L0.DE.033). Refuses one with relocations,
/// more units than the engine counts, or an ENGINE step (L0.DE.039).
pub fn lower(pkg: &Package, f: &Facts) -> Result<Package> {
    if pkg.steps.iter().any(|s| s.op == Op::Await && s.flags != 0) {
        return Ok(pkg.clone());
    }
    let mut src = pkg.clone();
    if mutant::on("l0.lower/await_absolute") {
        let mut prev: HashMap<u64, u64> = HashMap::new();
        for s in src.steps.iter_mut().filter(|s| s.op == Op::Await) {
            let p = prev.get(&s.unit).copied().unwrap_or(0);
            prev.insert(s.unit, p.max(s.count));
            s.count = s.count.saturating_sub(p);
        }
    }
    if mutant::on("l0.lower/jump_lowered") {
        src.steps.retain(|s| !matches!(s.op, Op::Jump | Op::Patch));
    }
    if mutant::on("l0.lower/steps_dropped") {
        src.steps.retain(|s| {
            matches!(
                s.op,
                Op::Dispatch
                    | Op::Repeat
                    | Op::Await
                    | Op::Barrier
                    | Op::Mover
                    | Op::Mwait
                    | Op::Fetch
                    | Op::Ring
                    | Op::End
                    | Op::Jump
                    | Op::Patch
            )
        });
    }
    let mut out = lower_steps(&src, f)?;
    if mutant::on("l0.lower/meta_lost") {
        out.signature = 0;
        out.ack_reserve = 0;
        out.checksum = false;
        for u in &mut out.units {
            u.credit = 0;
        }
    }
    Ok(out)
}

fn lower_steps(pkg: &Package, f: &Facts) -> Result<Package> {
    if pkg
        .steps
        .iter()
        .any(|s| matches!(s.op, Op::Jump | Op::Patch))
    {
        return Ok(pkg.clone());
    }
    if !pkg.relocs.is_empty() {
        return refuse!(
            "L0.DE.039",
            "an engine package is bound: it has no relocations"
        );
    }
    if pkg.units.len() > f.engine_units {
        return refuse!(
            "L0.DE.039",
            "{} units; the engine counts {}",
            pkg.units.len(),
            f.engine_units
        );
    }
    let low = lowered(pkg, f, false)?;
    if !rings_overflow(&low, f) || mutant::on("l0.lower/rings_in_engine") {
        return Ok(low);
    }
    lowered(pkg, f, true)
}

/// `pkg`'s steps lowered; with `rings_kept` every RING stays a step.
fn lowered(pkg: &Package, f: &Facts, rings_kept: bool) -> Result<Package> {
    let mut st = Stream::new(pkg, f);
    for (i, s) in pkg.steps.iter().enumerate() {
        let names_unit = matches!(s.op, Op::Dispatch | Op::Repeat | Op::Await | Op::Fetch);
        if names_unit && s.unit >= pkg.units.len() as u64 {
            return refuse!(
                "L0.PKG.032",
                "step {i} names unit {} of {}",
                s.unit,
                pkg.units.len()
            );
        }
        match s.op {
            Op::End => break,
            Op::Dispatch => st.words(s.unit as usize, s.arg, s.count)?,
            Op::Repeat => st.repeat(s)?,
            Op::Await => {
                let u = s.unit as usize;
                st.exp[u] += s.count as i128;
                st.wait(s.unit, st.exp[u]);
            }
            Op::Barrier => st.barrier(),
            Op::Mover => st.mover(s)?,
            Op::Mwait => st.wait(src_mover(f), s.count as i128),
            Op::Fetch => st.fetch(s.unit as usize, s.arg, s.count, 0),
            Op::Ring if !rings_kept => st.put(il_code(il_ring()), ring_value(s)),
            Op::Engine => return refuse!("L0.DE.039", "this package is lowered already"),
            _ => {
                st.cut();
                st.steps.push(*s);
            }
        }
    }
    st.barrier();
    st.cut();
    Ok(Package {
        steps: st.steps,
        relocs: Vec::new(),
        payloads: st.payloads,
        ..pkg.clone()
    })
}

/// Whether lowered `low` has more than `node.ring_depth` RING entries
/// between waits (WAIT entries, or a WAIT_BELL / WAIT_SLOT step). An engine
/// RING has no room check (R-046) and the firmware's count misses engine
/// rings, so such a package keeps every RING a step, each then waiting for
/// room on the firmware path (RUL.046, L0.STEP.032).
fn rings_overflow(low: &Package, f: &Facts) -> bool {
    let ring = il_code(IL_RING);
    let mut queued = 0;
    for s in &low.steps {
        match s.op {
            Op::Engine => {
                for (code, _) in entries(low, s) {
                    if code == WAIT {
                        queued = 0;
                    } else if code == ring {
                        queued += 1;
                        if queued > f.ring_depth {
                            return true;
                        }
                    }
                }
            }
            Op::WaitBell | Op::WaitSlot => queued = 0,
            _ => {}
        }
    }
    false
}

/// One engine entry for [`stream`]: a register write, or a WAIT on a source
/// (left out when the count is not positive, L0.DE.006).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Entry {
    Put(u64, u64),
    Wait(u64, i128),
}

/// `pkg` with its steps replaced by one ENGINE step of `entries` (L0.DE
/// codes), their payloads appended to the package's own.
pub fn stream(pkg: &Package, f: &Facts, entries: &[Entry]) -> Package {
    let mut st = Stream::new(pkg, f);
    for e in entries {
        match *e {
            Entry::Put(code, value) => st.put(code, value),
            Entry::Wait(src, want) => st.wait(src, want),
        }
    }
    st.cut();
    Package {
        steps: st.steps,
        payloads: st.payloads,
        ..pkg.clone()
    }
}

/// An ENGINE step's `(code, value)` entries, as the node reads them.
pub fn entries(pkg: &Package, step: &Step) -> Vec<(u64, u64)> {
    (0..step.count)
        .map(|k| {
            let w = pkg.payloads[(step.arg + k / 3) as usize];
            let j = (k % 3) as u32;
            (w.get(8 * j, 8), w.limb(64 * (j + 1)))
        })
        .collect()
}
