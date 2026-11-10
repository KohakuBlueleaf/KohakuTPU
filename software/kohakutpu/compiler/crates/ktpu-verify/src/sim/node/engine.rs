//! CPU enqueue and dispatch-engine execution have separate causal frontiers.

use std::collections::VecDeque;

use ktpu_l0::engine::{ARG3, GO, REL, WAIT};

use super::*;

#[derive(Clone, PartialEq, Eq)]
struct Entry {
    code: u64,
    value: u64,
    run: usize,
    issued: Clock,
}

#[derive(Clone, PartialEq, Eq)]
pub(super) struct Engine {
    pub proc: usize,
    pub step: Option<Step>,
    drained: bool,
    cursor: u32,
    codes: u64,
    enqueued: usize,
    observed: Vec<i64>,
    queue: VecDeque<Entry>,
    slots: VecDeque<Clock>,
    depth: usize,
    mailbox: [u64; 5],
    relative: Option<usize>,
    base: Vec<i64>,
    mask: u64,
    mover_source: usize,
}

impl Engine {
    pub(super) fn retained(&self, stats: &mut crate::sim::stats::Retained) {
        stats.add(format!("engine_queue/{}", self.proc), self.queue.len());
        stats.add(format!("engine_slots/{}", self.proc), self.slots.len());
        for entry in &self.queue {
            stats.clock(&entry.issued);
        }
        for clock in &self.slots {
            stats.clock(clock);
        }
    }

    pub(super) fn new(proc: usize, units: usize, machine: &ktpu_machine::Machine) -> Self {
        let facts = ktpu_l0::Facts::of(machine);
        Self {
            proc,
            step: None,
            drained: true,
            cursor: 0,
            codes: 0,
            enqueued: 0,
            observed: vec![0; facts.engine_units + 1],
            queue: VecDeque::new(),
            slots: VecDeque::new(),
            depth: machine.node.engine.depth as usize,
            mailbox: [0; 5],
            relative: None,
            base: vec![0; units],
            mask: facts.ctr_mask(),
            mover_source: facts.src_mover() as usize,
        }
    }

    pub(super) fn rebind(&mut self, sent: &[i64]) {
        // END does not clear mailbox/REL/mover configuration. Preserve those
        // fields across parts; only the stage-local base index table changes.
        self.base.resize(sent.len(), 0);
        if self.relative.is_some() {
            // Keep the fact that this mailbox names an old package. A new REL
            // descriptor replaces it; using it first is conservatively refused
            // rather than silently retargeted at the replacement package.
            self.relative = Some(usize::MAX);
        }
        self.end(sent);
    }

    pub(super) fn end(&mut self, sent: &[i64]) {
        self.base.copy_from_slice(sent);
        self.slots.clear();
        self.enqueued = 0;
        self.observed.fill(0);
    }

    pub(super) fn pending(&self) -> bool {
        !self.queue.is_empty()
    }
}

