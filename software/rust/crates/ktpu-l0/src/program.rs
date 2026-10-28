//! An L1 program: per-unit streams and the node's sync points, lowered to an
//! L0 package (L0.STEP.015, .021, .023, .028, .054, .062, .064).
//!
//! `send` queues words for a unit; `wait` holds the node until that unit has
//! completed what was sent to it, one completion per word; `barrier` until
//! every unit has; `move` is a mover step. The order, the units and the words
//! are the program's.

use std::collections::{BTreeMap, HashMap};

use crate::build::{Addresses, Increment, PackageBuilder, Segment};
use crate::error::Result;
use crate::facts::{Coord, Facts};
use crate::format::{signature, Alu, Cond, ANY_UNIT};
use crate::refuse;
use crate::units::machine_units;
use crate::word::Word;

/// Word `word` of an op's flits gets, when the node reaches it, `(register
/// reg + konst) >> shift` into each `(bit, width, shift)` of `segments`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Patch {
    pub word: usize,
    pub segments: Vec<Segment>,
    pub reg: u64,
    pub konst: u64,
}

/// One program statement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Stmt {
    Send {
        coord: Coord,
        words: Vec<Word>,
        patches: Vec<Patch>,
    },
    SendAdvance {
        coord: Coord,
        words: Vec<Word>,
        patches: Vec<Patch>,
    },
    Repeat {
        coord: Coord,
        words: Vec<Word>,
        repeats: u64,
        increments: Vec<Increment>,
    },
    Ring {
        mesh: u64,
        slot: u64,
        amount: u64,
        fence: bool,
    },
    WaitBell {
        mesh: u64,
        count: u64,
    },
    WaitSlot {
        slot: u64,
        count: u64,
        consume: bool,
    },
    Store {
        pairs: Vec<(u64, u64)>,
        from_regs: bool,
    },
    Ctrl {
        pairs: Vec<(u64, u64)>,
        from_regs: bool,
    },
    Load {
        reg: u64,
        address: u64,
        index: Option<u64>,
        ctrl: bool,
    },
    Alu {
        rd: u64,
        op: Alu,
        ra: u64,
        b: u64,
        imm: bool,
    },
    Label(String),
    Jump {
        name: String,
        cond: Cond,
        ra: u64,
        b: u64,
        imm: bool,
    },
    Signal {
        value32: u64,
        value64: u64,
    },
    AssertZero {
        reg: u64,
        code: u64,
    },
    Settle(u64),
    SendRaw {
        coord: Coord,
        flit_type: u64,
        words: Vec<Word>,
        ack: bool,
    },
    Push {
        coord: Coord,
        descriptor: Word,
        address: u64,
        words: u64,
        ack: bool,
    },
    Recv {
        address: u64,
        count: u64,
        coord: Option<Coord>,
    },
    Mark {
        coord: Coord,
        token: usize,
    },
    Wait {
        coord: Coord,
        token: Option<usize>,
    },
    Barrier,
    Move {
        writes: Vec<(u64, u64)>,
        patches: Vec<Patch>,
    },
    MoveAdvance {
        writes: Vec<(u64, u64)>,
        patches: Vec<Patch>,
    },
    Post {
        writes: Vec<(u64, u64)>,
        token: usize,
        patches: Vec<Patch>,
    },
    WaitMoves(usize),
    Fetch {
        coord: Coord,
        addr: u64,
        count: u64,
        patches: Vec<Patch>,
    },
}

/// A project's rewrite of a unit's stream against what the unit already
/// holds (a vector core's resident instruction memory), kept across packages.
pub trait Resident {
    /// `words` for the unit of `unit_type` at `coord`, as it should be sent.
    fn lower(&mut self, unit_type: &str, coord: Coord, words: Vec<Word>) -> Result<Vec<Word>>;
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Program {
    pub steps: Vec<Stmt>,
    /// `{base: size}`: the buffers the program is bound to.
    pub spans: BTreeMap<u64, u64>,
}

/// What `Program::build` is given besides the program.
#[derive(Default)]
pub struct BuildOpts<'a> {
    /// The memory port units stream their words from.
    pub fetch: Option<Coord>,
    /// The state the per-unit-type rewrites keep (None: every word is sent).
    pub resident: Option<&'a mut dyn Resident>,
    /// The project's address fields.
    pub addresses: Option<&'a dyn Addresses>,
    /// `{base: size}`: address fields inside a span are relocated.
    pub spans: Option<&'a BTreeMap<u64, u64>>,
}

