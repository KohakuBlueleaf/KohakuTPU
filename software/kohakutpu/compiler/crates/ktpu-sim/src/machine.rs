//! The units a node commands and the schedule between them: words of one unit
//! run in order, units in any order until a wait says otherwise (L1.SYNC.001-.002).

use std::collections::{HashMap, VecDeque};
use std::rc::Rc;

use ktpu_isa::Isa;

use crate::mem::Dram;
use crate::rng::Rng;
use crate::units::cluster::{self, Cluster};
use crate::units::core::inst::Inst;
use crate::units::core::{decode, Core, Desc, Step};
use crate::units::link::{Data, Landing, Place};
use crate::units::mover::{self, Move};
use crate::R;

/// A word sent to a core: an L1 op's whole descriptor and program, or a CU word.
#[derive(Clone, Debug)]
pub enum VcWord {
    Desc(u32, Desc),
    Run(Rc<Vec<Inst>>),
    Imem(usize, u64),
    DescField(u32, u32, u64),
    RunAt(usize),
}

const IMEM_WORDS: usize = 1024;

pub struct Vc {
    pub core: Core,
    imem: Vec<u64>,
    decoded: Option<(Rc<Isa>, decode::Cache)>,
    queue: VecDeque<VcWord>,
    pub sent: u64,
    pub done: u64,
}

/// ISA.MG.035, .050: at most eight granules per handoff, paid in full.
struct PendingOutput {
    output: cluster::Output,
    data: Data,
    sent: usize,
    completes_word: bool,
}

impl PendingOutput {
    fn granules(&self) -> usize {
        match &self.data {
            Data::Words(words) => words.len(),
            Data::Acc(tiles) => 2 * tiles.len(),
            Data::Credit(_) => unreachable!("output is data"),
        }
    }

    fn burst(&self) -> usize {
        (self.granules() - self.sent).min(8)
    }
}

pub struct Mg {
    pub cluster: Cluster,
    /// Incoming CREDIT amounts not yet spent; survives package boundaries.
    pub credits: usize,
    output: Option<PendingOutput>,
    queue: VecDeque<cluster::Word>,
    pub sent: u64,
    pub done: u64,
}

impl Mg {
    fn output_ready(&self) -> bool {
        self.output
            .as_ref()
            .is_some_and(|p| !p.output.credit || self.credits >= p.burst())
    }

    fn word_ready(&self) -> bool {
        let Some(w) = self.queue.front() else {
            return false;
        };
        if let Some(p) = &self.output {
            // FILL may overlap an emitting sweep's output. Collection and the
            // next output wait for its last handoff (ISA.MG.027, .030).
            if p.completes_word
                || !matches!(
                    w,
                    cluster::Word::Fill { .. }
                        | cluster::Word::FillRecv { .. }
                        | cluster::Word::Gemm { emit: None, .. }
                )
            {
                return false;
            }
        }
        self.cluster.ready(w)
    }
}

/// A unit of the schedule.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Unit {
    Vc(usize),
    Mg(usize),
    Mover,
}

/// `cores` and `clusters` units a mesh, in `vc` and `mg` mesh after mesh.
pub struct Machine {
    pub mem: Dram,
    pub vc: Vec<Vc>,
    pub mg: Vec<Mg>,
    cores: usize,
    clusters: usize,
    /// Each coordinate's unit, for words that name one that way.
    pub places: HashMap<(u32, u32), Place>,
    moves: VecDeque<Move>,
    pub moves_sent: u64,
    pub moves_done: u64,
    pub rng: Rng,
    /// What reads CU words; an L1 program sends none.
    pub isa: Option<Rc<Isa>>,
    /// Every move in posting order, what two levels' runs compare first.
    pub posted: Vec<Move>,
}

impl Machine {
    pub fn new(mem: Dram, cores: usize, clusters: usize, regs: usize, seed: u64) -> Machine {
        Machine::of_meshes(mem, 1, cores, clusters, regs, seed)
    }

    pub fn of_meshes(
        mem: Dram,
        meshes: usize,
        cores: usize,
        clusters: usize,
        regs: usize,
        seed: u64,
    ) -> Machine {
        Machine {
            mem,
            cores,
            clusters,
            places: HashMap::new(),
            vc: (0..meshes * cores)
                .map(|_| Vc {
                    core: Core::new(regs),
                    imem: vec![0; IMEM_WORDS],
                    decoded: None,
                    queue: VecDeque::new(),
                    sent: 0,
                    done: 0,
                })
                .collect(),
            mg: (0..meshes * clusters)
                .map(|_| Mg {
                    cluster: Cluster::default(),
                    credits: 0,
                    output: None,
                    queue: VecDeque::new(),
                    sent: 0,
                    done: 0,
                })
                .collect(),
            moves: VecDeque::new(),
            moves_sent: 0,
            moves_done: 0,
            rng: Rng::new(seed),
            isa: None,
            posted: Vec::new(),
        }
    }

