//! One hart: RV64IMA, Zicsr, Zifencei; every instruction a cycle.

use crate::bus::Bus;
use crate::csr::Csrs;
use crate::mutant;
use crate::vm::Access;

pub const CAUSE_ILLEGAL: u64 = 2;
const CAUSE_ECALL_U: u64 = 8;
const INTERRUPT: u64 = 1 << 63;
const MIE_BIT: u64 = 1 << 3;
const SIE_BIT: u64 = 1 << 1;
const MPIE_BIT: u64 = 1 << 7;
const SPIE_BIT: u64 = 1 << 5;
const SPP_BIT: u64 = 1 << 8;
const MPP_SHIFT: u32 = 11;
const TIMER_BIT: u64 = 1 << 7;
const EXTERNAL_BIT: u64 = 1 << 11;

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Mode {
    User = 0,
    Supervisor = 1,
    Machine = 3,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Trap {
    pub cause: u64,
    pub tval: u64,
}

pub struct Hart {
    pub id: u64,
    pub x: [u64; 32],
    pub pc: u64,
    pub mode: Mode,
    pub cycle: u64,
    pub retired: u64,
    pub csr: Csrs,
    /// A `wfi` holds until an interrupt is pending.
    pub waiting: bool,
    reservation: Option<u64>,
}

fn sext(v: u64, bits: u32) -> u64 {
    ((v << (64 - bits)) as i64 >> (64 - bits)) as u64
}

fn w(v: u64) -> u64 {
    v as i32 as i64 as u64
}

fn imm_i(i: u32) -> u64 {
    sext(u64::from(i >> 20), 12)
}

fn imm_s(i: u32) -> u64 {
    sext(u64::from((i >> 25) << 5 | (i >> 7) & 0x1F), 12)
}

fn imm_b(i: u32) -> u64 {
    let v =
        (i >> 31) << 12 | ((i >> 7) & 1) << 11 | ((i >> 25) & 0x3F) << 5 | ((i >> 8) & 0xF) << 1;
    sext(u64::from(v), 13)
}

fn imm_j(i: u32) -> u64 {
    let v = (i >> 31) << 20
        | ((i >> 12) & 0xFF) << 12
        | ((i >> 20) & 1) << 11
        | ((i >> 21) & 0x3FF) << 1;
    sext(u64::from(v), 21)
}

fn mulhu(a: u64, b: u64) -> u64 {
    ((u128::from(a) * u128::from(b)) >> 64) as u64
}

fn mulh(a: u64, b: u64) -> u64 {
    ((i128::from(a as i64) * i128::from(b as i64)) >> 64) as u64
}

fn mulhsu(a: u64, b: u64) -> u64 {
    ((i128::from(a as i64) * i128::from(b)) >> 64) as u64
}

fn div(a: u64, b: u64) -> u64 {
    match b {
        0 => u64::MAX,
        _ => (a as i64).wrapping_div(b as i64) as u64,
    }
}

fn rem(a: u64, b: u64) -> u64 {
    match b {
        0 => a,
        _ => (a as i64).wrapping_rem(b as i64) as u64,
    }
}

impl Hart {
    pub fn new(id: u64, pc: u64) -> Hart {
        Hart {
            id,
            x: [0; 32],
            pc,
            mode: Mode::Machine,
            cycle: 0,
            retired: 0,
            csr: Csrs {
                mtimecmp: u64::MAX,
                ..Csrs::default()
            },
            waiting: false,
            reservation: None,
        }
    }

    fn set(&mut self, rd: usize, v: u64) {
        if rd != 0 {
            self.x[rd] = v;
        }
    }

    fn load(&mut self, va: u64, bytes: u32, bus: &mut dyn Bus) -> Result<u64, Trap> {
        if !va.is_multiple_of(u64::from(bytes)) {
            return Err(Trap { cause: 4, tval: va });
        }
        let pa = self.translate(va, Access::Load, bus)?;
        bus.load(pa, bytes).map_err(|_| Trap { cause: 5, tval: va })
    }

    fn store(&mut self, va: u64, bytes: u32, v: u64, bus: &mut dyn Bus) -> Result<(), Trap> {
        if !va.is_multiple_of(u64::from(bytes)) {
            return Err(Trap { cause: 6, tval: va });
        }
        let pa = self.translate(va, Access::Store, bus)?;
        if self.reservation == Some(pa) {
            self.reservation = None;
        }
        bus.store(pa, bytes, v)
            .map_err(|_| Trap { cause: 7, tval: va })
    }

    /// Takes `t` where its delegation says, from the current mode.
    fn take(&mut self, cause: u64, tval: u64) {
        let code = cause & !INTERRUPT;
        let bit = 1u64.checked_shl(code as u32).unwrap_or(0);
        let delegated = if cause & INTERRUPT != 0 {
            self.csr.mideleg & bit != 0
        } else {
            self.csr.medeleg & bit != 0
        };
        let st = self.csr.mstatus;
        if self.mode != Mode::Machine && delegated && !mutant::on("emu.trap/no-delegation") {
            self.csr.sepc = self.pc;
            // The sources sit at machine positions and trap with the supervisor cause.
            self.csr.scause = if cause & INTERRUPT != 0 {
                INTERRUPT | (code / 4 * 4 + 1)
            } else {
                cause
            };
            self.csr.stval = tval;
            let spp = u64::from(self.mode == Mode::Supervisor);
            let spie = (st & SIE_BIT) << 4;
            self.csr.mstatus = (st & !(SPP_BIT | SPIE_BIT | SIE_BIT)) | spp << 8 | spie;
            self.mode = Mode::Supervisor;
            self.pc = self.csr.stvec & !3;
        } else {
            self.csr.mepc = self.pc;
            self.csr.mcause = cause;
            self.csr.mtval = tval;
            let mpie = (st & MIE_BIT) << 4;
            let mpp = (self.mode as u64) << MPP_SHIFT;
            self.csr.mstatus = (st & !(3 << MPP_SHIFT | MPIE_BIT | MIE_BIT)) | mpp | mpie;
            self.mode = Mode::Machine;
            self.pc = self.csr.mtvec & !3;
        }
        self.waiting = false;
    }

    fn interrupt(&self) -> Option<u64> {
        let pending = self.csr.mie & self.csr.mip;
        if pending == 0 {
            return None;
        }
        let st = self.csr.mstatus;
        let m_on = self.mode < Mode::Machine || st & MIE_BIT != 0;
        let s_on =
            self.mode < Mode::Supervisor || (self.mode == Mode::Supervisor && st & SIE_BIT != 0);
        for code in [11u64, 3, 7, 9, 1, 5] {
            let bit = 1 << code;
            if pending & bit == 0 {
                continue;
            }
            let delegated = self.csr.mideleg & bit != 0;
            if (!delegated && m_on) || (delegated && s_on) {
                return Some(INTERRUPT | code);
            }
        }
        None
    }

    fn ret(&mut self, from: Mode) {
        let st = self.csr.mstatus;
        if from == Mode::Machine {
            let mpp = st >> MPP_SHIFT & 3;
            let mie = if mutant::on("emu.ret/drops-mie") {
                0
            } else {
                (st & MPIE_BIT) >> 4
            };
            self.csr.mstatus = (st & !(MIE_BIT | 3 << MPP_SHIFT)) | mie | MPIE_BIT;
            self.mode = match mpp {
                3 => Mode::Machine,
                1 => Mode::Supervisor,
                _ => Mode::User,
            };
            self.pc = self.csr.mepc;
        } else {
            let spp = st & SPP_BIT != 0;
            let sie = (st & SPIE_BIT) >> 4;
            self.csr.mstatus = (st & !(SIE_BIT | SPP_BIT)) | sie | SPIE_BIT;
            self.mode = if spp { Mode::Supervisor } else { Mode::User };
            self.pc = self.csr.sepc;
        }
    }

    /// One instruction, or one held cycle, or a trap taken.
    pub fn step(&mut self, bus: &mut dyn Bus) {
        self.cycle += 1;
        let line = |on: bool, bit: u64, mip: u64| if on { mip | bit } else { mip & !bit };
        let due = self.cycle >= self.csr.mtimecmp && !mutant::on("emu.timer/never");
        let mip = line(due, TIMER_BIT, self.csr.mip);
        self.csr.mip = line(bus.tick(self.cycle), EXTERNAL_BIT, mip);
        if let Some(cause) = self.interrupt() {
            return self.take(cause, 0);
        }
        if self.waiting {
            self.waiting = self.csr.mie & self.csr.mip == 0 && !mutant::on("emu.wfi/runs-on");
            return;
        }
        let pc = self.pc;
        let fetched = self
            .translate(pc, Access::Fetch, bus)
            .and_then(|pa| bus.load(pa, 4).map_err(|_| Trap { cause: 1, tval: pc }));
        let done = fetched.and_then(|i| self.execute(i as u32, bus));
        match done {
            Ok(next) => {
                self.pc = next;
                self.retired += 1;
            }
            Err(t) => self.take(t.cause, t.tval),
        }
    }

    fn execute(&mut self, i: u32, bus: &mut dyn Bus) -> Result<u64, Trap> {
        let rd = (i >> 7 & 0x1F) as usize;
        let f3 = i >> 12 & 7;
        let (rs1, rs2) = ((i >> 15 & 0x1F) as usize, (i >> 20 & 0x1F) as usize);
        let f7 = i >> 25;
        let (a, b) = (self.x[rs1], self.x[rs2]);
        let illegal = Trap {
            cause: CAUSE_ILLEGAL,
            tval: u64::from(i),
        };
        let next = self.pc.wrapping_add(4);
        match i & 0x7F {
            0x37 => self.set(rd, sext(u64::from(i & 0xFFFF_F000), 32)),
            0x17 => self.set(
                rd,
                self.pc.wrapping_add(sext(u64::from(i & 0xFFFF_F000), 32)),
            ),
            0x6F => {
                self.set(rd, next);
                return Ok(self.pc.wrapping_add(imm_j(i)));
            }
            0x67 => {
                let to = a.wrapping_add(imm_i(i)) & !1;
                self.set(rd, next);
                return Ok(to);
            }
            0x63 => {
                let taken = match f3 {
                    0 => a == b,
                    1 => a != b,
                    4 => (a as i64) < b as i64,
                    5 => a as i64 >= b as i64,
                    6 => a < b,
                    7 => a >= b,
                    _ => return Err(illegal),
                };
                if taken {
                    return Ok(self.pc.wrapping_add(imm_b(i)));
                }
            }
            0x03 => {
                let at = a.wrapping_add(imm_i(i));
                let bytes = 1 << (f3 & 3);
                let v = self.load(at, bytes, bus)?;
                let v = match f3 {
                    0 => sext(v, 8),
                    1 => sext(v, 16),
                    2 => sext(v, 32),
                    3..=6 => v,
                    _ => return Err(illegal),
                };
                self.set(rd, v);
            }
            0x23 => {
                if f3 > 3 {
                    return Err(illegal);
                }
                self.store(a.wrapping_add(imm_s(i)), 1 << f3, b, bus)?;
            }
            0x13 => {
                let imm = imm_i(i);
                let sh = i >> 20 & 0x3F;
                let v = match f3 {
                    0 => a.wrapping_add(imm),
                    1 => a << sh,
                    2 => u64::from((a as i64) < imm as i64),
                    3 => u64::from(a < imm),
                    4 => a ^ imm,
                    5 if i >> 30 & 1 == 0 => a >> sh,
                    5 => ((a as i64) >> sh) as u64,
                    6 => a | imm,
                    _ => a & imm,
                };
                self.set(rd, v);
            }
            0x1B => {
                let imm = imm_i(i);
                let sh = i >> 20 & 0x1F;
                let v = match f3 {
                    0 => w(a.wrapping_add(imm)),
                    1 => w(a << sh),
                    5 if i >> 30 & 1 == 0 => w(((a as u32) >> sh).into()),
                    5 => ((a as i32) >> sh) as i64 as u64,
                    _ => return Err(illegal),
                };
                self.set(rd, v);
            }
            0x33 if f7 == 1 => {
                let v = match f3 {
                    0 => a.wrapping_mul(b),
                    1 => mulh(a, b),
                    2 => mulhsu(a, b),
                    3 => mulhu(a, b),
                    4 => div(a, b),
                    5 => a.checked_div(b).unwrap_or(u64::MAX),
                    6 => rem(a, b),
                    _ => a.checked_rem(b).unwrap_or(a),
                };
                self.set(rd, v);
            }
            0x33 => {
                let sh = (b & 0x3F) as u32;
                let v = match (f3, f7) {
                    (0, 0) => a.wrapping_add(b),
                    (0, 0x20) => a.wrapping_sub(b),
                    (1, 0) => a << sh,
                    (2, 0) => u64::from((a as i64) < b as i64),
                    (3, 0) => u64::from(a < b),
                    (4, 0) => a ^ b,
                    (5, 0) => a >> sh,
                    (5, 0x20) => ((a as i64) >> sh) as u64,
                    (6, 0) => a | b,
                    (7, 0) => a & b,
                    _ => return Err(illegal),
                };
                self.set(rd, v);
            }
            0x3B if f7 == 1 => {
                let (x, y) = (sext(a, 32), sext(b, 32));
                let (ux, uy) = (a as u32, b as u32);
                let v = match f3 {
                    0 => w(a.wrapping_mul(b)),
                    4 => match y {
                        0 => u64::MAX,
                        _ => w((x as i64).wrapping_div(y as i64) as u64),
                    },
                    5 => w(u64::from(ux.checked_div(uy).unwrap_or(u32::MAX))),
                    6 => match y {
                        0 => x,
                        _ => w((x as i64).wrapping_rem(y as i64) as u64),
                    },
                    7 => w(u64::from(ux.checked_rem(uy).unwrap_or(ux))),
                    _ => return Err(illegal),
                };
                self.set(rd, v);
            }
            0x3B => {
                let sh = (b & 0x1F) as u32;
                let v = match (f3, f7) {
                    (0, 0) => w(a.wrapping_add(b)),
                    (0, 0x20) => w(a.wrapping_sub(b)),
                    (1, 0) => w(a << sh),
                    (5, 0) => w(u64::from((a as u32) >> sh)),
                    (5, 0x20) => ((a as i32) >> sh) as i64 as u64,
                    _ => return Err(illegal),
                };
                self.set(rd, v);
            }
            0x0F => {}
            0x2F => self.atomic(i, a, b, bus)?,
            0x73 => return self.system(i, a, bus),
            _ => return Err(illegal),
        }
        Ok(next)
    }

    fn atomic(&mut self, i: u32, at: u64, b: u64, bus: &mut dyn Bus) -> Result<(), Trap> {
        let rd = (i >> 7 & 0x1F) as usize;
        let bytes = match i >> 12 & 7 {
            2 => 4,
            3 => 8,
            _ => {
                return Err(Trap {
                    cause: CAUSE_ILLEGAL,
                    tval: u64::from(i),
                })
            }
        };
        let word = |v: u64| if bytes == 4 { sext(v, 32) } else { v };
        match i >> 27 {
            2 => {
                let v = self.load(at, bytes, bus)?;
                self.reservation = Some(self.translate(at, Access::Load, bus)?);
                self.set(rd, word(v));
            }
            3 => {
                let pa = self.translate(at, Access::Store, bus)?;
                let held = self.reservation == Some(pa) || mutant::on("emu.atomic/sc-always");
                if held {
                    self.store(at, bytes, b, bus)?;
                }
                self.reservation = None;
                self.set(rd, u64::from(!held));
            }
            op => {
                let old = word(self.load(at, bytes, bus)?);
                let (x, y) = (old, word(b));
                let v = match op {
                    _ if mutant::on("emu.atomic/amo-add-only") => x.wrapping_add(y),
                    0 => x.wrapping_add(y),
                    1 => y,
                    4 => x ^ y,
                    8 => x | y,
                    12 => x & y,
                    16 => (x as i64).min(y as i64) as u64,
                    20 => (x as i64).max(y as i64) as u64,
                    24 => x.min(y),
                    28 => x.max(y),
                    _ => {
                        return Err(Trap {
                            cause: CAUSE_ILLEGAL,
                            tval: u64::from(i),
                        })
                    }
                };
                self.store(at, bytes, v, bus)?;
                self.set(rd, old);
            }
        }
        Ok(())
    }

    fn system(&mut self, i: u32, a: u64, _bus: &mut dyn Bus) -> Result<u64, Trap> {
        let rd = (i >> 7 & 0x1F) as usize;
        let f3 = i >> 12 & 7;
        let csr = i >> 20;
        let next = self.pc.wrapping_add(4);
        if f3 == 0 {
            match (csr, i >> 25) {
                (0, _) => {
                    return Err(Trap {
                        cause: CAUSE_ECALL_U + self.mode as u64,
                        tval: 0,
                    })
                }
                (1, _) => {
                    return Err(Trap {
                        cause: 3,
                        tval: self.pc,
                    })
                }
                (0x102, _) => {
                    self.ret(Mode::Supervisor);
                    return Ok(self.pc);
                }
                (0x302, _) => {
                    self.ret(Mode::Machine);
                    return Ok(self.pc);
                }
                (0x105, _) => self.waiting = self.csr.mie & self.csr.mip == 0,
                (_, 9) => self.reservation = None,
                _ => {
                    return Err(Trap {
                        cause: CAUSE_ILLEGAL,
                        tval: u64::from(i),
                    })
                }
            }
            return Ok(next);
        }
        let operand = if f3 >= 5 { (i >> 15 & 0x1F) as u64 } else { a };
        let rs1 = i >> 15 & 0x1F;
        let old = if f3 & 3 == 1 && rd == 0 {
            0
        } else {
            self.csr_read(csr, i)?
        };
        match f3 & 3 {
            1 => self.csr_write(csr, operand, i)?,
            2 if rs1 != 0 => self.csr_write(csr, old | operand, i)?,
            3 if rs1 != 0 => self.csr_write(csr, old & !operand, i)?,
            _ => {}
        }
        self.set(rd, old);
        Ok(next)
    }
}
