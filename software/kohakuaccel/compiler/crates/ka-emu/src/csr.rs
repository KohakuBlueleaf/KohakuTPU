//! Control and status registers: the set firmware and the OS touch.

use crate::hart::{Hart, Trap, CAUSE_ILLEGAL};

pub const MSTATUS: u32 = 0x300;
pub const MEDELEG: u32 = 0x302;
pub const MIDELEG: u32 = 0x303;
pub const MIE: u32 = 0x304;
pub const MTVEC: u32 = 0x305;
pub const MCOUNTEREN: u32 = 0x306;
pub const MSCRATCH: u32 = 0x340;
pub const MEPC: u32 = 0x341;
pub const MCAUSE: u32 = 0x342;
pub const MTVAL: u32 = 0x343;
pub const MIP: u32 = 0x344;
pub const MHARTID: u32 = 0xF14;
pub const MCYCLE: u32 = 0xB00;
pub const MINSTRET: u32 = 0xB02;
pub const SSTATUS: u32 = 0x100;
pub const SIE: u32 = 0x104;
pub const STVEC: u32 = 0x105;
pub const SCOUNTEREN: u32 = 0x106;
pub const SSCRATCH: u32 = 0x140;
pub const SEPC: u32 = 0x141;
pub const SCAUSE: u32 = 0x142;
pub const STVAL: u32 = 0x143;
pub const SIP: u32 = 0x144;
pub const SATP: u32 = 0x180;
pub const CYCLE: u32 = 0xC00;
pub const TIME: u32 = 0xC01;
pub const INSTRET: u32 = 0xC02;
pub const PMPCFG0: u32 = 0x3A0;
pub const PMPADDR0: u32 = 0x3B0;

/// The machine timer's compare (`kos/hw.h` CSR_MTIMECMP).
pub const MTIMECMP: u32 = 0x7C0;

/// The bits of mstatus sstatus shows: SIE, SPIE, SPP, SUM, MXR.
const SSTATUS_MASK: u64 = 1 << 1 | 1 << 5 | 1 << 8 | 1 << 18 | 1 << 19;

#[derive(Clone, Debug, Default)]
pub struct Csrs {
    pub mstatus: u64,
    pub medeleg: u64,
    pub mideleg: u64,
    pub mie: u64,
    pub mip: u64,
    pub mtvec: u64,
    pub mscratch: u64,
    pub mepc: u64,
    pub mcause: u64,
    pub mtval: u64,
    pub stvec: u64,
    pub sscratch: u64,
    pub sepc: u64,
    pub scause: u64,
    pub stval: u64,
    pub satp: u64,
    pub mcounteren: u64,
    pub scounteren: u64,
    pub mtimecmp: u64,
}

impl Hart {
    fn illegal(&self, inst: u32) -> Trap {
        Trap {
            cause: CAUSE_ILLEGAL,
            tval: u64::from(inst),
        }
    }

    pub(crate) fn csr_read(&self, addr: u32, inst: u32) -> Result<u64, Trap> {
        if (addr >> 8 & 3) as u8 > self.mode as u8 {
            return Err(self.illegal(inst));
        }
        let c = &self.csr;
        Ok(match addr {
            MSTATUS => c.mstatus,
            MEDELEG => c.medeleg,
            MIDELEG => c.mideleg,
            MIE => c.mie,
            MIP => c.mip,
            MTVEC => c.mtvec,
            MSCRATCH => c.mscratch,
            MEPC => c.mepc,
            MCAUSE => c.mcause,
            MTVAL => c.mtval,
            MHARTID => self.id,
            MCOUNTEREN => c.mcounteren,
            MCYCLE | CYCLE | TIME => self.cycle,
            MINSTRET | INSTRET => self.retired,
            SSTATUS => c.mstatus & SSTATUS_MASK,
            SIE => c.mie & c.mideleg,
            SIP => c.mip & c.mideleg,
            MTIMECMP => c.mtimecmp,
            STVEC => c.stvec,
            SCOUNTEREN => c.scounteren,
            SSCRATCH => c.sscratch,
            SEPC => c.sepc,
            SCAUSE => c.scause,
            STVAL => c.stval,
            SATP => c.satp,
            PMPCFG0 | PMPADDR0 => 0,
            _ => return Err(self.illegal(inst)),
        })
    }

    pub(crate) fn csr_write(&mut self, addr: u32, v: u64, inst: u32) -> Result<(), Trap> {
        if (addr >> 8 & 3) as u8 > self.mode as u8 || addr >> 10 == 3 {
            return Err(self.illegal(inst));
        }
        let c = &mut self.csr;
        match addr {
            MSTATUS => c.mstatus = v,
            MEDELEG => c.medeleg = v,
            MIDELEG => c.mideleg = v,
            MIE => c.mie = v,
            MIP => c.mip = v,
            MTVEC => c.mtvec = v,
            MSCRATCH => c.mscratch = v,
            MEPC => c.mepc = v & !3,
            MCAUSE => c.mcause = v,
            MTVAL => c.mtval = v,
            MCOUNTEREN => c.mcounteren = v,
            MCYCLE => self.cycle = v,
            MINSTRET => self.retired = v,
            SSTATUS => c.mstatus = (c.mstatus & !SSTATUS_MASK) | (v & SSTATUS_MASK),
            SIE => c.mie = (c.mie & !c.mideleg) | (v & c.mideleg),
            SIP => c.mip = (c.mip & !c.mideleg) | (v & c.mideleg),
            MTIMECMP => c.mtimecmp = v,
            STVEC => c.stvec = v,
            SCOUNTEREN => c.scounteren = v,
            SSCRATCH => c.sscratch = v,
            SEPC => c.sepc = v & !3,
            SCAUSE => c.scause = v,
            STVAL => c.stval = v,
            SATP => c.satp = v,
            PMPCFG0 | PMPADDR0 => {}
            _ => return Err(self.illegal(inst)),
        }
        Ok(())
    }
}