    pub fn send_vc(&mut self, i: usize, w: VcWord) -> R<()> {
        let v = self.vc.get_mut(i).ok_or(format!("vc[{i}]: no such core"))?;
        v.queue.push_back(w);
        v.sent += 1;
        Ok(())
    }

    pub fn send_mg(&mut self, i: usize, w: cluster::Word) -> R<()> {
        let m = self
            .mg
            .get_mut(i)
            .ok_or(format!("mg[{i}]: no such cluster"))?;
        m.queue.push_back(w);
        m.sent += 1;
        Ok(())
    }

    pub fn post_move(&mut self, m: Move) {
        self.posted.push(m.clone());
        self.moves.push_back(m);
        self.moves_sent += 1;
    }

    /// Unit `u` of `mesh`, as its place in the machine.
    pub fn in_mesh(&self, mesh: usize, u: Unit) -> Unit {
        match u {
            Unit::Vc(i) => Unit::Vc(mesh * self.cores + i),
            Unit::Mg(i) => Unit::Mg(mesh * self.clusters + i),
            Unit::Mover => Unit::Mover,
        }
    }

    fn busy(&self, u: Unit) -> bool {
        match u {
            Unit::Vc(i) => !self.vc[i].queue.is_empty() || !self.vc[i].core.idle(),
            Unit::Mg(i) => !self.mg[i].queue.is_empty() || self.mg[i].output.is_some(),
            Unit::Mover => !self.moves.is_empty(),
        }
    }

    fn runnable(&self, u: Unit) -> bool {
        match u {
            Unit::Vc(i) => self.vc[i].core.runnable(),
            Unit::Mg(i) => self.mg[i].output_ready() || self.mg[i].word_ready(),
            Unit::Mover => true,
        }
    }

    /// A burst from a unit of `mesh` lands.
    fn carry(&mut self, mesh: usize, l: Landing) -> R<()> {
        let m = l.to.mesh.unwrap_or(mesh);
        let place = match l.to.place {
            Place::At(x, y) => *self
                .places
                .get(&(x, y))
                .ok_or(format!("no unit at ({x}, {y})"))?,
            other => other,
        };
        match place {
            Place::Vc(i) => {
                let Data::Words(words) = &l.data else {
                    return Err("an accumulator burst to a core".into());
                };
                let v = self
                    .vc
                    .get_mut(m * self.cores + i)
                    .ok_or(format!("vc[{i}] of mesh {m}"))?;
                v.core.land(l.at, words, l.count);
                Ok(())
            }
            Place::Mg(i) => {
                let c = self
                    .mg
                    .get_mut(m * self.clusters + i)
                    .ok_or(format!("mg[{i}] of mesh {m}"))?;
                if let Data::Credit(n) = l.data {
                    c.credits += n;
                    Ok(())
                } else {
                    c.cluster.land(l.buf, l.at, &l.data, l.count)
                }
            }
            Place::At(..) => unreachable!("a coordinate is resolved above"),
        }
    }

    fn output_burst(&mut self, i: usize) -> R<()> {
        let m = &mut self.mg[i];
        let p = m.output.as_mut().expect("a ready output");
        let n = p.burst();
        let data = match &p.data {
            Data::Words(words) => Data::Words(words[p.sent..p.sent + n].to_vec()),
            Data::Acc(tiles) => Data::Acc(tiles[p.sent / 2..(p.sent + n) / 2].to_vec()),
            Data::Credit(_) => unreachable!("output is data"),
        };
        let landing = Landing {
            to: p.output.to,
            buf: p.output.buf,
            at: p.output.at + p.sent,
            data,
            count: p.output.count,
        };
        if p.output.credit {
            m.credits -= n;
        }
        p.sent += n;
        if p.sent == p.granules() {
            if p.completes_word {
                m.done += 1;
            }
            m.output = None;
        }
        self.carry(i / self.clusters, landing)
    }