/// Moves a mover step starts: its writes to the control register with GO.
fn gos(writes: &[(u64, u64)]) -> u64 {
    writes
        .iter()
        .filter(|&&(r, v)| r == 0 && v >> 16 & 1 != 0)
        .count() as u64
}

impl Program {
    pub fn new() -> Program {
        Program::default()
    }

    /// Append words to `coord`'s stream; `patches` (word indices within these
    /// words) set fields from registers when the node reaches them.
    pub fn send(&mut self, coord: Coord, words: Vec<Word>, patches: Vec<Patch>) -> &mut Self {
        if !words.is_empty() {
            self.steps.push(Stmt::Send {
                coord,
                words,
                patches,
            });
        }
        self
    }

    /// A fixed dispatch template repeated with affine field increments.
    pub fn repeat(
        &mut self,
        coord: Coord,
        words: Vec<Word>,
        repeats: u64,
        increments: Vec<Increment>,
    ) -> &mut Self {
        self.steps.push(Stmt::Repeat {
            coord,
            words,
            repeats,
            increments,
        });
        self
    }

    pub fn ring(&mut self, mesh: u64, slot: u64, amount: u64, fence: bool) -> &mut Self {
        self.steps.push(Stmt::Ring {
            mesh,
            slot,
            amount,
            fence,
        });
        self
    }

    pub fn wait_bell(&mut self, mesh: u64, count: u64) -> &mut Self {
        self.steps.push(Stmt::WaitBell { mesh, count });
        self
    }

    pub fn wait_slot(&mut self, slot: u64, count: u64, consume: bool) -> &mut Self {
        self.steps.push(Stmt::WaitSlot {
            slot,
            count,
            consume,
        });
        self
    }

    pub fn store(&mut self, pairs: Vec<(u64, u64)>, from_regs: bool) -> &mut Self {
        self.steps.push(Stmt::Store { pairs, from_regs });
        self
    }

    pub fn ctrl(&mut self, pairs: Vec<(u64, u64)>, from_regs: bool) -> &mut Self {
        self.steps.push(Stmt::Ctrl { pairs, from_regs });
        self
    }

    pub fn load(&mut self, reg: u64, address: u64, index: Option<u64>, ctrl: bool) -> &mut Self {
        self.steps.push(Stmt::Load {
            reg,
            address,
            index,
            ctrl,
        });
        self
    }

    pub fn alu(&mut self, rd: u64, op: Alu, ra: u64, b: u64, imm: bool) -> &mut Self {
        self.steps.push(Stmt::Alu { rd, op, ra, b, imm });
        self
    }

    /// A jump target here; refuses a name used twice.
    pub fn label(&mut self, name: &str) -> Result<&mut Self> {
        if self
            .steps
            .iter()
            .any(|s| matches!(s, Stmt::Label(n) if n == name))
        {
            return refuse!("L0.STEP.052", "label {name} twice");
        }
        self.steps.push(Stmt::Label(name.to_string()));
        Ok(self)
    }

    pub fn jump(&mut self, name: &str, cond: Cond, ra: u64, b: u64, imm: bool) -> &mut Self {
        self.steps.push(Stmt::Jump {
            name: name.to_string(),
            cond,
            ra,
            b,
            imm,
        });
        self
    }

    pub fn signal(&mut self, value32: u64, value64: u64) -> &mut Self {
        self.steps.push(Stmt::Signal { value32, value64 });
        self
    }

    pub fn settle(&mut self, cycles: u64) -> &mut Self {
        self.steps.push(Stmt::Settle(cycles));
        self
    }

