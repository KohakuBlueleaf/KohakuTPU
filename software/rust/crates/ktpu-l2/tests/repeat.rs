//! A repeated par's advances reach L1 keyed by descriptor (L1.VC.010).

use ktpu_l2::{compile_with, Program, Search, Target, PASSES};
use ktpu_machine::Machine;

/// The L1 text of a repeated copy par, or the refusal's clause.
fn repeated(advances: &str) -> Result<String, String> {
    let text = format!(
        "target ktpu.v9

fn rep.l2(x: f16[64, 128] @dram, out: f16[64, 128] @dram)
    par c in 0..1 on vc[c] classed=1 repeats=3 advances={advances}
        for t in 0..2
            xl = load x[t*4 : +4, 0 : +128] : f16[4, 128] @l1(gt4)
            v = unpack xl : f32[4, 128] @reg(gt4)
            e = add v, 1.0 : f32[4, 128] @reg(gt4)
            p = pack e : f16[4, 128] @l1(gt4)
            store out[t*4 : +4, 0 : +128] <- p
"
    );
    let p = Program::parse(&text, "rep", None).unwrap();
    let m = Machine::named("multimesh_v9t6").unwrap();
    compile_with(&p, &Target::of(&m), &PASSES, false, &Search::default())
        .map(|c| c.l1_text)
        .map_err(|e| e.clause.to_string())
}

#[test]
fn the_ith_advance_keys_descriptor_i() {
    let l1 = repeated("(1024, 2048)").unwrap();
    assert!(
        l1.contains("send vc[0] repeats=3 advances=((0, 1024), (1, 2048))"),
        "{l1}"
    );
}

#[test]
fn advances_are_a_tuple() {
    assert_eq!(repeated("5").unwrap_err(), "L2.VC.018");
}
