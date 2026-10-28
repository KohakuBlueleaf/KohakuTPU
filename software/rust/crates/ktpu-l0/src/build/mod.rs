//! Building a package step by step: the builder owns the unit, buffer and
//! payload tables and the step list; an address field inside a live span
//! becomes a relocation (L0.PKG.018, L0.BIND.002-L0.BIND.008).

pub mod fields;
pub mod periodic;

use std::collections::{BTreeMap, HashMap};

use crate::error::Result;
use crate::facts::{Coord, Facts};
use crate::format::{
    patch_arg, type_code, type_name, Alu, Buffer, Cond, Kind, Op, Package, Reloc, Step, Unit,
    F_ACK, F_CONSUME, F_CTRL, F_IMM, F_INDEXED, F_MEM, F_POSTED, F_REG, MOVER_SKIP,
};
use crate::mutant;
use crate::refuse;
use crate::word::{Word, WordOps};

pub use periodic::Increment;

/// `(bit, width, shift)`: payload bits `[bit, bit+width)` hold `address >> shift`.
pub type Segment = (u32, u32, u32);

/// One address field of a word: its segments (a split field has two) and the
/// address they hold.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AddrField {
    pub segments: Vec<Segment>,
    pub value: u64,
}

/// A project's report on one payload of one unit type: every address field
/// and its value (`isa.fields.addresses`; the mover's is `mover::addresses`).
pub trait Addresses {
    fn fields(&self, word: &Word, unit_type: &str) -> Vec<AddrField>;
}

impl<F: Fn(&Word, &str) -> Vec<AddrField>> Addresses for F {
    fn fields(&self, word: &Word, unit_type: &str) -> Vec<AddrField> {
        self(word, unit_type)
    }
}

/// Buffers a relocation can name (its 8-bit buffer field, L0.BIND.005).
pub const RELOC_BUFFERS: usize = 256;

/// `(first, second)` 64-bit pairs, two to a payload, an odd one padded with a
/// skipped pair (L0.STEP.047, L0.STEP.063).
pub fn pairs(pairs: &[(u64, u64)]) -> Vec<Word> {
    let pad = if mutant::on("l0.build/pairs_unpadded") {
        (0, 0)
    } else {
        (MOVER_SKIP, 0)
    };
    padded(pairs, pad)
}

fn padded(pairs: &[(u64, u64)], pad: (u64, u64)) -> Vec<Word> {
    let mut ps = pairs.to_vec();
    if ps.len() % 2 == 1 {
        ps.push(pad);
    }
    ps.chunks(2)
        .map(|c| Word([c[0].0, c[0].1, c[1].0, c[1].1]))
        .collect()
}

/// Every `(bit, width)` address segment `addresses` reports in `words`, sorted.
pub fn address_fields(
    words: &[Word],
    addresses: Option<&dyn Addresses>,
    unit_type: &str,
) -> Vec<(u32, u32)> {
    let Some(a) = addresses else {
        return Vec::new();
    };
    let mut out: Vec<(u32, u32)> = words
        .iter()
        .flat_map(|w| a.fields(w, unit_type))
        .flat_map(|f| f.segments.into_iter().map(|(b, w, _)| (b, w)))
        .collect();
    out.sort_unstable();
    out.dedup();
    out
}

/// The 64-bit address fields `_addressed` reports: each half not `MOVER_SKIP`.
struct Halves(Vec<AddrField>);

impl Addresses for Halves {
    fn fields(&self, _: &Word, _: &str) -> Vec<AddrField> {
        if mutant::on("l0.build/drop-address64") {
            Vec::new()
        } else {
            self.0.clone()
        }
    }
}

/// Accumulates units, buffers, payloads and steps into one [`Package`].
///
/// `types` maps a coordinate to its unit type name ("MG"), so a completion
/// from a unit the package only AWAITs is still attributed. `credit` is the
/// in-flight bound a unit is given. `fetch` is the memory port every unit's
/// DISPATCH words stream from.
#[derive(Debug, Clone)]
pub struct PackageBuilder {
    pub types: BTreeMap<Coord, String>,
    pub credit: u64,
    pub fetch: Option<Coord>,
    pub signature: u64,
    pub mesh: u64,
    pub units: Vec<Unit>,
    pub buffers: Vec<Buffer>,
    pub payloads: Vec<Word>,
    pub relocs: Vec<Reloc>,
    pub steps: Vec<Step>,
    pub ack_reserve: u64,
    /// Live spans `(base, size)` the address fields are matched against,
    /// sorted by base.
    pub spans: Vec<(u64, u64)>,
    regs: u64,
    il_slots: u64,
    buf: HashMap<String, usize>,
    span_buf: HashMap<u64, usize>,
    unit_at: HashMap<(u64, u64, u64), usize>,
}

