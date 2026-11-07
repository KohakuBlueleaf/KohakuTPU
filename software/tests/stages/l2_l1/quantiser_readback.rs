//! FP16 remains observable while its sole MX7 consumer chooses a chunk order.
use ktpu_l2::{compile_l1, Program, Search, PASSES};
use ktpu_sim::l1::{run, Binds};
use ktpu_sim::machine::Machine;
use ktpu_sim::Dram;

use super::common::card;

#[test]
fn quantiser_readback_preserves_both_outputs_and_checked_status() {
    let source = r#"target ktpu.v9
fn readback.l2(x: f16[16,256] @dram, out: f16[16,256] @dram, q0: mx7a[16,256] @dram(gm=4,nk=1), status: f16[128] @dram)
    par c in 0..2 on vc[c]
        for t in 0..2
            xl = load x[c*8+t*4:+4,0:+256] : f16[4,256] @l1(gt4)
            v = unpack xl : f32[4,256] @reg(gt4)
            a = mul v, 1.0003 : f32[4,256] @reg(gt4)
            b = mul a, a : f32[4,256] @reg(gt4)
            d = mul b, a : f32[4,256] @reg(gt4)
            e = add d, a : f32[4,256] @reg(gt4)
            p = pack e : f16[4,256] @l1(gt4)
            store out[c*8+t*4:+4,0:+256] <- p
            rounded = unpack p : f32[4,256] @reg(gt4)
            q = pack rounded status=status[c*64:+64] : mx7a[4,256] @l1
            store q0[c*8+t*4:+4,0:+256] <- q
    quant_check status
"#;
    let card = card();
    let program =
        Program::parse(source, "quantiser readback", Some("readback")).expect("typed readback");
    let compile = || {
        compile_l1(&program, &card, &PASSES, true, &Search::default())
            .expect("readback lowering")
            .l1
    };
    let reference = ktpu::mutant::with("l2.vector/no-quantiser-order", compile)
        .expect("reference mutant scope");
    let selected = compile();
    let target = ktpu_l2::target::Target::of(&card);
    let price = |module| ktpu_l2::cost::cycles(module, "readback", &target).expect("price");
    assert!(price(&selected) < price(&reference));
    let moves = |module: &ktpu_l1::text::Module| {
        module
            .source
            .lines()
            .filter(|line| line.trim_start().starts_with("vmov ") && line.contains("vl=16"))
            .count()
    };
    assert!(moves(&selected) < moves(&reference));
    let binds = Binds::from([
        ("x".into(), 4096),
        ("out".into(), 16384),
        ("q0".into(), 28672),
        ("status".into(), 36864),
    ]);
    for nan in [false, true] {
        let execute = |module: &ktpu_l1::text::Module| {
            let mut memory = Dram::new(40960);
            for i in 0..4096usize {
                let bits = if nan && i == 13 * 256 + 40 {
                    0x7e00u16
                } else {
                    0x3000 | ((i * 37) as u16 & 0x3ff) | (((i / 7) as u16 & 1) << 15)
                };
                let at = binds["x"] as usize + 2 * i;
                memory.bytes[at..at + 2].copy_from_slice(&bits.to_le_bytes());
            }
            let mut machine = Machine::new(memory, 2, 4, 128, 7);
            let result = run(module, "readback", &binds, &mut machine);
            (machine.mem.bytes, result.map_err(|e| e.to_string()))
        };
        let (want, reference_result) = execute(&reference);
        let (actual, selected_result) = execute(&selected);
        if nan {
            assert!(reference_result
                .expect_err("nonfinite reference")
                .contains("assertion"));
            assert!(selected_result
                .expect_err("nonfinite selected")
                .contains("assertion"));
        } else {
            reference_result.expect("finite reference");
            selected_result.expect("finite selected");
            for (port, bytes) in [("out", 8192), ("q0", 4096)] {
                let at = binds[port] as usize;
                assert_eq!(&actual[at..at + bytes], &want[at..at + bytes], "{port}");
            }
        }
    }
}
