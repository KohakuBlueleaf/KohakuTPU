//! A node: its hart, IMEM, scratchpad, control region and card memory
//! (`docs/spec/control-registers.md` s7), mirrored by `ka/hal/control.h`.

use std::cell::RefCell;
use std::collections::{HashMap, VecDeque};
use std::rc::Rc;

use crate::bus::{get, put, Bus, Fault};
use crate::elf::Image;
use crate::hart::Hart;

pub const IMEM: u64 = 0;
pub const IMEM_BYTES: usize = 32 << 10;
pub const SPAD: u64 = 0x1_0000;
pub const SPAD_BYTES: usize = 32 << 10;
pub const CONTROL: u64 = 0x2_0000;
pub const CONTROL_BYTES: u64 = 0x400;
/// The uncached alias of card memory: one store behind both.
pub const UNCACHED: u64 = 1 << 38;
/// Below this a mesh-0 address is the on-chip window (`rv64_syscore` fetch_cached).
pub const CARD: u64 = 0x1000_0000;
const CARD_END: u64 = 1 << 40;
const PAGE: u64 = 4096;
/// An image's segments at or past here are per-node parts its loader places.
const IMAGE_END: u64 = CARD_END;
const MESHES: usize = 4;
const SLOTS: usize = 16;
const SPECIAL: u64 = 1 << 39;
const MESH_BITS: u64 = 3 << 36;

const R_EXIT: u64 = 0x00;
const R_CONSOLE: u64 = 0x08;
const R_DBELL: u64 = 0x10;
const R_SATP: u64 = 0x18;
const R_MVSTAT: u64 = 0x20;
const R_DBCNT: u64 = 0x28;
const R_STDIN: u64 = 0x30;
const R_IRQ: u64 = 0x38;
const R_NM: u64 = 0x40;
const R_DCACHE: u64 = 0x1C8;
const R_SIG: u64 = 0xE0;
const R_IL_CTL: u64 = 0xC0;
const R_IL_RING: u64 = 0xD0;
const R_IL_CONSUME: u64 = 0xE0;
const R_IL_SELECT: u64 = 0xE8;
const IL_CLEAR_DOORBELLS: u64 = 2;
const IL_CLEAR_SLOTS: u64 = 8;
const NM_DST: usize = 0;
const NM_ARGS: usize = 1;
const NM_GO: u64 = 5;
const NM_STAT: u64 = 6;
const NM_HEAD: u64 = 7;
const MV_ROOM: u64 = 1 << 33;

/// A mailbox send: the destination word and the 256-bit payload.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Send {
    pub dst: u64,
    pub payload: [u64; 4],
}

/// What answers the node's mesh sends; its completion words queue for `NM_HEAD`.
pub trait Mesh {
    fn send(&mut self, s: Send) -> Vec<u64>;
}

/// A mesh with nothing on it.
pub struct Silent;

impl Mesh for Silent {
    fn send(&mut self, _: Send) -> Vec<u64> {
        Vec::new()
    }
}

/// Card memory: one store behind every node, by global address.
#[derive(Default)]
pub struct Memory {
    pages: HashMap<u64, Box<[u8; PAGE as usize]>>,
    /// One DRAM whatever the mesh bits, staging still each mesh's (a flat card).
    pub flat: bool,
}

impl Memory {
    fn page(&mut self, at: u64) -> (&mut [u8; PAGE as usize], usize) {
        let at = if self.flat && at & SPECIAL == 0 {
            at & !MESH_BITS
        } else {
            at
        };
        let page = self
            .pages
            .entry(at / PAGE)
            .or_insert_with(|| Box::new([0; PAGE as usize]));
        (page, (at % PAGE) as usize)
    }
}

/// The interlink between nodes (`control-registers.md` s7.4): rings land at once.
#[derive(Default)]
pub struct Link {
    /// Each mesh's signal slots.
    slots: [[u32; SLOTS]; MESHES],
    /// Rings each mesh received from each mesh since it cleared them.
    doorbells: [[u16; MESHES]; MESHES],
    sent: [u16; MESHES],
}

impl Link {
    fn ring(&mut self, from: usize, w: u64) {
        let (mesh, slot) = ((w & 3) as usize, (w >> 8) as usize & (SLOTS - 1));
        let amount = (w >> 16) as u32 & 0xFFFF;
        self.slots[mesh][slot] = self.slots[mesh][slot].wrapping_add(amount.max(1));
        self.doorbells[mesh][from] = self.doorbells[mesh][from].wrapping_add(1);
        self.sent[from] = self.sent[from].wrapping_add(1);
    }
}