impl PackageBuilder {
    pub fn new(f: &Facts, types: BTreeMap<Coord, String>, credit: u64) -> PackageBuilder {
        PackageBuilder {
            types,
            credit,
            fetch: None,
            signature: mutant::on("l0.build/signature_default") as u64,
            mesh: 0,
            units: Vec::new(),
            buffers: Vec::new(),
            payloads: Vec::new(),
            relocs: Vec::new(),
            steps: Vec::new(),
            ack_reserve: 0,
            spans: Vec::new(),
            regs: f.regs,
            il_slots: f.il_slots,
            buf: HashMap::new(),
            span_buf: HashMap::new(),
            unit_at: HashMap::new(),
        }
    }

    // ------------------------------------------------------------- tables
    /// The index of the unit at `coord`, added on first use, keyed by
    /// `(mesh, x, y)` (L0.PKG.018). Refuses a coordinate with no known type
    /// (L0.PKG.034).
    pub fn unit(
        &mut self,
        coord: Coord,
        type_name: Option<&str>,
        mesh: Option<u64>,
    ) -> Result<usize> {
        let m = mesh.unwrap_or(self.mesh);
        let key = (m, coord.0 as u64, coord.1 as u64);
        if let Some(&got) = self.unit_at.get(&key) {
            if !mutant::on("l0.build/unit_per_use") {
                return Ok(got);
            }
        }
        let name = match type_name.filter(|n| !n.is_empty()) {
            Some(n) => n.to_string(),
            None => match self.types.get(&coord) {
                Some(n) => n.clone(),
                None => return refuse!("L0.PKG.034", "no unit type is known for {coord:?}"),
            },
        };
        let port = self.fetch.map_or(0, |(x, y)| {
            if mutant::on("l0.build/port_swapped") {
                1 << 16 | (x as u64) << 8 | y as u64
            } else {
                1 << 16 | (y as u64) << 8 | x as u64
            }
        });
        self.units.push(Unit {
            type_code: type_code(&name)?,
            x: coord.0 as u64,
            y: coord.1 as u64,
            mesh: m,
            credit: self.credit,
            fetch: port,
        });
        self.unit_at.insert(key, self.units.len() - 1);
        Ok(self.units.len() - 1)
    }

    /// The index of buffer `name`, added on first use.
    pub fn buffer(&mut self, name: &str, size: u64, default: u64, kind: Kind) -> usize {
        if let Some(&got) = self.buf.get(name) {
            return got;
        }
        self.buffers.push(Buffer {
            name: name.to_string(),
            size,
            default,
            kind,
            mesh: self.mesh,
        });
        self.buf.insert(name.to_string(), self.buffers.len() - 1);
        self.buffers.len() - 1
    }

    /// Make every `{base: size}` span a candidate buffer for address fields; a
    /// span becomes a buffer, in first-reached order, when one lands in it.
    pub fn spans_from(&mut self, live: &BTreeMap<u64, u64>) {
        self.spans = live.iter().map(|(&b, &s)| (b, s)).collect();
    }

    fn span_of(&self, value: u64) -> Option<(u64, u64)> {
        let lo = self.spans.partition_point(|&(base, _)| base <= value);
        if lo > 0 {
            let (base, size) = self.spans[lo - 1];
            if (value as u128) < base as u128 + size as u128 {
                return Some((base, size));
            }
        }
        None
    }

    // ----------------------------------------------------------- payloads
    /// Append one word; every address field `addresses` reports that lands in
    /// a known span becomes a relocation, its segments cleared (L0.BIND.002).
    pub fn payload(
        &mut self,
        word: Word,
        addresses: Option<&dyn Addresses>,
        unit_type: &str,
    ) -> usize {
        let idx = self.payloads.len();
        let mut word = word;
        let fields = addresses.map_or_else(Vec::new, |a| a.fields(&word, unit_type));
        for f in fields {
            let Some((base, size)) = self.span_of(f.value) else {
                continue;
            };
            let b = match self.span_buf.get(&base) {
                Some(&b) => b,
                None => {
                    if self.buffers.len() >= RELOC_BUFFERS {
                        continue;
                    }
                    let name = format!("buf{}", self.buffers.len());
                    let b = self.buffer(&name, size, base, Kind::Temp);
                    self.span_buf.insert(base, b);
                    b
                }
            };
            for &(bit, width, shift) in &f.segments {
                self.relocs.push(Reloc {
                    payload: idx as u64,
                    bit: bit as u64,
                    width: width as u64,
                    buffer: b as u64,
                    addend: f.value as i128 - base as i128,
                    shift: shift as u64,
                });
                word = word.cleared(bit, width);
            }
        }
        self.payloads.push(word);
        idx
    }