impl Sim<'_> {
    /// Firmware drains acquire each nonempty engine batch once.
    pub(super) fn engine_drain(&mut self, i: usize) -> bool {
        if self.nodes[i].engine.pending() {
            self.nodes[i].why = "firmware waits for queued engine entries".into();
            return false;
        }
        if !self.nodes[i].engine.drained {
            let issued = self.clock_of(self.nodes[i].engine.proc);
            let cpu = self.nodes[i].proc;
            self.tick(cpu, &[issued]);
            self.nodes[i].engine.drained = true;
        }
        true
    }

    pub(super) fn node_engine(&mut self, i: usize, s: Step) -> StepOutcome {
        self.nodes[i].engine.step = Some(s);
        match self.engine_progress(i, s) {
            Ok(progress) => progress.into(),
            Err(why) => {
                self.find(Kind::Model, self.nodes[i].name.clone(), why);
                StepOutcome::Terminal
            }
        }
    }

    fn engine_progress(&mut self, i: usize, s: Step) -> Result<bool, String> {
        let mut progress = false;
        let nd = &self.nodes[i];
        let run = nd
            .step_sources
            .get(&nd.at)
            .and_then(|sources| sources.first())
            .map(|&(run, _)| run)
            .ok_or("ENGINE has no package origin")?;
        let first = (s.arg as usize)
            .checked_sub(nd.payload_bases[run])
            .ok_or("ENGINE entry before package")?;
        let available = nd.pkgs[run].payloads.len();
        if first > available || (s.count as usize).div_ceil(3) > available - first {
            return Err("ENGINE entry stream outside package".into());
        }
        if nd.engine.cursor < s.count && nd.engine.queue.len() < nd.engine.depth {
            let entry = nd.engine.cursor;
            let index = s.arg as usize + (entry / 3) as usize;
            let location = nd.locations[index].clone();
            let initial = nd.payloads[index];
            let p = nd.proc;
            // Reusing a FIFO slot observes the entry that released that slot,
            // not arbitrary completion signals or the current engine frontier.
            let released = if self.nodes[i].engine.enqueued >= self.nodes[i].engine.depth {
                self.nodes[i].engine.slots.pop_front().into_iter().collect()
            } else {
                Vec::new()
            };
            let clock = self.tick(p, &[]);
            let slot = entry % 3;
            let codes = location.clone();
            let value_at = location.offset(u64::from(slot + 1) * 8);
            if slot == 0 {
                self.package_access(
                    &codes,
                    8,
                    false,
                    &clock,
                    &clock,
                    p,
                    format!("{} ENGINE codes", self.nodes[i].name),
                );
                self.nodes[i].engine.codes = self
                    .package_value(&codes, U256([initial.0[0], 0, 0, 0]), 8, &clock)?
                    .0[0];
            }
            self.package_access(
                &value_at,
                8,
                false,
                &clock,
                &clock,
                p,
                format!("{} ENGINE value {entry}", self.nodes[i].name),
            );
            let value_word = self.package_value(
                &value_at,
                U256([initial.0[(slot + 1) as usize], 0, 0, 0]),
                8,
                &clock,
            )?;
            let issued = self.tick(p, &released);
            let code = (self.nodes[i].engine.codes >> (slot * 8)) & 255;
            if let Some(part) = &self.nodes[i].part {
                if code == 26 && self.part_channel == Some(((value_word.0[0] >> 8) & 255) as u16) {
                    return Err("ENGINE ring aliases the retained join channel".into());
                }
                if self.nodes[i].at >= part.tail && code != WAIT {
                    return Err("retained protocol suffix contains an ENGINE data operation".into());
                }
            }
            self.nodes[i].engine.queue.push_back(Entry {
                code,
                value: value_word.0[0],
                run,
                issued,
            });
            self.nodes[i].engine.cursor += 1;
            self.nodes[i].engine.enqueued += 1;
            self.nodes[i].engine.drained = false;
            progress = true;
        }
        if self.nodes[i].engine.cursor == s.count {
            self.nodes[i].engine.cursor = 0;
            self.nodes[i].engine.step = None;
            self.nodes[i].at += 1;
            progress = true;
        }
        Ok(progress)
    }

    pub(super) fn engine_dispatch(&mut self, i: usize) -> Result<bool, String> {
        if let Some(entry) = self.nodes[i].engine.queue.front().cloned() {
            let p = self.nodes[i].engine.proc;
            let clock = self.tick(p, std::slice::from_ref(&entry.issued));
            if self.engine_execute(i, &entry, &clock)? {
                self.nodes[i].engine.queue.pop_front();
                let released = self.clock_of(p);
                self.nodes[i].engine.slots.push_back(released);
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn engine_execute(&mut self, i: usize, entry: &Entry, clock: &Clock) -> Result<bool, String> {
        let Entry {
            code, value, run, ..
        } = *entry;
        match code {
            WAIT => return self.engine_wait(i, value),
            GO => self.engine_go(i, clock)?,
            0..=4 => {
                self.nodes[i].engine.mailbox[code as usize] = value;
                if code == ARG3 {
                    self.nodes[i].engine.relative = None;
                }
            }
            c if c == (ARG3 | REL) => {
                self.nodes[i].engine.mailbox[ARG3 as usize] = value;
                self.nodes[i].engine.relative = Some(run);
            }
            8..=23 => self.mover_write(i, (code - 8) * 8, value, clock),
            26 => {
                let done: Vec<_> = self.nodes[i].moves.last().cloned().into_iter().collect();
                let p = self.nodes[i].engine.proc;
                self.tick(p, &done);
                let cpu = self.nodes[i].proc;
                let at = self.nodes[i].at;
                self.nodes[i].proc = p;
                self.node_other(
                    i,
                    Step::new(
                        op::RING,
                        (value & 3) as u16,
                        ((value >> 8) & 255) as u32,
                        ((value >> 16) & 65535) | (((value >> 32) & 1) << 16),
                        0,
                    ),
                );
                self.nodes[i].proc = cpu;
                self.nodes[i].at = at;
            }
            _ => return Err(format!("ENGINE control code {code} is not modelled")),
        }
        Ok(true)
    }

    fn engine_wait(&mut self, i: usize, value: u64) -> Result<bool, String> {
        let source = ((value >> 56) & 31) as usize;
        let nd = &self.nodes[i];
        let mask = nd.engine.mask;
        let want = value & mask;
        let (base, now, counter) = if source == nd.engine.mover_source {
            (nd.move_base as i64, nd.moves.len() as i64, None)
        } else {
            let unit = nd
                .units
                .get(source)
                .copied()
                .flatten()
                .ok_or("ENGINE WAIT names an absent counter")?;
            let counter = self.units[unit].done;
            (
                nd.engine.base[source],
                self.counters[counter].value,
                Some(counter),
            )
        };
        // Acquire the earliest satisfying frontier, not whichever counter epoch
        // an eager unit simulation has already completed.
        let previous = nd.engine.observed[source];
        let target = if ((previous as u64).wrapping_sub(want) & mask) <= mask / 2 {
            base + previous
        } else {
            base + previous + (want.wrapping_sub(previous as u64) & mask) as i64
        };
        if now < target {
            self.nodes[i].why = format!("ENGINE WAIT source {source} target {want}");
            return Ok(false);
        }
        self.nodes[i].engine.observed[source] = target - base;
        let observed = if let Some(counter) = counter {
            let unit = self.nodes[i].units[source].ok_or("ENGINE WAIT unit absent")?;
            let cap = [(self.units[unit].completer, self.nodes[i].sent[source])];
            self.clock_to(counter, target.max(0), &cap)
        } else if target > 0 {
            self.nodes[i].moves[target as usize - 1].clone()
        } else {
            Clock::default()
        };
        let p = self.nodes[i].engine.proc;
        self.tick(p, &[observed]);
        Ok(true)
    }

    fn engine_go(&mut self, i: usize, clock: &Clock) -> Result<(), String> {
        let nd = &self.nodes[i];
        let mb = nd.engine.mailbox;
        let fetch = mb[0] & ktpu_l0::engine::TYPED_MEM_RD_REQ != 0;
        let (x, y) = if fetch {
            let coord = mb[3] >> 40;
            ((coord & 15) as u8, ((coord >> 4) & 15) as u8)
        } else {
            ((mb[0] & 255) as u8, ((mb[0] >> 8) & 255) as u8)
        };
        let mesh = nd.mesh;
        let unit = self
            .unit((mesh, x, y))
            .ok_or("ENGINE GO names an absent unit")?;
        let nd = &self.nodes[i];
        let u = nd
            .units
            .iter()
            .position(|&candidate| candidate == Some(unit))
            .ok_or("ENGINE GO destination absent from unit table")?;
        if !fetch && mb[0] & !0xffff != 0 {
            return Err("ENGINE mailbox destination type is not modelled".into());
        }
        if fetch {
            let port = u64::from(nd.pkgs[0].units[u].fetch & 0xffff);
            if mb[0] != (ktpu_l0::engine::TYPED_MEM_RD_REQ | port)
                || mb[1] != 0
                || mb[2] != 0
                || mb[3] != (((u64::from(y) << 4) | u64::from(x)) << 40 | 1 << 30)
                || mb[4] & 0xffff00 != 0x6000
            {
                return Err("ENGINE fetch descriptor is not modelled".into());
            }
            let first = mb[4] >> 24;
            let count = mb[4] & 255;
            if first % 32 != 0 || count == 0 {
                return Err("ENGINE fetch requires aligned words and a nonzero count".into());
            }
            let relative = nd.engine.relative;
            for k in 0..count {
                let (word, source) = if let Some(run) = relative {
                    let nd = &self.nodes[i];
                    let offset = (first / 32 + k) as usize;
                    let package = nd.pkgs.get(run).ok_or(
                        "retained ENGINE relative descriptor requires reinitialization in this part"
                    )?;
                    if offset >= package.payloads.len() {
                        return Err("ENGINE relative fetch outside package".into());
                    }
                    let index = nd.payload_bases[run] + offset;
                    let source = nd.locations[index].clone();
                    let initial = nd.payloads[index];
                    (self.package_value(&source, initial, 32, clock)?, source)
                } else {
                    let at = first + k * 32;
                    let mut word = U256([0; 4]);
                    for limb in 0..4 {
                        let physical = self.physical(at + limb * 8);
                        let value = self
                            .card
                            .values
                            .get(physical, 8, clock)
                            .ok_or("ENGINE fetch instruction has no visible known bytes")?;
                        word.set_bits(limb as u32 * 64, 64, value);
                    }
                    (word, Location::Physical(at))
                };
                self.deliver(unit, word, clock.clone());
                if let Some(word) = self.units[unit].words.last_mut() {
                    word.source = Some(source);
                }
            }
            self.nodes[i].sent[u] += count as i64;
        } else {
            self.deliver(unit, U256([mb[1], mb[2], mb[3], mb[4]]), clock.clone());
            self.nodes[i].sent[u] += 1;
        }
        Ok(())
    }
}