pub struct Soc {
    pub imem: Vec<u8>,
    pub spad: Vec<u8>,
    memory: Rc<RefCell<Memory>>,
    link: Rc<RefCell<Link>>,
    /// This node's mesh, and the slot `KA_R_SIG` reports.
    mesh: usize,
    selected: usize,
    /// A D-cache operation reads busy once (memory here has no cache to walk).
    flushing: bool,
    /// Registers with no behaviour here hold what was written.
    regs: HashMap<u64, u64>,
    pub exit: Option<u64>,
    pub console: Vec<u8>,
    pub stdin: VecDeque<u8>,
    pub doorbell: u64,
    pub doorbells: u64,
    pub satp: u64,
    pub irq: bool,
    pub moves: u64,
    mailbox: [u64; 5],
    pub sends: Vec<Send>,
    completions: VecDeque<u64>,
    pub units: Box<dyn Mesh>,
}

impl Soc {
    /// `bytes` of card memory at `at`, a page boundary crossed.
    pub fn card_write(&mut self, at: u64, bytes: &[u8]) {
        let mut m = self.memory.borrow_mut();
        for (i, b) in bytes.iter().enumerate() {
            let (page, off) = m.page(at + i as u64);
            page[off] = *b;
        }
    }

    pub fn card_read(&mut self, at: u64, n: usize) -> Vec<u8> {
        let mut m = self.memory.borrow_mut();
        (0..n as u64)
            .map(|i| {
                let (page, off) = m.page(at + i);
                page[off]
            })
            .collect()
    }

    pub fn card_u64(&mut self, at: u64) -> u64 {
        u64::from_le_bytes(self.card_read(at, 8).try_into().expect("eight bytes"))
    }

    pub fn card_set_u64(&mut self, at: u64, v: u64) {
        self.card_write(at, &v.to_le_bytes());
    }

    fn control_load(&mut self, off: u64) -> u64 {
        match off {
            R_DBELL => self.doorbell,
            R_SATP => self.satp,
            R_MVSTAT => MV_ROOM | self.moves & 0x0FFF_FFFF,
            R_DBCNT => {
                let link = self.link.borrow();
                let counts = link.doorbells[self.mesh].iter().enumerate();
                counts.fold(self.doorbells, |w, (m, n)| w | u64::from(*n) << (16 * m))
            }
            R_SIG => {
                let link = self.link.borrow();
                let count = u64::from(link.slots[self.mesh][self.selected]);
                count | u64::from(link.sent[self.mesh]) << 32 | (self.selected as u64) << 50
            }
            R_STDIN => self.stdin.front().map_or(0, |b| 0x100 | u64::from(*b)),
            R_IRQ => u64::from(self.irq),
            R_DCACHE => u64::from(std::mem::take(&mut self.flushing)),
            _ if (R_NM..R_NM + 0x40).contains(&off) => {
                let k = (off - R_NM) / 8;
                match k {
                    NM_GO => 0,
                    NM_STAT => self.completions.len() as u64,
                    NM_HEAD => self.completions.front().copied().unwrap_or(0),
                    _ => self.mailbox[k as usize],
                }
            }
            _ => self.regs.get(&off).copied().unwrap_or(0),
        }
    }

    fn control_store(&mut self, off: u64, v: u64) {
        match off {
            R_EXIT => self.exit = Some(v),
            R_CONSOLE => self.console.push(v as u8),
            R_STDIN => {
                self.stdin.pop_front();
            }
            R_IRQ => self.irq = !self.irq,
            R_DCACHE => self.flushing = true,
            R_IL_CTL => {
                let mut link = self.link.borrow_mut();
                if v & IL_CLEAR_DOORBELLS != 0 {
                    link.doorbells[self.mesh] = [0; MESHES];
                }
                if v & IL_CLEAR_SLOTS != 0 {
                    link.slots[self.mesh] = [0; SLOTS];
                }
            }
            R_IL_RING => self.link.borrow_mut().ring(self.mesh, v),
            R_IL_CONSUME => {
                let slot = v as usize & (SLOTS - 1);
                let mut link = self.link.borrow_mut();
                let count = &mut link.slots[self.mesh][slot];
                *count = count.wrapping_sub((v >> 32) as u32);
            }
            R_IL_SELECT => self.selected = v as usize & (SLOTS - 1),
            _ if (R_NM..R_NM + 0x40).contains(&off) => {
                let k = (off - R_NM) / 8;
                match k {
                    NM_GO => {
                        let m = self.mailbox;
                        let s = Send {
                            dst: m[NM_DST],
                            payload: [m[NM_ARGS], m[2], m[3], m[4]],
                        };
                        self.sends.push(s);
                        self.completions.extend(self.units.send(s));
                    }
                    NM_STAT => {}
                    NM_HEAD => {
                        self.completions.pop_front();
                    }
                    _ => self.mailbox[k as usize] = v,
                }
            }
            _ => {
                self.regs.insert(off, v);
            }
        }
    }
}

