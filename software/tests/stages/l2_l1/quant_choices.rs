//! Complete pack schedules preserve packed bytes and the finite contract.
use ktpu_l1::text::Module;
use ktpu_l2::{compile_l1, Program, Search, PASSES};
use ktpu_sim::l1::{run, Binds};
use ktpu_sim::l2::view::Buf as SimBuf;
use ktpu_sim::machine::Machine;
use ktpu_sim::num::mx7;
use ktpu_sim::Dram;

use super::common::card;

fn lower(source: &str) -> Module {
    let p = Program::parse(source, "quant choices", Some("choose_pack")).expect("L2");
    compile_l1(&p, &card(), &PASSES, true, &Search::default())
        .expect("pack lowering")
        .l1
}

fn execute(module: &Module, rows: usize, nan: bool) -> Vec<u8> {
    let binds = Binds::from([
        ("x".into(), 4096),
        ("q".into(), 65536),
        ("status".into(), 114688),
    ]);
    let mut memory = Dram::new(131072);
    for i in 0..rows * 96 {
        let bits = if nan && i == (rows - 1) * 96 + 40 {
            0x7e00u16
        } else {
            0x2800 | ((i * 37) as u16 & 0x3ff) | (((i / 7) as u16 & 1) << 15)
        };
        let at = 4096 + 2 * i;
        memory.bytes[at..at + 2].copy_from_slice(&bits.to_le_bytes());
    }
    memory
        .watch_writes(65536..65536 + rows * 64)
        .expect("packed writes");
    let mut machine = Machine::new(memory, 2, 4, 128, 7);
    let result = run(module, "choose_pack", &binds, &mut machine);
    if nan {
        assert!(result
            .expect_err("nonfinite contract")
            .to_string()
            .contains("assertion"));
    } else {
        result.expect("finite pack");
        assert!(machine
            .mem
            .write_counts()
            .expect("write census")
            .iter()
            .all(|&n| n == 1));
    }
    machine.mem.bytes[65536..65536 + rows * 64].to_vec()
}

#[test]
fn mapped_whole_words_unpack_into_their_final_quant_chunks() {
    let mapped = r#"target ktpu.v9
fn choose_pack.l2(x: f16[16,96] @dram, q: mx7a[16,64] @dram(gm=4,nk=1), status: f16[128] @dram)
    quant q <- x backend=vector_mx7 status=status finite_batch=4 overlap=1 pipeline=1 zero_classifier=1 read_map=((1536,0,0,1024,((16,96),(64,1)),((16,64),(64,1))),) read_shape=(64,0,0,16,64)
    quant_check status
"#;
    let ordinary = r#"target ktpu.v9
fn choose_pack.l2(x: f16[16,96] @dram, q: mx7a[16,64] @dram(gm=4,nk=1), status: f16[128] @dram)
    quant q <- x[0:+16,0:+64] backend=vector_mx7 status=status finite_batch=4 overlap=1 pipeline=1 zero_classifier=1 stream_batch=0 classifier_banks=0
    quant_check status
"#;
    let selected = lower(mapped);
    let reference = lower(ordinary);
    assert!(!selected
        .source
        .lines()
        .any(|line| line.trim_start().starts_with("vsel ")));
    for nan in [false, true] {
        assert_eq!(execute(&selected, 16, nan), execute(&reference, 16, nan));
    }
}

#[test]
fn mapped_entries_share_a_source_tile_before_packing_and_draining() {
    let source = r#"target ktpu.v9
fn choose_pack.l2(x: f16[196,96] @dram, q: mx7a[196,64] @dram(gm=49,nk=1), status: f16[128] @dram)
    quant q <- x backend=vector_mx7 status=status finite_batch=4 overlap=1 pipeline=1 zero_classifier=1 read_map=((18816,0,0,12544,((196,96),(64,1)),((196,64),(64,1))),) read_shape=(64,0,0,196,64)
    quant_check status
"#;
    for descriptors in [4, 8] {
        let machine = card()
            .with_fact("vc.descriptors", descriptors.into())
            .expect("descriptor variant");
        let lower = |policy: &str| {
            let text = source.replace("read_shape=", &format!("{policy} read_shape="));
            let p =
                Program::parse(&text, "source tile descriptors", Some("choose_pack")).expect("L2");
            compile_l1(&p, &machine, &PASSES, true, &Search::default())
                .expect("pack lowering")
                .l1
        };
        let reference = lower("source_tiles=0");
        let tiled = lower("source_tiles=1");
        let selected = lower("");
        let target = ktpu_l2::target::Target::of(&machine);
        let price = |module: &Module| {
            ktpu_l2::cost::cycles(module, "choose_pack", &target).expect("tile price")
        };
        assert!(price(&tiled) < price(&reference));
        assert!(price(&selected) <= price(&tiled));
        for nan in [false, true] {
            assert_eq!(execute(&tiled, 196, nan), execute(&reference, 196, nan));
        }
    }
}

