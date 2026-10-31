//! Sv39 address translation.

use crate::bus::Bus;
use crate::hart::{Hart, Mode, Trap};
use crate::mutant;

const PAGE_BITS: u32 = 12;
const LEVELS: u32 = 3;
const SV39: u64 = 8;
const V: u64 = 1;
const R: u64 = 2;
const W: u64 = 4;
const X: u64 = 8;
const U: u64 = 16;
const A: u64 = 64;
const D: u64 = 128;
const SUM: u64 = 1 << 18;
const MXR: u64 = 1 << 19;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Access {
    Fetch,
    Load,
    Store,
}

impl Access {
    fn page_fault(self) -> u64 {
        match self {
            Access::Fetch => 12,
            Access::Load => 13,
            Access::Store => 15,
        }
    }
}

impl Hart {
    pub(crate) fn translate(&mut self, va: u64, a: Access, bus: &mut dyn Bus) -> Result<u64, Trap> {
        let effective = match (a, self.mode) {
            (Access::Fetch, m) => m,
            (_, Mode::Machine) if self.csr.mstatus >> 17 & 1 != 0 => {
                match self.csr.mstatus >> 11 & 3 {
                    0 => Mode::User,
                    1 => Mode::Supervisor,
                    _ => Mode::Machine,
                }
            }
            (_, m) => m,
        };
        if effective == Mode::Machine || self.csr.satp >> 60 != SV39 {
            return Ok(va);
        }
        let fault = Trap {
            cause: a.page_fault(),
            tval: va,
        };
        let mut table = (self.csr.satp & ((1 << 44) - 1)) << PAGE_BITS;
        for level in (0..LEVELS).rev() {
            let shift = PAGE_BITS + 9 * level;
            let index = (va >> shift) & 0x1FF;
            let at = table + index * 8;
            let pte = bus.load(at, 8).map_err(|_| fault)?;
            if pte & V == 0 || (pte & R == 0 && pte & W != 0) {
                return Err(fault);
            }
            let next = (pte >> 10) << PAGE_BITS;
            if pte & (R | X) == 0 {
                table = next;
                continue;
            }
            let user = pte & U != 0;
            let mstatus = self.csr.mstatus;
            let allowed = match a {
                Access::Fetch => pte & X != 0,
                Access::Load => pte & R != 0 || (mstatus & MXR != 0 && pte & X != 0),
                Access::Store => pte & W != 0,
            };
            let mode_ok = match effective {
                Mode::User => user || mutant::on("emu.vm/no-user"),
                _ => !user || (a != Access::Fetch && mstatus & SUM != 0),
            };
            let aligned = next & ((1 << shift) - 1) == 0;
            let dirty = a != Access::Store || pte & D != 0 || mutant::on("emu.vm/no-dirty");
            if !allowed || !mode_ok || !aligned || pte & A == 0 || !dirty {
                return Err(fault);
            }
            if mutant::on("emu.vm/page-offset-only") {
                return Ok(next | (va & ((1 << PAGE_BITS) - 1)));
            }
            return Ok(next | (va & ((1 << shift) - 1)));
        }
        Err(fault)
    }
}
