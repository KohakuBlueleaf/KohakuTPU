//! Operand-axis folds retain every input word and every row's destination.

use ktpu_isa::numerics::f16_saturating;
use ktpu_l2::{compile_l1, Program, Search, PASSES};
use ktpu_sim::l1::{run, Binds};
use ktpu_sim::machine::Machine;
use ktpu_sim::Dram;

use super::common::card;

#[test]
fn streamed_statistic_folds_visit_every_operand_and_row() {
    let mut source = String::from(
        "target ktpu.v9\nfn fold.l2(x: f16[64, 128] @dram, sums: f16[128] @dram, maxima: f16[128] @dram)\n",
    );
    for (op, output) in [("add", "sums"), ("max", "maxima")] {
        source.push_str(
            "    par c in 0..2 on vc[c]\n        for t in 0..4\n            l0 = load x[0, c*64+t*16:+16] : f16[16] @l1\n            v0 = unpack l0 : f32[16] @reg\n",
        );
        let mut acc = "v0".to_string();
        for key in 1..64 {
            source.push_str(&format!(
                "            l{key} = load x[{key}, c*64+t*16:+16] : f16[16] @l1\n            v{key} = unpack l{key} : f32[16] @reg\n            z{key} = {op} {acc}, v{key} : f32[16] @reg\n",
            ));
            acc = format!("z{key}");
        }
        source.push_str(&format!(
            "            p = pack {acc} : f16[16] @l1\n            store {output}[c*64+t*16:+16] <- p\n",
        ));
    }
    let program = Program::parse(&source, "statistic-fold", Some("fold")).expect("fold");
    let compiled = compile_l1(&program, &card(), &PASSES, true, &Search::default())
        .expect("stream the operand axis");
    let binds = Binds::from([
        ("x".into(), 4096),
        ("sums".into(), 24576),
        ("maxima".into(), 28672),
    ]);
    let tag = |key: usize, row: usize| ((key * 13 + row * 7) % 127) as i32 - 63;
    let mut memory = Dram::new(32768);
    for key in 0..64 {
        for row in 0..128 {
            let at = binds["x"] as usize + 2 * (key * 128 + row);
            let value = f16_saturating(f64::from(tag(key, row)));
            memory.bytes[at..at + 2].copy_from_slice(&value.to_le_bytes());
        }
    }
    let mut machine = Machine::new(memory, 2, 4, 128, 7);
    run(&compiled.l1, "fold", &binds, &mut machine).expect("fold execution");
    for row in 0..128 {
        let sum: i32 = (0..64).map(|key| tag(key, row)).sum();
        let maximum = (0..64).map(|key| tag(key, row)).max().expect("operands");
        for (port, value) in [("sums", sum), ("maxima", maximum)] {
            let at = binds[port] as usize + 2 * row;
            let expected = f16_saturating(f64::from(value)).to_le_bytes();
            assert_eq!(
                &machine.mem.bytes[at..at + 2],
                &expected,
                "{port} row {row}: the integer tags identify the source words"
            );
        }
    }
}
