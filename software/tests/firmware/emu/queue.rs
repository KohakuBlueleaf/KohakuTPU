//! The dispatcher image on the emulated node, the host's side played here from
//! `ka/queue/layout.h`: boot, the rings, the refusals, a clean stop.

use std::path::Path;

use ka_emu::node::Silent;
use ka_emu::{elf, Node};

const CYCLES: u64 = 2_000_000;
const QUEUE: u64 = 0x1040_0000;
const REGION: u64 = 1 << 20;
const SQ_N: u64 = 8;
const CQ_N: u64 = 8;
const STDOUT: u64 = 4096;
const STDIN: u64 = 512;

const BOOT_MAGIC: u64 = 0x31544f4f42414b;
const REGION_QUEUE: u64 = 2;
const REGIONS_AT: u64 = 8 * (9 + 16);
const Q_MAGIC: u64 = 0x314555455551414B;
const Q_VERSION: u64 = 4;
const INFO: u64 = 0x000;
const FW: u64 = 0x040;
const SQ_TAIL: u64 = 0x080;
const SQ_HEAD: u64 = 0x0C0;
const CQ_TAIL: u64 = 0x100;
const SQ: u64 = 0x440;
const SO_WR: u64 = 0x180;
const ST_READY: u64 = 1;
const ST_STOPPED: u64 = 2;
const NOP: u64 = 0;
const STOP: u64 = 2;
const EXIT_STOP: u64 = 0x5701000000000000;
const EXIT_BOOTARGS: u64 = 0xE100000000000000;
const BOOT_ERR_MAGIC: u64 = 1;
const BOOT_ERR_QUEUE: u64 = 0x10;

fn dispatcher() -> Option<Node> {
    let root = std::env::var("KA_EMU_FIRMWARE").ok()?;
    let bytes = std::fs::read(Path::new(&root).join("kohakuaccel_node.elf")).unwrap();
    let mut node = Node::new(Box::new(Silent));
    node.load(&elf::read(&bytes).unwrap()).unwrap();
    Some(node)
}

fn boot_word(node: &mut Node, off: u64, v: u64) {
    node.soc.spad[off as usize..][..8].copy_from_slice(&v.to_le_bytes());
}

fn boot_block(node: &mut Node) {
    boot_word(node, 0, BOOT_MAGIC);
    boot_word(node, 8, 3);
    boot_word(node, 16, QUEUE);
    boot_word(node, 40, 1 << 24);
    boot_word(node, REGIONS_AT, QUEUE);
    boot_word(node, REGIONS_AT + 8, REGION);
    boot_word(node, REGIONS_AT + 16, REGION_QUEUE);
}

fn queue_header(node: &mut Node) {
    let s = &mut node.soc;
    s.card_set_u64(QUEUE + INFO, Q_MAGIC);
    s.card_set_u64(QUEUE + INFO + 8, Q_VERSION);
    s.card_set_u64(QUEUE + INFO + 16, SQ_N | CQ_N << 16);
    s.card_set_u64(QUEUE + INFO + 24, STDOUT | STDIN << 32);
}

fn submit(node: &mut Node, tail: u64, op: u64, tag: u64, arg: u64) {
    let e = QUEUE + SQ + (tail % SQ_N) * 64;
    let s = &mut node.soc;
    s.card_set_u64(e, op);
    s.card_set_u64(e + 8, tag);
    s.card_set_u64(e + 16, arg);
    s.card_set_u64(QUEUE + SQ_TAIL, tail + 1);
}

fn completion(node: &mut Node, i: u64) -> (u64, u64) {
    let e = QUEUE + SQ + SQ_N * 64 + (i % CQ_N) * 32;
    (node.soc.card_u64(e), node.soc.card_u64(e + 8))
}

fn stdout(node: &mut Node) -> String {
    let start = (QUEUE + SQ + SQ_N * 64 + CQ_N * 32 + 63) & !63;
    let n = node.soc.card_u64(QUEUE + SO_WR);
    String::from_utf8_lossy(&node.soc.card_read(start, n as usize)).to_string()
}

#[test]
fn a_node_boots_serves_and_stops() {
    let Some(mut node) = dispatcher() else { return };
    boot_block(&mut node);
    queue_header(&mut node);
    assert!(
        node.run_until(CYCLES, |s| s.card_u64(QUEUE + FW) == ST_READY),
        "never ready"
    );

    submit(&mut node, 0, NOP, 0x11, 0);
    submit(&mut node, 1, 5, 0x22, 0);
    assert!(
        node.run_until(CYCLES, |s| s.card_u64(QUEUE + CQ_TAIL) == 2),
        "no completions"
    );
    let (tag, status) = completion(&mut node, 0);
    assert_eq!((tag, status & 0xFFFF), (0x11, 0));
    let (tag, status) = completion(&mut node, 1);
    assert_eq!(tag, 0x22);
    assert_ne!(status & 0xFFFF, 0, "a reserved opcode is refused");
    assert!(
        node.run_until(CYCLES, |s| s.card_u64(QUEUE + SQ_HEAD) == 2),
        "head stands"
    );

    submit(&mut node, 2, STOP, 0x33, 0x1234);
    let exit = node.run(CYCLES);
    println!("{}", stdout(&mut node));
    assert_eq!(exit, Some(EXIT_STOP | 0x1234));
    assert_eq!(node.soc.card_u64(QUEUE + FW), ST_STOPPED);
    assert_eq!(completion(&mut node, 2).0, 0x33);
    assert!(stdout(&mut node).contains("[ka] stop: 2 entries"));
}

#[test]
fn no_boot_block_is_refused() {
    let Some(mut node) = dispatcher() else { return };
    assert_eq!(node.run(CYCLES), Some(EXIT_BOOTARGS | BOOT_ERR_MAGIC));
}

#[test]
fn a_bad_queue_is_refused() {
    let Some(mut node) = dispatcher() else { return };
    boot_block(&mut node);
    queue_header(&mut node);
    node.soc.card_set_u64(QUEUE + INFO + 8, Q_VERSION + 1);
    assert_eq!(node.run(CYCLES), Some(EXIT_BOOTARGS | BOOT_ERR_QUEUE));
}