    fn unit_kind(&self, unit: usize) -> Result<String> {
        match self.units.get(unit) {
            Some(u) => Ok(type_name(u.type_code)),
            None => refuse!("L0.PKG.032", "unit {unit} of {}", self.units.len()),
        }
    }

    // -------------------------------------------------------------- steps
    /// DISPATCH `words` to `unit` (L0.STEP.014); nothing for no words.
    pub fn dispatch(
        &mut self,
        unit: usize,
        words: &[Word],
        addresses: Option<&dyn Addresses>,
    ) -> Result<()> {
        let first = self.payloads.len();
        let kind = self.unit_kind(unit)?;
        for w in words {
            self.payload(*w, addresses, &kind);
        }
        if self.payloads.len() > first {
            self.steps.push(Step::new(
                Op::Dispatch,
                unit as u64,
                (self.payloads.len() - first) as u64,
                first as u64,
                0,
            ));
        }
        Ok(())
    }

    /// `template` sent `repeats` times; repetition r adds r * delta to each
    /// increment's field (L0.STEP.039, refusals L0.STEP.041).
    pub fn repeat(
        &mut self,
        unit: usize,
        template: &[Word],
        repeats: u64,
        increments: &[Increment],
        addresses: Option<&dyn Addresses>,
    ) -> Result<()> {
        let mut incs = increments.to_vec();
        incs.sort_unstable();
        if template.is_empty() || !(1..=0xFFFF).contains(&repeats) || incs.len() > 0xFFFF {
            return refuse!(
                "L0.STEP.041",
                "a REPEAT of {} words x {repeats}",
                template.len()
            );
        }
        for &(index, bit, width, delta) in &incs {
            if !(1..=64).contains(&width) || bit + width > 256 {
                return refuse!(
                    "L0.STEP.041",
                    "increment field [{bit}, +{width}) leaves the word"
                );
            }
            let Some(w) = template.get(index) else {
                return refuse!(
                    "L0.STEP.041",
                    "increment word {index} of {}",
                    template.len()
                );
            };
            let field = w.get(bit, width) as u128;
            if field + (repeats as u128 - 1) * delta as u128 >= 1u128 << width {
                return refuse!(
                    "L0.STEP.041",
                    "word {index}'s field [{bit}, +{width}) wraps within {repeats} repetitions of {delta}"
                );
            }
        }
        let first = self.payloads.len();
        let kind = self.unit_kind(unit)?;
        for w in template {
            self.payload(*w, addresses, &kind);
        }
        for pair in incs.chunks(2) {
            let mut word = Word::ZERO;
            for (half, &(index, bit, width, delta)) in pair.iter().enumerate() {
                let at = 128 * half as u32;
                word = word
                    | Word::placed(index as u64 | (bit as u64) << 32 | (width as u64) << 40, at)
                    | Word::placed(delta, at + 64);
            }
            self.payloads.push(word);
        }
        let arg = first as u64 | repeats << 32 | (incs.len() as u64) << 48;
        self.steps.push(Step::new(
            Op::Repeat,
            unit as u64,
            template.len() as u64,
            arg,
            0,
        ));
        Ok(())
    }

    /// `words` as DISPATCH, REPEAT, DISPATCH: the longest run that repeats
    /// with only `fields` stepping becomes one REPEAT.
    pub fn dispatch_periodic(
        &mut self,
        unit: usize,
        words: &[Word],
        fields: &[(u32, u32)],
        addresses: Option<&dyn Addresses>,
    ) -> Result<()> {
        let Some((head, period, repeats, incs)) = periodic::periodic(words, fields) else {
            return self.dispatch(unit, words, addresses);
        };
        self.dispatch(unit, &words[..head], addresses)?;
        self.repeat(
            unit,
            &words[head..head + period],
            repeats as u64,
            &incs,
            addresses,
        )?;
        self.dispatch(unit, &words[head + period * repeats..], addresses)
    }

