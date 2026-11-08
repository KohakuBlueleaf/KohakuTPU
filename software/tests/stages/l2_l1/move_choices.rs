//! Raw-word layout traffic is priced without interpreting its element bits.
use ktpu_l1::text::read;
use ktpu_l2::{compile_l1, Program, Search, PASSES};
use ktpu_sim::l1::{run, Binds};
use ktpu_sim::machine::Machine;
use ktpu_sim::Dram;

use super::common::card;

#[test]
fn a_safe_fragmented_move_can_use_raw_vector_dma() {
    let source = r#"target ktpu.v9
fn word_walk.l2(x: f16[2048,16] @dram, out: f16[128,16] @dram)
    copy out <- x walk=((128,512),) dwalk=((128,32),)
"#;
    let card = card();
    let program = Program::parse(source, "word walk", Some("word_walk")).expect("L2");
    let selected = compile_l1(&program, &card, &PASSES, true, &Search::default())
        .expect("copy lowering")
        .l1;
    let mover = read(
        r#"target ktpu.v9
fn word_walk.l1(x: f16[2048,16] @dram, out: f16[128,16] @dram)
    move local=1
        copy w(x, (128,512)) -> w(out, (128,32))
"#,
        "mover alternative",
    )
    .expect("L1");
    let target = ktpu_l2::target::Target::of(&card);
    let price = |module| ktpu_l2::cost::cycles(module, "word_walk", &target).expect("price");
    assert!(selected.source.contains("vfill"));
    assert!(price(&selected) < price(&mover));
    let mut memory = Dram::new(81920);
    for i in 0..65536usize {
        memory.bytes[4096 + i] = (i.wrapping_mul(71) ^ (i >> 8)) as u8;
    }
    let expected: Vec<u8> = (0..128)
        .flat_map(|word| memory.bytes[4096 + word * 512..4096 + word * 512 + 32].to_vec())
        .collect();
    memory.watch_writes(73728..77824).expect("copy census");
    let mut machine = Machine::new(memory, 2, 4, 128, 7);
    run(
        &selected,
        "word_walk",
        &Binds::from([("x".into(), 4096), ("out".into(), 73728)]),
        &mut machine,
    )
    .expect("raw copy");
    assert_eq!(&machine.mem.bytes[73728..77824], expected.as_slice());
    assert!(machine
        .mem
        .write_counts()
        .expect("writes")
        .iter()
        .all(|&n| n == 1));
}

#[test]
fn raw_copy_streams_keep_the_odd_piece_and_overlap_dma_slots() {
    let source = r#"target ktpu.v9
fn stream_walk.l2(x: f16[18432,16] @dram, out: f16[2304,16] @dram)
    copy out <- x walk=((2304,256),) dwalk=((2304,32),)
"#;
    let card = card();
    let lower = |policy: &str| {
        let text = source.replace("dwalk=", &format!("{policy} dwalk="));
        let program = Program::parse(&text, "DMA streams", Some("stream_walk")).expect("L2");
        compile_l1(&program, &card, &PASSES, true, &Search::default())
            .expect("stream lowering")
            .l1
    };
    let selected = lower("");
    let reference = lower("stream_copy=0");
    let blocked = lower("stream_copy=1 queued_copy=0");
    let streamed = lower("stream_copy=1 queued_copy=1");
    assert!(streamed.source.contains("vsync fill on=drain"));
    assert!(!streamed.source.contains("vsync fe on=fill"));
    let target = ktpu_l2::target::Target::of(&card);
    let price = |module| ktpu_l2::cost::cycles(module, "stream_walk", &target).expect("price");
    assert!(price(&streamed) < price(&reference));
    assert!(price(&selected) <= price(&streamed));
    assert!(price(&selected) <= price(&blocked));
    let mut memory = Dram::new(786432);
    for i in 0..589824usize {
        memory.bytes[4096 + i] = (i.wrapping_mul(71) ^ (i >> 8)) as u8;
    }
    let expected: Vec<u8> = (0..2304)
        .flat_map(|word| memory.bytes[4096 + word * 256..4096 + word * 256 + 32].to_vec())
        .collect();
    let output = 655360;
    memory
        .watch_writes(output..output + expected.len())
        .expect("copy census");
    let mut machine = Machine::new(memory, 2, 4, 128, 7);
    run(
        &streamed,
        "stream_walk",
        &Binds::from([("x".into(), 4096), ("out".into(), output as i64)]),
        &mut machine,
    )
    .expect("streamed raw copy");
    assert_eq!(
        &machine.mem.bytes[output..output + expected.len()],
        expected.as_slice()
    );
    assert!(machine
        .mem
        .write_counts()
        .expect("writes")
        .iter()
        .all(|&n| n == 1));
}
