//! Transposed source windows preserve finite normal/signed-zero lanes and pad invalid lanes.

use ktpu_l2::{compile_l1, Program, Search, PASSES};
use ktpu_sim::l1::{run, Binds};
use ktpu_sim::machine::Machine;
use ktpu_sim::Dram;

use super::common::card;

#[test]
fn shifted_source_words_exchange_at_lower_price_with_exact_lane_order() {
    let source = r#"target ktpu.v9
fn window.l2(x: f16[160,16] @dram, out: f16[64,32] @dram)
    par c in 0..1 on vc[c]
        for t in 0..4
            xl = load x read_map=((2560,1,0,2048,((64,1),(32,80)),((64,32),(32,1))),) read_at=t*32 read_pitch=32 read_stride=1 read_rows=15+t%2 read_cols=16+16*(t%2) : f16[16,32] @l1(gt4)
            v = unpack xl : f32[16,32] @reg(gt4)
            p = pack v : f16[16,32] @l1(gt4)
            store out[t*16:+16,0:+32] <- p
"#;
    let card = card();
    let program = Program::parse(source, "shifted transpose", Some("window")).expect("L2");
    let compile = |policy| {
        let mut search = Search::default();
        search.window_exchange = policy;
        compile_l1(&program, &card, &PASSES, true, &search)
            .expect("mapped window lowering")
            .l1
    };
    let gathered = compile(Some(false));
    let selected = compile(None);
    let target = ktpu_l2::target::Target::of(&card);
    let price = |module| ktpu_l2::cost::cycles(module, "window", &target).expect("complete price");
    assert!(price(&selected) < price(&gathered));
    let binds = Binds::from([("x".into(), 4096), ("out".into(), 12288)]);
    let bits = |i: usize| {
        if i % 17 == 0 {
            ((i & 1) << 15) as u16
        } else {
            0x2800 | ((i * 37) as u16 & 0x3ff) | (((i / 7) as u16 & 1) << 15)
        }
    };
    for (policy, module) in [("selected", &selected), ("gathered", &gathered)] {
        let mut memory = Dram::new(20480);
        for i in 0..2560 {
            let at = binds["x"] as usize + 2 * i;
            memory.bytes[at..at + 2].copy_from_slice(&bits(i).to_le_bytes());
        }
        memory.bytes[12288..16384].fill(0xff);
        let mut machine = Machine::new(memory, 2, 4, 128, 7);
        run(module, "window", &binds, &mut machine).expect("execute exchange");
        for t in 0..4 {
            let (rows, cols) = if t % 2 == 0 { (15, 16) } else { (16, 32) };
            for r in 0..16 {
                for c in 0..32 {
                    let at = binds["out"] as usize + 2 * ((t * 16 + r) * 32 + c);
                    let wanted = if r < rows && c < cols {
                        bits(c * 80 + 1 + t + r)
                    } else {
                        0
                    };
                    assert_eq!(
                        &machine.mem.bytes[at..at + 2],
                        &wanted.to_le_bytes(),
                        "{policy}: trip {t}, row {r}, column {c}"
                    );
                }
            }
        }
    }
}