    pub fn send_raw(
        &mut self,
        coord: Coord,
        flit_type: u64,
        words: Vec<Word>,
        ack: bool,
    ) -> &mut Self {
        self.steps.push(Stmt::SendRaw {
            coord,
            flit_type,
            words,
            ack,
        });
        self
    }

    pub fn push(
        &mut self,
        coord: Coord,
        descriptor: Word,
        address: u64,
        words: u64,
        ack: bool,
    ) -> &mut Self {
        self.steps.push(Stmt::Push {
            coord,
            descriptor,
            address,
            words,
            ack,
        });
        self
    }

    pub fn recv(&mut self, address: u64, count: u64, coord: Option<Coord>) -> &mut Self {
        self.steps.push(Stmt::Recv {
            address,
            count,
            coord,
        });
        self
    }

    /// A point in `coord`'s stream: everything sent to it so far. Returns the
    /// token a `wait` names.
    pub fn mark(&mut self, coord: Coord) -> usize {
        let token = self
            .steps
            .iter()
            .filter(|s| matches!(s, Stmt::Mark { .. }))
            .count();
        self.steps.push(Stmt::Mark { coord, token });
        token
    }

    /// Hold the node until `coord` has completed everything sent to it, or,
    /// given a mark token of that unit, everything sent up to the mark.
    pub fn wait(&mut self, coord: Coord, token: Option<usize>) -> Result<&mut Self> {
        if let Some(t) = token {
            let at = self.steps.iter().find_map(|s| match s {
                Stmt::Mark { coord, token } if *token == t => Some(*coord),
                _ => None,
            });
            if at != Some(coord) {
                return refuse!("L0.STEP.021", "token {t} marks {at:?}, not {coord:?}");
            }
        }
        self.steps.push(Stmt::Wait { coord, token });
        Ok(self)
    }

    pub fn barrier(&mut self) -> &mut Self {
        self.steps.push(Stmt::Barrier);
        self
    }

    /// `count` words for `coord` its fetch port reads from `addr` when the
    /// node gets here; `patches` set the step's own address (step bits
    /// [127:64]) from registers.
    pub fn fetch(&mut self, coord: Coord, addr: u64, count: u64, patches: Vec<Patch>) -> &mut Self {
        self.steps.push(Stmt::Fetch {
            coord,
            addr,
            count,
            patches,
        });
        self
    }

    /// A mover step the node waits for, then for every unit; a patch's word is
    /// the index of its write.
    pub fn move_(&mut self, writes: Vec<(u64, u64)>, patches: Vec<Patch>) -> &mut Self {
        self.steps.push(Stmt::Move { writes, patches });
        self
    }

    /// A mover step the node does not wait for. Returns the token a
    /// `wait_moves` names.
    pub fn post(&mut self, writes: Vec<(u64, u64)>, patches: Vec<Patch>) -> usize {
        let token = self
            .steps
            .iter()
            .filter(|s| matches!(s, Stmt::Post { .. }))
            .count();
        self.steps.push(Stmt::Post {
            writes,
            token,
            patches,
        });
        token
    }

    /// Hold the node until the moves of post `token`, and every move before
    /// them, are done.
    pub fn wait_moves(&mut self, token: usize) -> Result<&mut Self> {
        if !self
            .steps
            .iter()
            .any(|s| matches!(s, Stmt::Post { token: t, .. } if *t == token))
        {
            return refuse!("L0.STEP.028", "no post {token}");
        }
        self.steps.push(Stmt::WaitMoves(token));
        Ok(self)
    }

    /// The package's builder (L0.STEP.015-L0.STEP.064). Credit is
    /// `unit.inst_depth`; a stream is cut into pieces of half of it,
    /// interleaved across units. The package carries the signature of the
    /// machine's unit table (RUL.041).
    pub fn build(&self, f: &Facts, opts: BuildOpts<'_>) -> Result<PackageBuilder> {
        if opts.resident.is_some() && self.steps.iter().any(|s| matches!(s, Stmt::Repeat { .. })) {
            return refuse!(
                "L0.STEP.041",
                "explicit repeat templates require resident rewriting disabled"
            );
        }
        Lowering::new(f, opts)?.run(&self.steps)
    }
}

