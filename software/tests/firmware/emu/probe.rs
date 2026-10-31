//! The emulated hart against cpuprobe, whose hashes come from a Python reference
//! and pass on silicon: every class of instruction firmware uses, in one image.

use std::path::Path;

use ka_emu::node::Silent;
use ka_emu::{elf, Node};

const CYCLES: u64 = 5_000_000;

#[test]
fn cpuprobe_exits_clean() {
    let Ok(root) = std::env::var("KA_EMU_FIRMWARE") else {
        return;
    };
    let bytes = std::fs::read(Path::new(&root).join("cpuprobe.elf")).unwrap();
    let mut node = Node::new(Box::new(Silent));
    node.load(&elf::read(&bytes).unwrap()).unwrap();
    let exit = node.run(CYCLES);
    println!("{}", String::from_utf8_lossy(&node.soc.console));
    println!(
        "exit {exit:?} after {} cycles, pc {:#x}",
        node.hart.cycle, node.hart.pc
    );
    assert_eq!(exit, Some(0));
}
