//! The hart against the RISC-V privileged spec, an instruction at a time: what the
//! firmware and the OS never make it show, reservations, wakes and page rights.

use ka_emu::bus::{get, put, Bus, Fault};
use ka_emu::{Hart, Mode};

const NOP: u32 = 0x13;
const WFI: u32 = 0x1050_0073;
const MRET: u32 = 0x3020_0073;
/// `ld x3, 0(x1)`, `sd x2, 0(x1)`, `lr.d x3, (x1)`, `sc.d x4, x2, (x1)`.
const LD: u32 = 1 << 15 | 3 << 12 | 3 << 7 | 0x03;
const SD: u32 = 2 << 20 | 1 << 15 | 3 << 12 | 0x23;
const LR: u32 = 2 << 27 | 1 << 15 | 3 << 12 | 3 << 7 | 0x2F;
const SC: u32 = 3 << 27 | 2 << 20 | 1 << 15 | 3 << 12 | 4 << 7 | 0x2F;

const DATA: u64 = 0x100;
const TRAP: u64 = 0x800;
const ROOT: u64 = 0x1000;
const INTERRUPT: u64 = 1 << 63;
const MTIMER: u64 = 7;
const LOAD_PAGE_FAULT: u64 = 13;
const STORE_PAGE_FAULT: u64 = 15;
const MIE: u64 = 1 << 3;
const MPIE: u64 = 1 << 7;
const MPP: u64 = 3 << 11;
const MTIE: u64 = 1 << 7;
const SV39: u64 = 8 << 60;
const VRWXA: u64 = 1 | 2 | 4 | 8 | 64;
const U: u64 = 16;
const D: u64 = 128;
const GIG: u64 = 1 << 30;

struct Ram(Vec<u8>);

impl Bus for Ram {
    fn load(&mut self, at: u64, bytes: u32) -> Result<u64, Fault> {
        get(&self.0, at as usize, bytes)
    }
    fn store(&mut self, at: u64, bytes: u32, value: u64) -> Result<(), Fault> {
        put(&mut self.0, at as usize, bytes, value)
    }
}

/// A hart at 0 in machine mode over `code`, x1 at `DATA`, x2 = 7.
fn machine(code: &[u32]) -> (Hart, Ram) {
    let mut ram = Ram(vec![0; 1 << 16]);
    for (i, word) in code.iter().enumerate() {
        ram.store(i as u64 * 4, 4, u64::from(*word)).unwrap();
    }
    let mut hart = Hart::new(0, 0);
    hart.x[1] = DATA;
    hart.x[2] = 7;
    hart.csr.mtvec = TRAP;
    (hart, ram)
}

/// The same in user mode, under Sv39: the first gigabyte is user code and data with
/// `first` rights, the second a supervisor's.
fn user(code: &[u32], first: u64) -> (Hart, Ram) {
    let (mut hart, mut ram) = machine(code);
    ram.store(ROOT, 8, first).unwrap();
    ram.store(ROOT + 8, 8, (GIG >> 12) << 10 | VRWXA | D)
        .unwrap();
    hart.csr.satp = SV39 | ROOT >> 12;
    hart.mode = Mode::User;
    (hart, ram)
}

#[test]
fn a_store_conditional_needs_its_reservation() {
    let (mut hart, mut ram) = machine(&[SC, LR, SC]);
    hart.step(&mut ram);
    assert_eq!(hart.x[4], 1);
    assert_eq!(ram.load(DATA, 8), Ok(0));
    hart.step(&mut ram);
    hart.step(&mut ram);
    assert_eq!(hart.x[4], 0);
    assert_eq!(ram.load(DATA, 8), Ok(7));
}

#[test]
fn a_store_ends_a_reservation_on_its_address() {
    let (mut hart, mut ram) = machine(&[LR, SD, SC]);
    (0..3).for_each(|_| hart.step(&mut ram));
    assert_eq!(hart.x[4], 1);
}

#[test]
fn the_timer_interrupts_at_its_compare() {
    let (mut hart, mut ram) = machine(&[NOP; 8]);
    hart.csr.mstatus = MIE;
    hart.csr.mie = MTIE;
    hart.csr.mtimecmp = 3;
    (0..2).for_each(|_| hart.step(&mut ram));
    assert_eq!(hart.csr.mcause, 0);
    hart.step(&mut ram);
    assert_eq!(hart.csr.mcause, INTERRUPT | MTIMER);
    assert_eq!(hart.pc, TRAP);
}

#[test]
fn a_wfi_holds_without_a_pending_interrupt() {
    let (mut hart, mut ram) = machine(&[WFI, NOP]);
    (0..4).for_each(|_| hart.step(&mut ram));
    assert!(hart.waiting);
    assert_eq!(hart.retired, 1);
}

#[test]
fn mret_restores_mie_from_mpie() {
    let (mut hart, mut ram) = machine(&[MRET]);
    hart.csr.mstatus = MPIE | MPP;
    hart.csr.mepc = 0x40;
    hart.step(&mut ram);
    assert_ne!(hart.csr.mstatus & MIE, 0);
    assert_eq!(hart.pc, 0x40);
}

#[test]
fn a_store_needs_the_dirty_bit() {
    let (mut hart, mut ram) = user(&[SD], VRWXA | U);
    hart.step(&mut ram);
    assert_eq!(hart.csr.mcause, STORE_PAGE_FAULT);
    assert_eq!(ram.load(DATA, 8), Ok(0));
}

#[test]
fn a_dirty_user_page_takes_the_store() {
    let (mut hart, mut ram) = user(&[SD], VRWXA | U | D);
    hart.step(&mut ram);
    assert_eq!(hart.csr.mcause, 0);
    assert_eq!(ram.load(DATA, 8), Ok(7));
}

#[test]
fn user_mode_faults_on_a_supervisor_page() {
    let (mut hart, mut ram) = user(&[LD], VRWXA | U | D);
    hart.x[1] = GIG;
    hart.step(&mut ram);
    assert_eq!(hart.csr.mcause, LOAD_PAGE_FAULT);
    assert_eq!(hart.csr.mtval, GIG);
}