/// One unit's queued words and the patches into them.
struct Pending {
    unit: usize,
    words: Vec<Word>,
    patches: Vec<Patch>,
}

struct Lowering<'a> {
    b: PackageBuilder,
    types: BTreeMap<Coord, String>,
    resident: Option<&'a mut dyn Resident>,
    addresses: Option<&'a dyn Addresses>,
    /// Words dispatched to and awaited from each unit, cumulative.
    sent: BTreeMap<usize, u64>,
    awaited: HashMap<usize, u64>,
    marks: HashMap<usize, u64>,
    /// Queued words by unit, in first-queued order.
    pending: Vec<Pending>,
    piece: usize,
    gos: u64,
    posts: HashMap<usize, u64>,
    labels: HashMap<String, u64>,
    jumps: Vec<(usize, String)>,
    /// Past a label a step may run again.
    looping: bool,
    address_literals: Vec<(usize, u64)>,
    tmp: u64,
}

impl<'a> Lowering<'a> {
    fn new(f: &Facts, opts: BuildOpts<'a>) -> Result<Lowering<'a>> {
        let types = f.unit_types();
        let mut b = PackageBuilder::new(f, types.clone(), f.inst_depth);
        b.mesh = f.mesh as u64;
        b.fetch = opts.fetch;
        b.signature = if crate::mutant::on("l0.program/no_signature") {
            0
        } else {
            signature(&machine_units(f)?)
        };
        if let Some(spans) = opts.spans.filter(|s| !s.is_empty()) {
            b.spans_from(spans);
        }
        Ok(Lowering {
            b,
            types,
            resident: opts.resident,
            addresses: opts.addresses,
            sent: BTreeMap::new(),
            awaited: HashMap::new(),
            marks: HashMap::new(),
            pending: Vec::new(),
            piece: (f.inst_depth / 2) as usize,
            gos: 0,
            posts: HashMap::new(),
            labels: HashMap::new(),
            jumps: Vec::new(),
            looping: false,
            address_literals: Vec::new(),
            tmp: if crate::mutant::on("l0.program/tmp_reg") {
                f.regs - 2
            } else {
                f.regs - 1
            },
        })
    }

    fn unit(&mut self, coord: Coord) -> Result<usize> {
        self.b.unit(coord, None, None)
    }

    fn await_to(&mut self, u: usize, upto: u64) {
        let have = self.awaited.get(&u).copied().unwrap_or(0);
        if upto > have {
            if self.looping && !crate::mutant::on("l0.program/static-loop-wait") {
                // Static counts after a backedge can fall behind by more than half the HW counter.
                self.b.await_sent(u);
            } else {
                self.b.await_(u, upto - have);
            }
            self.awaited.insert(u, upto);
        }
    }

    /// PATCH steps for `p` into word `index`, its bits offset by `base`; a
    /// constant is added through the scratch register (L0.STEP.054).
    fn patch(
        &mut self,
        p: &Patch,
        index: u64,
        steps: bool,
        base: u64,
        address: bool,
    ) -> Result<()> {
        let mut reg = p.reg;
        if p.konst != 0 || address {
            if address && !crate::mutant::on("l0.program/unrelocated-literal") {
                self.address_literals.push((self.b.steps.len(), p.konst));
                self.b.steps.push(crate::Step::new(
                    crate::Op::Load,
                    self.tmp,
                    0,
                    0,
                    crate::format::F_IMM,
                ));
                self.b.alu(self.tmp, Alu::Add, p.reg, self.tmp, false)?;
            } else {
                self.b.alu(self.tmp, Alu::Add, p.reg, p.konst, true)?;
            }
            reg = self.tmp;
        }
        for &(bit, width, shift) in &p.segments {
            self.b.patch(
                reg,
                index,
                base + bit as u64,
                width as u64,
                shift as u64,
                steps,
                false,
            )?;
        }
        Ok(())
    }

    /// A MOVER step's pair i is payload first + i / 2, its value at bit
    /// (i % 2) * 128 + 64.
    fn mover_patches(&mut self, patches: &[Patch]) -> Result<()> {
        let first = self.b.payloads.len() as u64;
        for p in patches {
            self.patch(
                p,
                first + (p.word / 2) as u64,
                false,
                (p.word % 2) as u64 * 128 + 64,
                false,
            )?;
        }
        Ok(())
    }

    /// Send queued words: every unit's (`only` None), or `only` and every unit
    /// queued before it. Pieces of half the credit interleave across units.
    fn flush(&mut self, only: Option<usize>) -> Result<()> {
        let take = match only {
            None => self.pending.len(),
            Some(u) => match self.pending.iter().position(|p| p.unit == u) {
                Some(at) => at + 1,
                None => 0,
            },
        };
        let mut ready = Vec::new();
        for p in self.pending.drain(..take).collect::<Vec<_>>() {
            let Pending {
                unit: u,
                mut words,
                patches,
            } = p;
            let unit = self.b.units[u];
            let coord = (unit.x as u32, unit.y as u32);
            if patches.is_empty() {
                if let (Some(r), Some(kind)) =
                    (self.resident.as_deref_mut(), self.types.get(&coord))
                {
                    words = r.lower(kind, coord, words)?;
                }
            }
            if words.is_empty() {
                continue;
            }
            if !patches.is_empty() && self.looping {
                self.b.await_sent(u);
            }
            *self.sent.entry(u).or_insert(0) += words.len() as u64;
            ready.push((u, words, patches));
        }
        let longest = ready.iter().map(|(_, w, _)| w.len()).max().unwrap_or(0);
        let mut k = 0;
        while k < longest {
            for (u, words, patches) in &ready {
                if k >= words.len() {
                    continue;
                }
                let chunk = &words[k..(k + self.piece).min(words.len())];
                let first = self.b.payloads.len() as u64;
                for p in patches {
                    if (k..k + chunk.len()).contains(&p.word) {
                        let unit = self.b.units[*u];
                        let kind = &self.types[&(unit.x as u32, unit.y as u32)];
                        let address = self.addresses.is_some_and(|a| {
                            a.fields(&words[p.word], kind).iter().any(|f| {
                                f.segments
                                    .iter()
                                    .any(|s| p.segments.iter().any(|p| p.0 == s.0))
                            })
                        });
                        self.patch(p, first + (p.word - k) as u64, false, 0, address)?;
                    }
                }
                self.b.dispatch(*u, chunk, self.addresses)?;
            }
            k += self.piece;
        }
        Ok(())
    }

    fn run(mut self, steps: &[Stmt]) -> Result<PackageBuilder> {
        for step in steps {
            match step {
                Stmt::Send {
                    coord,
                    words,
                    patches,
                } => {
                    let u = self.unit(*coord)?;
                    let at = match self.pending.iter().position(|p| p.unit == u) {
                        Some(at) => at,
                        None => {
                            self.pending.push(Pending {
                                unit: u,
                                words: Vec::new(),
                                patches: Vec::new(),
                            });
                            self.pending.len() - 1
                        }
                    };
                    let q = &mut self.pending[at];
                    let have = q.words.len();
                    q.words.extend_from_slice(words);
                    q.patches.extend(patches.iter().map(|p| Patch {
                        word: have + p.word,
                        ..p.clone()
                    }));
                }
                Stmt::SendAdvance {
                    coord,
                    words,
                    patches,
                } => {
                    self.flush(None)?;
                    let unit = self.unit(*coord)?;
                    self.await_to(unit, self.sent.get(&unit).copied().unwrap_or(0));
                    let first = self.b.payloads.len() as u64;
                    for patch in patches {
                        if patch.segments.len() > 1 {
                            let &(bit, width, shift) =
                                patch.segments.iter().find(|s| s.2 == 0).ok_or_else(|| {
                                    crate::Refusal::new(
                                        "L0.STEP.054",
                                        "advancing address has no low segment",
                                    )
                                })?;
                            self.b.patch(
                                patch.reg,
                                first + patch.word as u64,
                                bit as u64,
                                width as u64,
                                shift as u64,
                                false,
                                true,
                            )?;
                        } else {
                            self.patch(patch, first + patch.word as u64, false, 0, true)?;
                        }
                    }
                    self.b.dispatch(unit, words, self.addresses)?;
                    let total = self.sent.get(&unit).copied().unwrap_or(0) + words.len() as u64;
                    self.sent.insert(unit, total);
                    self.await_to(unit, total);
                }
                Stmt::Repeat {
                    coord,
                    words,
                    repeats,
                    increments,
                } => {
                    self.flush(None)?;
                    let u = self.unit(*coord)?;
                    let count = (words.len() as u64).checked_mul(*repeats).ok_or_else(|| {
                        crate::Refusal::new("L0.STEP.041", "repeat completion count overflow")
                    })?;
                    let sent = self
                        .sent
                        .get(&u)
                        .copied()
                        .unwrap_or(0)
                        .checked_add(count)
                        .ok_or_else(|| {
                            crate::Refusal::new("L0.STEP.041", "repeat completion total overflow")
                        })?;
                    self.b
                        .repeat(u, words, *repeats, increments, self.addresses)?;
                    self.sent.insert(
                        u,
                        if crate::mutant::on("l0.program/repeat-count") {
                            words.len() as u64
                        } else {
                            sent
                        },
                    );
                }
                Stmt::Ring {
                    mesh,
                    slot,
                    amount,
                    fence,
                } => {
                    self.flush(None)?;
                    self.b.ring(*mesh, *slot, *amount, *fence)?;
                }
                Stmt::WaitBell { mesh, count } => {
                    self.flush(None)?;
                    self.b.wait_bell(*mesh, *count);
                }
                Stmt::WaitSlot {
                    slot,
                    count,
                    consume,
                } => {
                    self.flush(None)?;
                    self.b.wait_slot(*slot, *count, *consume)?;
                }
                Stmt::Store { pairs, from_regs } => {
                    self.flush(None)?;
                    self.b.store(pairs, *from_regs, true);
                }
                Stmt::Ctrl { pairs, from_regs } => {
                    self.flush(None)?;
                    self.b.ctrl(pairs, *from_regs);
                }
                Stmt::Load {
                    reg,
                    address,
                    index,
                    ctrl,
                } => {
                    self.flush(None)?;
                    self.b.load(*reg, *address, *index, *ctrl, true);
                }
                Stmt::Alu { rd, op, ra, b, imm } => {
                    self.flush(None)?;
                    self.b.alu(*rd, *op, *ra, *b, *imm)?;
                }
                Stmt::Label(name) => {
                    self.flush(None)?;
                    self.labels.insert(name.clone(), self.b.steps.len() as u64);
                    self.looping = true;
                }
                Stmt::Jump {
                    name,
                    cond,
                    ra,
                    b,
                    imm,
                } => {
                    self.flush(None)?;
                    let at = self.b.jump(0, *cond, *ra, *b, *imm);
                    self.jumps.push((at, name.clone()));
                }
                Stmt::AssertZero { reg, code } => {
                    self.flush(None)?;
                    self.b.assert_zero(*reg, *code)?;
                }
                Stmt::Signal { value32, value64 } => {
                    self.flush(None)?;
                    self.b.signal(*value32, *value64);
                }
                Stmt::Settle(c) => {
                    self.flush(None)?;
                    self.b.settle(*c);
                }
                Stmt::SendRaw {
                    coord,
                    flit_type,
                    words,
                    ack,
                } => {
                    self.flush(None)?;
                    let u = self.unit(*coord)?;
                    self.b.send(u, *flit_type, words, *ack);
                }
                Stmt::Push {
                    coord,
                    descriptor,
                    address,
                    words,
                    ack,
                } => {
                    self.flush(None)?;
                    let u = self.unit(*coord)?;
                    self.b.push(u, *descriptor, *address, *words, *ack, true);
                }
                Stmt::Recv {
                    address,
                    count,
                    coord,
                } => {
                    self.flush(None)?;
                    let unit = match coord {
                        None => ANY_UNIT,
                        Some(c) => self.unit(*c)? as u64,
                    };
                    self.b.recv(*address, *count, unit, true);
                }
                Stmt::Mark { coord, token } => {
                    let u = self.unit(*coord)?;
                    self.flush(None)?;
                    let s = self.sent.get(&u).copied().unwrap_or(0);
                    self.marks.insert(*token, s);
                }
                Stmt::Wait { coord, token } => {
                    let u = self.unit(*coord)?;
                    self.flush(None)?;
                    let upto = match token {
                        None => self.sent.get(&u).copied().unwrap_or(0),
                        Some(t) => match self.marks.get(t) {
                            Some(&m) => m,
                            None => return refuse!("L0.STEP.021", "no mark {t}"),
                        },
                    };
                    self.await_to(u, upto);
                }
                Stmt::Barrier => {
                    self.flush(None)?;
                    self.await_all();
                    self.b.barrier();
                }
                Stmt::MoveAdvance { writes, patches } => {
                    self.flush(None)?;
                    let first = self.b.payloads.len() as u64;
                    for p in patches {
                        for &(bit, width, shift) in &p.segments {
                            self.b.patch(
                                p.reg,
                                first + p.word as u64 / 2,
                                (p.word % 2) as u64 * 128 + 64 + bit as u64,
                                width as u64,
                                shift as u64,
                                false,
                                !crate::mutant::on("l0.program/advance-replace"),
                            )?;
                        }
                    }
                    self.b.mover(writes, self.addresses, false);
                    self.gos += gos(writes);
                    self.b.barrier();
                }
                Stmt::Move { writes, patches } => {
                    self.flush(None)?;
                    self.mover_patches(patches)?;
                    self.b.mover(writes, self.addresses, false);
                    self.gos += gos(writes);
                    self.b.barrier();
                }
                Stmt::Post {
                    writes,
                    token,
                    patches,
                } => {
                    self.flush(None)?;
                    self.mover_patches(patches)?;
                    self.b.mover(writes, self.addresses, true);
                    self.gos += gos(writes);
                    self.posts.insert(*token, self.gos);
                }
                Stmt::WaitMoves(token) => {
                    if self.looping {
                        return refuse!(
                            "L0.STEP.064",
                            "`wait moves` past a label: its count is the package's moves so far, \
                             which a step run again changes; use a blocking move inside a loop"
                        );
                    }
                    self.flush(None)?;
                    let Some(&n) = self.posts.get(token) else {
                        return refuse!("L0.STEP.028", "no post {token}");
                    };
                    self.b.mwait(n);
                }
                Stmt::Fetch {
                    coord,
                    addr,
                    count,
                    patches,
                } => {
                    let u = self.unit(*coord)?;
                    self.flush(Some(u))?;
                    let start = self.b.steps.len();
                    for p in patches {
                        self.patch(p, 0, true, 0, true)?;
                    }
                    self.b.fetch_from(u, *addr, *count)?;
                    self.b
                        .repoint(start, (self.b.steps.len() as u64).saturating_sub(1));
                    *self.sent.entry(u).or_insert(0) += count;
                }
            }
        }
        self.flush(None)?;
        self.await_all();
        self.b.barrier();
        // Literal payloads follow dispatch payloads so their insertion cannot move PATCH targets.
        for (at, value) in std::mem::take(&mut self.address_literals) {
            self.b.address(self.tmp, value);
            self.b.steps[at] = self.b.steps.pop().unwrap();
        }
        for (at, name) in std::mem::take(&mut self.jumps) {
            let Some(&target) = self.labels.get(&name) else {
                return refuse!("L0.STEP.052", "goto {name}: no such label");
            };
            self.b.retarget(at, target);
        }
        Ok(self.b)
    }

    /// AWAITs up to everything sent, by unit index.
    fn await_all(&mut self) {
        let sent: Vec<(usize, u64)> = self.sent.iter().map(|(&u, &n)| (u, n)).collect();
        for (u, n) in sent {
            self.await_to(u, n);
        }
    }
}