    /// Wait for this unit's runtime sent frontier, without waiting for other units.
    pub fn await_sent(&mut self, unit: usize) {
        self.steps
            .push(Step::new(Op::Await, unit as u64, 0, 0, F_IMM));
    }

    /// AWAIT `count` more completions from `unit` (L0.STEP.020); none for 0.
    pub fn await_(&mut self, unit: usize, count: u64) {
        if count != 0 {
            let count = count + mutant::on("l0.build/await_plus_one") as u64;
            self.steps
                .push(Step::new(Op::Await, unit as u64, count, 0, 0));
        }
    }

    /// A BARRIER, never right after another (L0.STEP.023).
    pub fn barrier(&mut self) {
        if mutant::on("l0.build/double_barrier")
            || self.steps.last().is_some_and(|s| s.op != Op::Barrier)
        {
            self.steps.push(Step::bare(Op::Barrier));
        }
    }

    /// Reject a completed computation whose device-side status is nonzero.
    pub fn assert_zero(&mut self, reg: u64, code: u64) -> Result<()> {
        if reg >= self.regs || code > u32::MAX as u64 {
            return refuse!("L0.STEP.066", "ASSERT_ZERO register/code out of range");
        }
        self.steps.push(Step::new(Op::AssertZero, reg, code, 0, 0));
        Ok(())
    }

    pub fn signal(&mut self, value32: u64, value64: u64) {
        self.steps
            .push(Step::new(Op::Signal, 0, value32, value64, 0));
    }

    pub fn settle(&mut self, cycles: u64) {
        self.steps.push(Step::new(Op::Settle, 0, cycles, 0, 0));
    }

    /// Ring `mesh`'s signal slot `slot` by `amount` (L0.STEP.031, L0.STEP.033;
    /// the lowering bounds rings between waits, RUL.046).
    pub fn ring(&mut self, mesh: u64, slot: u64, amount: u64, fence: bool) -> Result<()> {
        if slot >= 256 || !(1..1 << 16).contains(&amount) {
            return refuse!("L0.STEP.033", "ring slot {slot} amount {amount}");
        }
        self.steps.push(Step::new(
            Op::Ring,
            mesh,
            slot,
            amount & 0xFFFF | (fence as u64) << 16,
            0,
        ));
        Ok(())
    }

    pub fn wait_bell(&mut self, mesh: u64, count: u64) {
        self.steps.push(Step::new(Op::WaitBell, mesh, count, 0, 0));
    }

    /// Wait until signal slot `slot` holds `count`; `consume` takes it
    /// (L0.STEP.035; slots are `il.slots`).
    pub fn wait_slot(&mut self, slot: u64, count: u64, consume: bool) -> Result<()> {
        if slot >= self.il_slots {
            return refuse!(
                "L0.STEP.066",
                "signal slot {slot} is not 0..{}",
                self.il_slots - 1
            );
        }
        let flags = if consume { F_CONSUME } else { 0 };
        self.steps
            .push(Step::new(Op::WaitSlot, slot, count, 0, flags));
        Ok(())
    }

    /// Framework memory fields use the same span rule as project fields.
    /// Control offsets have no memory fields. Returns the first payload index.
    fn addressed(&mut self, words: &[Word], relocate: bool, halves: &[u32]) -> usize {
        let first = self.payloads.len();
        for w in words {
            let fields: Vec<AddrField> = halves
                .iter()
                .map(|&h| (h, w.limb(h)))
                .filter(|&(_, v)| v != MOVER_SKIP)
                .map(|(h, v)| AddrField {
                    segments: vec![(h, 64, 0)],
                    value: v,
                })
                .collect();
            let fns = Halves(fields);
            self.payload(*w, relocate.then_some(&fns as &dyn Addresses), "");
        }
        first
    }

    /// 64-bit stores `(address, value)` in order (L0.STEP.047); with
    /// `from_regs` each value names a register.
    pub fn store(&mut self, ps: &[(u64, u64)], from_regs: bool, relocate: bool) {
        let words = pairs(ps);
        let first = self.addressed(&words, relocate, &[0, 128]);
        let flags = if from_regs { F_REG } else { 0 };
        self.steps.push(Step::new(
            Op::Store,
            0,
            words.len() as u64,
            first as u64,
            flags,
        ));
    }

    /// Control-region writes `(offset, value)` (L0.STEP.050).
    pub fn ctrl(&mut self, ps: &[(u64, u64)], from_regs: bool) {
        let words = pairs(ps);
        let first = self.payloads.len();
        self.payloads.extend_from_slice(&words);
        let flags = if from_regs { F_REG } else { 0 };
        self.steps.push(Step::new(
            Op::Ctrl,
            0,
            words.len() as u64,
            first as u64,
            flags,
        ));
    }