#[test]
fn transposed_source_capture_prefetches_before_quant_entry_completion() {
    let source = r#"target ktpu.v9
fn transpose_pack.l2(x: f16[64,64] @dram, q: mx7a[64,64] @dram(gm=16,nk=1), status: f16[128] @dram)
    quant q <- x backend=vector_mx7 status=status finite_batch=4 overlap=1 pipeline=1 zero_classifier=1 read_map=((4096,0,0,4096,((64,1),(64,64)),((64,64),(64,1))),) read_shape=(64,0,0,64,64)
    quant_check status
"#;
    let card = card();
    let compile = |policy: i32| {
        let text = source.replace("read_shape=", &format!("source_tiles={policy} read_shape="));
        let p =
            Program::parse(&text, "transposed tile pipeline", Some("transpose_pack")).expect("L2");
        compile_l1(&p, &card, &PASSES, true, &Search::default())
            .expect("lowered tile")
            .l1
    };
    let reference = compile(0);
    let tiled = compile(1);
    let target = ktpu_l2::target::Target::of(&card);
    let price = |module| ktpu_l2::cost::cycles(module, "transpose_pack", &target).expect("price");
    assert!(price(&tiled) < price(&reference));
    let body = tiled.body("transpose_pack", "l1").expect("body");
    let (_, ty) = body
        .params
        .iter()
        .find(|(name, _)| name == "q")
        .expect("packed output");
    let output = SimBuf::of(ty, 16384).expect("MX7 address map");
    let input: Vec<u16> = (0..4096usize)
        .map(|i| 0x2800 | ((i * 37) as u16 & 0x3ff) | (((i / 7) as u16 & 1) << 15))
        .collect();
    let mut expected = vec![0u8; 4096];
    for group in 0..16i64 {
        for block in 0..2i64 {
            let values = std::array::from_fn(|row| {
                std::array::from_fn(|k| {
                    input[((block * 32 + k as i64) * 64 + group * 4 + row as i64) as usize]
                })
            });
            let at = (output.entry(group, block).expect("entry address") - 16384) as usize;
            for (word, bytes) in mx7::entry(&values).write(false).iter().enumerate() {
                expected[at + word * 32..at + (word + 1) * 32].copy_from_slice(bytes);
            }
        }
    }
    for nan in [false, true] {
        let mut memory = Dram::new(32768);
        for (i, &bits) in input.iter().enumerate() {
            let bits = if nan && i == 61 * 64 + 53 {
                0x7e00
            } else {
                bits
            };
            memory.bytes[4096 + 2 * i..4098 + 2 * i].copy_from_slice(&bits.to_le_bytes());
        }
        memory.watch_writes(16384..20480).expect("packed census");
        let mut machine = Machine::new(memory, 2, 4, 128, 7);
        let result = run(
            &tiled,
            "transpose_pack",
            &Binds::from([
                ("x".into(), 4096),
                ("q".into(), 16384),
                ("status".into(), 24576),
            ]),
            &mut machine,
        );
        if nan {
            assert!(result
                .expect_err("nonfinite transpose")
                .to_string()
                .contains("assertion"));
        } else {
            result.expect("finite transpose");
            assert_eq!(&machine.mem.bytes[16384..20480], expected.as_slice());
            assert!(machine
                .mem
                .write_counts()
                .expect("writes")
                .iter()
                .all(|&n| n == 1));
        }
    }
}

#[test]
fn entry_major_gathers_unpack_whole_quant_values() {
    let source = r#"target ktpu.v9
fn choose_pack.l2(x: f16[196,96] @dram, q: mx7a[196,64] @dram(gm=49,nk=1), status: f16[128] @dram)
    quant q <- x[0:+196,0:+64] backend=vector_mx7 status=status finite_batch=4 overlap=1 pipeline=1 zero_classifier=1 stream_axes=1 fill_overlap=1 zero_reuse=1 stream_batch=0 classifier_banks=0
    quant_check status
"#;
    let row_major = lower(&source.replace("stream_axes=1", "stream_axes=1 entry_major=0"));
    let entry_major = lower(&source.replace("stream_axes=1", "stream_axes=1 entry_major=1"));
    let sliced_unpack = |module: &Module| {
        module.source.lines().any(|line| {
            let line = line.trim_start();
            line.starts_with("vunpk ")
                && line.split("<-").next().is_some_and(|lhs| lhs.contains('['))
        })
    };
    assert!(sliced_unpack(&row_major));
    assert!(!sliced_unpack(&entry_major));
    for nan in [false, true] {
        assert_eq!(
            execute(&entry_major, 196, nan),
            execute(&row_major, 196, nan)
        );
    }
    let selected = lower(source);
    let target = ktpu_l2::target::Target::of(&card());
    let price = |module| ktpu_l2::cost::cycles(module, "choose_pack", &target).expect("price");
    assert!(price(&selected) <= price(&row_major));
    assert!(price(&selected) <= price(&entry_major));
}
