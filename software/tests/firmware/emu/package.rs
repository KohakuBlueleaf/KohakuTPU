//! The package interpreter on the emulated node, the units a double that answers
//! each send: words in order, each unit's credit kept, a unit's fault reported.

use std::cell::RefCell;
use std::path::Path;
use std::rc::Rc;

use ka_emu::node::{Mesh, Send};
use ka_emu::{elf, Node};
use ktpu_isa::Machine as Card;
use ktpu_l0::format::{type_code, Step, Unit};
use ktpu_l0::{Facts, Op, Package, Word};

const CYCLES: u64 = 4_000_000;
const QUEUE: u64 = 0x1040_0000;
const REGION: u64 = 1 << 20;
const PACKAGE: u64 = 0x1060_0000;
const SQ_N: u64 = 8;
const CQ_N: u64 = 8;
const STDOUT: u64 = 4096;
const STDIN: u64 = 512;

const BOOT_MAGIC: u64 = 0x31544f4f42414b;
const REGION_QUEUE: u64 = 2;
const REGIONS_AT: u64 = 8 * (9 + 16);
const Q_MAGIC: u64 = 0x314555455551414B;
const Q_VERSION: u64 = 4;
const FW: u64 = 0x040;
const SQ_TAIL: u64 = 0x080;
const CQ_TAIL: u64 = 0x100;
const SQ: u64 = 0x440;
const ST_READY: u64 = 1;
const RUN: u64 = 1;
const TIMEOUT: u64 = 1_000_000;

const ST_BAD_MAGIC: u64 = 0x10;
const ST_UNIT_FAULT: u64 = 0x20;
const SIG_DONE: u64 = 0;
const SIG_FAULT: u64 = 4;
const UNIT: (u64, u64) = (1, 3);
const CREDIT: u64 = 2;

/// Answers every send as `code`.
struct Units {
    code: u64,
    seen: Rc<RefCell<Vec<Send>>>,
}

impl Mesh for Units {
    fn send(&mut self, s: Send) -> Vec<u64> {
        self.seen.borrow_mut().push(s);
        vec![UNIT.0 << 48 | UNIT.1 << 52 | self.code << 40]
    }
}

fn card() -> Card {
    let crate_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let root = crate_dir.ancestors().nth(5).expect("the repository");
    Card::load(root.join("machines/multimesh_v9t8.json")).expect("the machine")
}

fn package(words: u64) -> Vec<u8> {
    let vc = type_code("VC").expect("a unit type");
    let unit = Unit {
        type_code: vc,
        x: UNIT.0,
        y: UNIT.1,
        mesh: 0,
        credit: CREDIT,
        fetch: 0,
    };
    let payloads = (0..words)
        .map(|i| Word([i + 1, i + 2, i + 3, i + 4]))
        .collect();
    let steps = vec![
        Step::new(Op::Dispatch, 0, words, 0, 0),
        Step::new(Op::Await, 0, words, 0, 0),
        Step::bare(Op::Barrier),
        Step::bare(Op::End),
    ];
    let pkg = Package {
        units: vec![unit],
        steps,
        payloads,
        ..Package::default()
    };
    pkg.to_bytes(&Facts::of(&card())).expect("a package")
}

/// A ready node, its dispatcher serving; None without firmware built.
fn ready(code: u64, seen: Rc<RefCell<Vec<Send>>>) -> Option<Node> {
    let root = std::env::var("KA_EMU_FIRMWARE").ok()?;
    let bytes = std::fs::read(Path::new(&root).join("kohakuaccel_node.elf")).unwrap();
    let mut node = Node::new(Box::new(Units { code, seen }));
    node.load(&elf::read(&bytes).unwrap()).unwrap();
    for (off, v) in [
        (0, BOOT_MAGIC),
        (8, 3),
        (16, QUEUE),
        (40, 1 << 24),
        (REGIONS_AT, QUEUE),
        (REGIONS_AT + 8, REGION),
        (REGIONS_AT + 16, REGION_QUEUE),
    ] {
        node.soc.spad[off as usize..][..8].copy_from_slice(&v.to_le_bytes());
    }
    let s = &mut node.soc;
    s.card_set_u64(QUEUE, Q_MAGIC);
    s.card_set_u64(QUEUE + 8, Q_VERSION);
    s.card_set_u64(QUEUE + 16, SQ_N | CQ_N << 16);
    s.card_set_u64(QUEUE + 24, STDOUT | STDIN << 32);
    assert!(
        node.run_until(CYCLES, |s| s.card_u64(QUEUE + FW) == ST_READY),
        "never ready"
    );
    Some(node)
}

fn run(node: &mut Node, raw: &[u8]) -> u64 {
    node.soc.card_write(PACKAGE, raw);
    let e = QUEUE + SQ;
    let s = &mut node.soc;
    s.card_set_u64(e, RUN);
    s.card_set_u64(e + 8, 0x77);
    s.card_set_u64(e + 16, PACKAGE);
    s.card_set_u64(e + 24, raw.len() as u64);
    s.card_set_u64(e + 40, TIMEOUT);
    s.card_set_u64(QUEUE + SQ_TAIL, 1);
    assert!(
        node.run_until(CYCLES, |s| s.card_u64(QUEUE + CQ_TAIL) == 1),
        "no completion"
    );
    let done = e + SQ_N * 64;
    assert_eq!(node.soc.card_u64(done), 0x77);
    node.soc.card_u64(done + 8) & 0xFFFF
}

#[test]
fn a_package_sends_its_words_in_order() {
    let seen = Rc::default();
    let Some(mut node) = ready(SIG_DONE, Rc::clone(&seen)) else {
        return;
    };
    assert_eq!(run(&mut node, &package(6)), 0);
    let sends = seen.borrow();
    assert_eq!(sends.len(), 6);
    for (i, s) in sends.iter().enumerate() {
        let i = i as u64;
        assert_eq!(s.payload, [i + 1, i + 2, i + 3, i + 4]);
        assert_eq!(s.dst, UNIT.1 << 8 | UNIT.0);
    }
}

#[test]
fn a_unit_fault_ends_the_run() {
    let seen = Rc::default();
    let Some(mut node) = ready(SIG_FAULT, Rc::clone(&seen)) else {
        return;
    };
    assert_eq!(run(&mut node, &package(6)), ST_UNIT_FAULT);
    assert!(seen.borrow().len() < 6, "words went on past the fault");
}

#[test]
fn a_wrong_magic_is_refused_before_any_send() {
    let seen = Rc::default();
    let Some(mut node) = ready(SIG_DONE, Rc::clone(&seen)) else {
        return;
    };
    let mut raw = package(2);
    raw[0] ^= 0xFF;
    assert_eq!(run(&mut node, &raw), ST_BAD_MAGIC);
    assert!(seen.borrow().is_empty());
}