    /// Register := a relocated payload address, without dereferencing device memory.
    pub fn address(&mut self, reg: u64, address: u64) {
        let first = self.addressed(&[Word::from_u64(address)], true, &[0]);
        self.steps
            .push(Step::new(Op::Load, reg, 0, first as u64, F_IMM));
    }

    /// Register `reg` := the 64 bits at `address` (+ register `index`), or the
    /// control register at offset `address` with `ctrl` (L0.STEP.049).
    pub fn load(&mut self, reg: u64, address: u64, index: Option<u64>, ctrl: bool, relocate: bool) {
        let flags = if index.is_some() { F_INDEXED } else { 0 } | if ctrl { F_CTRL } else { 0 };
        let first = self.addressed(
            &[Word::from_u64(address)],
            relocate,
            if ctrl { &[] } else { &[0] },
        );
        self.steps.push(Step::new(
            Op::Load,
            reg,
            index.unwrap_or(0),
            first as u64,
            flags,
        ));
    }

    /// rd := ra OP b, `b` an immediate (two's complement) or, with
    /// `imm = false`, a register (L0.STEP.051).
    pub fn alu(&mut self, rd: u64, op: Alu, ra: u64, b: u64, imm: bool) -> Result<()> {
        if rd >= self.regs || ra >= self.regs || (!imm && b >= self.regs) {
            return refuse!("L0.STEP.070", "alu r{rd} = r{ra} {op:?} {b}");
        }
        let count = op as u64 | ra << 8 | if imm { 0 } else { b << 12 };
        let (arg, flags) = if imm { (b, F_IMM) } else { (0, 0) };
        self.steps.push(Step::new(Op::Alu, rd, count, arg, flags));
        Ok(())
    }

    /// Go to step `target` when ra COND b; returns this step's index (a
    /// forward target is set with [`retarget`](Self::retarget)).
    pub fn jump(&mut self, target: u64, cond: Cond, ra: u64, b: u64, imm: bool) -> usize {
        let count = cond as u64 | ra << 8 | if imm { 0 } else { b << 12 };
        let (arg, flags) = if imm { (b, F_IMM) } else { (0, 0) };
        self.steps
            .push(Step::new(Op::Jump, target, count, arg, flags));
        self.steps.len() - 1
    }

    /// Every step-patching PATCH from `start` on patches step `index`.
    pub fn repoint(&mut self, start: usize, index: u64) {
        for s in self.steps.iter_mut().skip(start) {
            if s.op == Op::Patch && s.arg >> 24 & 1 != 0 {
                s.count = index;
            }
        }
    }

    pub fn retarget(&mut self, step: usize, target: u64) {
        self.steps[step].unit = target;
    }

    /// Payload (or step) `index`'s bits `[bit, bit+width)` := register `reg`
    /// >> `shift`, or += it with `add` (L0.STEP.053).
    #[allow(clippy::too_many_arguments)]
    pub fn patch(
        &mut self,
        reg: u64,
        index: u64,
        bit: u64,
        width: u64,
        shift: u64,
        steps: bool,
        add: bool,
    ) -> Result<()> {
        let arg = patch_arg(bit, width, shift, steps, add)?;
        self.steps.push(Step::new(Op::Patch, reg, index, arg, 0));
        Ok(())
    }

    /// Raw flits of `flit_type` to `unit`, one a word, outside credit and
    /// completions (L0.STEP.055).
    pub fn send(&mut self, unit: usize, flit_type: u64, words: &[Word], ack: bool) {
        let first = self.payloads.len() as u64;
        self.payloads.extend_from_slice(words);
        let flags = if ack { F_ACK } else { 0 };
        self.steps.push(Step::new(
            Op::Send,
            unit as u64,
            words.len() as u64,
            first | flit_type << 32,
            flags,
        ));
    }

    /// A CU_DATA burst to `unit`: `descriptor`, then `words` 32-byte words the
    /// node reads from `address` (L0.STEP.055, `MEM`).
    pub fn push(
        &mut self,
        unit: usize,
        descriptor: Word,
        address: u64,
        words: u64,
        ack: bool,
        relocate: bool,
    ) {
        let first = self.payloads.len() as u64;
        self.payloads.push(descriptor);
        self.addressed(&[Word::from_u64(address)], relocate, &[0]);
        let flags = F_MEM | if ack { F_ACK } else { 0 };
        self.steps.push(Step::new(
            Op::Send,
            unit as u64,
            words,
            first | 8 << 32,
            flags,
        ));
    }