    fn step_unit(&mut self, u: Unit) -> R<()> {
        if let Some(reach) = &mut self.mem.reach {
            reach.unit = format!("{u:?}");
        }
        match u {
            Unit::Vc(i) => {
                let v = &mut self.vc[i];
                if v.core.idle() {
                    match v.queue.pop_front().expect("a busy core's word") {
                        VcWord::Desc(ad, d) => {
                            v.core.desc[ad as usize] = d;
                            v.done += 1;
                        }
                        VcWord::Run(prog) => v.core.start(prog),
                        VcWord::Imem(at, word) => {
                            v.imem[at] = word;
                            v.done += 1;
                        }
                        VcWord::DescField(ad, fld, value) => {
                            let d = &mut v.core.desc[ad as usize];
                            if fld == 0 {
                                d.base = value as i64;
                            } else if fld <= 4 {
                                let stride = ((value >> 16) as i64) << 46 >> 46;
                                d.dims[fld as usize - 1] = (stride, value as u32 & 0xFFFF);
                            }
                            v.done += 1;
                        }
                        VcWord::RunAt(pc) => {
                            let isa = self.isa.as_ref().ok_or("CU words with no ISA")?;
                            if v.decoded
                                .as_ref()
                                .is_none_or(|(old, _)| !Rc::ptr_eq(old, isa))
                            {
                                v.decoded = Some((isa.clone(), decode::Cache::default()));
                            }
                            let (_, cache) = v.decoded.as_mut().expect("decoder ISA");
                            v.core
                                .start(Rc::new(cache.program(&isa.vector, &v.imem, pc)?));
                        }
                    }
                    return Ok(());
                }
                if v.core.step(&mut self.mem, &mut self.rng)? == Step::Done {
                    v.done += 1;
                }
                let out = std::mem::take(&mut v.core.outbox);
                for l in out {
                    self.carry(i / self.cores, l)?;
                }
            }
            Unit::Mg(i) => {
                if self.mg[i].output_ready() {
                    return self.output_burst(i);
                }
                let m = &mut self.mg[i];
                let w = m.queue.pop_front().expect("a ready cluster word");
                match w {
                    cluster::Word::GemmTo { sweep, output } => {
                        m.cluster.run(&sweep, &mut self.mem)?;
                        m.output = Some(PendingOutput {
                            data: Data::Words(m.cluster.words(output.n)),
                            output,
                            sent: 0,
                            completes_word: false,
                        });
                    }
                    cluster::Word::DrainCreditTo { output } => {
                        m.output = Some(PendingOutput {
                            data: m.cluster.out(output.buf, output.n),
                            output,
                            sent: 0,
                            completes_word: true,
                        });
                        return Ok(());
                    }
                    cluster::Word::DrainTo {
                        to,
                        buf,
                        at,
                        n,
                        count,
                    } => {
                        let data = m.cluster.out(buf, n);
                        self.carry(
                            i / self.clusters,
                            Landing {
                                to,
                                buf,
                                at,
                                data,
                                count,
                            },
                        )?;
                    }
                    _ => m.cluster.run(&w, &mut self.mem)?,
                }
                self.mg[i].done += 1;
            }
            Unit::Mover => {
                let m = self.moves.pop_front().expect("a busy mover's move");
                mover::run(&m, &mut self.mem)?;
                self.moves_done += 1;
            }
        }
        Ok(())
    }

    /// One step of a unit with work; false when none has any.
    pub fn step(&mut self) -> R<bool> {
        let mut busy = Vec::with_capacity(self.vc.len() + self.mg.len() + 1);
        busy.extend((0..self.vc.len()).map(Unit::Vc));
        busy.extend((0..self.mg.len()).map(Unit::Mg));
        busy.push(Unit::Mover);
        busy.retain(|u| self.busy(*u));
        if busy.is_empty() {
            return Ok(false);
        }
        let waiting = busy[0];
        busy.retain(|u| self.runnable(*u));
        if busy.is_empty() {
            return Err(match waiting {
                Unit::Vc(i) => self.vc[i].core.state(),
                u => format!("{u:?} waits on what no unit gives"),
            });
        }
        let u = busy[self.rng.pick(busy.len())];
        self.step_unit(u)?;
        Ok(true)
    }

    /// Steps until `done` holds.
    pub fn until(&mut self, done: impl Fn(&Machine) -> bool) -> R<()> {
        while !done(self) {
            if !self.step()? {
                return Err("a wait no unit can end".into());
            }
        }
        Ok(())
    }

    pub fn units_idle(&self) -> bool {
        (0..self.vc.len()).all(|i| !self.busy(Unit::Vc(i)))
            && (0..self.mg.len()).all(|i| !self.busy(Unit::Mg(i)))
    }

    /// Whether every unit of `mesh` has run out of words.
    pub fn mesh_idle(&self, mesh: usize) -> bool {
        (0..self.cores).all(|i| !self.busy(self.in_mesh(mesh, Unit::Vc(i))))
            && (0..self.clusters).all(|i| !self.busy(self.in_mesh(mesh, Unit::Mg(i))))
    }

    pub fn idle(&self) -> bool {
        self.units_idle() && self.moves.is_empty()
    }
}