/// The node port: anything at or past [`CARD`], staging included.
fn card_address(at: u64) -> Option<u64> {
    let at = at & !UNCACHED;
    (CARD..CARD_END).contains(&at).then_some(at)
}

impl Bus for Soc {
    fn load(&mut self, at: u64, bytes: u32) -> Result<u64, Fault> {
        match at {
            _ if at < IMEM + IMEM_BYTES as u64 => get(&self.imem, (at - IMEM) as usize, bytes),
            _ if (SPAD..SPAD + SPAD_BYTES as u64).contains(&at) => {
                get(&self.spad, (at - SPAD) as usize, bytes)
            }
            _ if (CONTROL..CONTROL + CONTROL_BYTES).contains(&at) => {
                Ok(self.control_load(at - CONTROL))
            }
            _ => {
                let at = card_address(at).ok_or(Fault)?;
                let raw = self.card_read(at, bytes as usize);
                get(&raw, 0, bytes)
            }
        }
    }

    fn store(&mut self, at: u64, bytes: u32, value: u64) -> Result<(), Fault> {
        match at {
            _ if at < IMEM + IMEM_BYTES as u64 => {
                put(&mut self.imem, (at - IMEM) as usize, bytes, value)
            }
            _ if (SPAD..SPAD + SPAD_BYTES as u64).contains(&at) => {
                put(&mut self.spad, (at - SPAD) as usize, bytes, value)
            }
            _ if (CONTROL..CONTROL + CONTROL_BYTES).contains(&at) => {
                self.control_store(at - CONTROL, value);
                Ok(())
            }
            _ => {
                let at = card_address(at).ok_or(Fault)?;
                self.card_write(at, &value.to_le_bytes()[..bytes as usize]);
                Ok(())
            }
        }
    }

    fn tick(&mut self, _cycle: u64) -> bool {
        self.link.borrow().doorbells[self.mesh]
            .iter()
            .any(|n| *n != 0)
    }
}

pub struct Node {
    pub hart: Hart,
    pub soc: Soc,
}

impl Node {
    pub fn new(mesh: Box<dyn Mesh>) -> Node {
        let shared = (Rc::default(), Rc::default());
        Node::on(0, shared, mesh)
    }

    /// Node `index` of a card whose memory and interlink are `shared`.
    pub fn on(
        index: usize,
        shared: (Rc<RefCell<Memory>>, Rc<RefCell<Link>>),
        mesh: Box<dyn Mesh>,
    ) -> Node {
        Node {
            hart: Hart::new(index as u64, IMEM),
            soc: Soc {
                imem: vec![0; IMEM_BYTES],
                spad: vec![0; SPAD_BYTES],
                memory: shared.0,
                link: shared.1,
                mesh: index,
                selected: 0,
                flushing: false,
                regs: HashMap::new(),
                exit: None,
                console: Vec::new(),
                stdin: VecDeque::new(),
                doorbell: 0,
                doorbells: 0,
                satp: 0,
                irq: false,
                moves: 0,
                mailbox: [0; 5],
                sends: Vec::new(),
                completions: VecDeque::new(),
                units: mesh,
            },
        }
    }

    /// Places `image` and starts the hart at its entry.
    pub fn load(&mut self, image: &Image) -> Result<(), Fault> {
        for s in image.segments.iter().filter(|s| s.at < IMAGE_END) {
            self.place(s.at, &s.bytes)?;
        }
        self.hart.pc = image.entry;
        Ok(())
    }

    /// `bytes` at physical address `at`.
    pub fn place(&mut self, at: u64, bytes: &[u8]) -> Result<(), Fault> {
        for (i, b) in bytes.iter().enumerate() {
            self.soc.store(at + i as u64, 1, u64::from(*b))?;
        }
        Ok(())
    }

    /// One cycle.
    pub fn step(&mut self) {
        self.hart.step(&mut self.soc);
        self.soc.satp = self.hart.csr.satp;
    }

    /// Runs to the exit register or `limit` cycles; the exit word, if one came.
    pub fn run(&mut self, limit: u64) -> Option<u64> {
        self.run_until(limit, |_| false);
        self.soc.exit
    }

    /// Runs until `done` holds or the node exits; whether `done` held. `done`
    /// is the host: it sees the node between cycles and may write its memory.
    pub fn run_until(&mut self, limit: u64, mut done: impl FnMut(&mut Soc) -> bool) -> bool {
        for _ in 0..limit {
            self.step();
            if done(&mut self.soc) {
                return true;
            }
            if self.soc.exit.is_some() {
                break;
            }
        }
        false
    }
}