    /// `count` flits from the node's RX queue (from `unit`, or any with
    /// `ANY_UNIT`), each one's payload to `address + 32 i` (L0.STEP.056).
    pub fn recv(&mut self, address: u64, count: u64, unit: u64, relocate: bool) {
        let first = self.addressed(&[Word::from_u64(address)], relocate, &[0]);
        self.steps
            .push(Step::new(Op::Recv, unit, count, first as u64, 0));
    }

    /// Mover register writes `(reg, value)` in order, two pairs a payload; the
    /// step waits for every move a GO in them starts unless `posted`
    /// (L0.STEP.024-L0.STEP.026).
    pub fn mover(
        &mut self,
        writes: &[(u64, u64)],
        addresses: Option<&dyn Addresses>,
        posted: bool,
    ) {
        let first = self.payloads.len();
        let swapped: Vec<(u64, u64)>;
        let writes = if mutant::on("l0.build/mover_swapped") {
            swapped = writes.iter().map(|&(r, v)| (v, r)).collect();
            &swapped[..]
        } else {
            writes
        };
        for w in padded(writes, (MOVER_SKIP, 0)) {
            self.payload(w, addresses, "mover");
        }
        let posted = posted && !mutant::on("l0.build/never_posted");
        let flags = if posted { F_POSTED } else { 0 };
        self.steps.push(Step::new(
            Op::Mover,
            0,
            (self.payloads.len() - first) as u64,
            first as u64,
            flags,
        ));
    }

    /// `count` words for `unit` read by its fetch port from `addr`, an
    /// absolute address (L0.STEP.046).
    pub fn fetch_from(&mut self, unit: usize, addr: u64, count: u64) -> Result<()> {
        if !self.units.get(unit).is_some_and(|u| u.has_port()) {
            return refuse!(
                "L0.STEP.046",
                "unit {unit} has no fetch port to stream from"
            );
        }
        if count != 0 {
            let (arg, flags) = if self.span_of(addr).is_some() {
                let first = self.addressed(&[Word::from_u64(addr)], true, &[0]);
                (first as u64, F_MEM)
            } else {
                (addr, 0)
            };
            self.steps
                .push(Step::new(Op::Fetch, unit as u64, count, arg, flags));
        }
        Ok(())
    }

    /// Wait until `moves` of the package's moves are done (L0.STEP.027).
    pub fn mwait(&mut self, moves: u64) {
        self.steps.push(Step::new(Op::Mwait, 0, moves, 0, 0));
    }

    /// Keep `count` mailbox entries free for acknowledgements (L0.STEP.011).
    pub fn reserve_acks(&mut self, count: u64) {
        self.ack_reserve = self.ack_reserve.max(count);
    }

    // -------------------------------------------------------------- build
    /// The package. `defaults = false` writes every buffer's default address
    /// as 0, so its bytes name no address (L0.BIND.032).
    pub fn build(&self, defaults: bool, checksum: bool) -> Package {
        let buffers = self
            .buffers
            .iter()
            .map(|b| Buffer {
                default: if defaults { b.default } else { 0 },
                ..b.clone()
            })
            .collect();
        let mut steps = self.steps.clone();
        for s in &mut steps {
            if mutant::on("l0.build/flags_dropped") {
                s.flags = 0;
            }
            if mutant::on("l0.build/count_arg_swapped") {
                (s.unit, s.count) = (s.count & 0xFFFF, s.unit);
            }
        }
        Package {
            units: self.units.clone(),
            buffers,
            steps,
            relocs: self.relocs.clone(),
            payloads: self.payloads.clone(),
            signature: self.signature,
            ack_reserve: self.ack_reserve,
            checksum,
            buffer_names: self.buffers.iter().map(|b| b.name.clone()).collect(),
        }
    }

    /// Each buffer's recorded address, in buffer order (L0.BIND.006).
    pub fn bindings(&self) -> Vec<u64> {
        let mut out: Vec<u64> = self.buffers.iter().map(|b| b.default).collect();
        if mutant::on("l0.build/bindings_reversed") {
            out.reverse();
        }
        out
    }

    pub fn len(&self) -> usize {
        self.steps.len()
    }

    pub fn is_empty(&self) -> bool {
        self.steps.is_empty()
    }
}
