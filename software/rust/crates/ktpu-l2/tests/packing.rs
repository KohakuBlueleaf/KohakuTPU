//! Shared quant-boundary selection, geometry and scheduling mutation witnesses.

use ktpu_l2::{compile_l1, Program, Search};
use ktpu_machine::Machine;

fn source(options: &str) -> String {
    format!("target ktpu.v9\n\nfn q.l2(x: f16[128, 64] @dram, q: mx7a[128, 64] @dram(gm=32,nk=2))\n    quant q <- x {options}\n")
}

fn lower(options: &str) -> Result<String, ktpu_l2::Error> {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let p = Program::parse(&source(options), "<packing>", None)?;
    compile_l1(&p, &m, &[], false, &Search::default()).map(|c| c.l1_text)
}

#[test]
fn finite_vector_stage_uses_raw_ordered_unpacks_and_completion() {
    let text = lower("finite=1").unwrap();
    assert!(text.contains("walk=((32, 2), (128, 4), (512, 16))"));
    assert!(text.contains("vunpk v0 <- l1[120] f16"));
    assert!(text.contains("vpack v0 -> l1[188] mx7a"));
    assert!(text.contains("vsync fe on=drain"));
    assert!(!text.contains("vmov") && !text.contains("quant w("));
}

#[test]
fn unproven_vector_input_is_checked_on_device() {
    let text = lower("").unwrap();
    assert!(text.contains("vcmplt p0"));
    assert!(text.contains("assert_zero r14 code=1"));
    assert!(text.contains("__mx7_status_"));
    assert!(!lower("finite=1").unwrap().contains("assert_zero"));
}

#[test]
fn excluded_override_refuses_and_policy_mutant_is_killed() {
    assert!(lower("backend=mover_quant").is_err());
    #[cfg(feature = "mutants")]
    {
        let text = ktpu_l2::mutant::scoped(
            ktpu_l2::mutant::State {
                active: Some("l2.packing/ignore-policy"),
                ..Default::default()
            },
            || lower("backend=mover_quant").unwrap(),
        );
        assert!(text.contains("quant w("));
    }
}

#[test]
fn finite_batch_defaults_and_admission() {
    assert_eq!(lower("").unwrap(), lower("finite_batch=1").unwrap());
    for n in [2, 4] {
        let text = lower(&format!("finite_batch={n}")).unwrap();
        assert!(text.contains(&format!("vcmplt p{}", n - 1)));
        assert!(text.contains("vmax v31, v31, s14"));
    }
    for n in [0, 3, 5, -1] {
        assert!(lower(&format!("finite_batch={n}")).is_err());
    }
}

#[test]
fn half_column_padding_has_word_reads_and_no_dram_scratch() {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let src = "target ktpu.v9\nfn tail.l2(x: f16[224,48] @dram, q: mx7b[224,32] @dram(gn=56,nk=1))\n    quant q <- x[0 : +224, 16 : +16] pad_cols=16 finite_batch=4 overlap=1\n";
    let p = Program::parse(src, "<tail>", None).unwrap();
    let text = compile_l1(&p, &m, &[], false, &Search::default())
        .unwrap()
        .l1_text;
    assert!(text.contains("walk=((32, 1), (96, 4)"));
    assert!(text.contains("vunpk v0[6:]"));
    assert!(text.contains("vsync pack on=drain slack=1"));
    assert!(!text.contains("fill w(") && !text.contains("copy w("));
    let bad = src.replace("pad_cols=16", "pad_cols=8");
    let p = Program::parse(&bad, "<tail>", None).unwrap();
    assert!(compile_l1(&p, &m, &[], false, &Search::default()).is_err());
}

#[test]
fn output_order_is_deterministic_across_compiles() {
    assert_eq!(lower("finite=1").unwrap(), lower("finite=1").unwrap());
}

#[test]
fn expanded_walk_keeps_absolute_offsets_and_small_groups_in_one_image() {
    let m = Machine::named("vlt_v9t6s_1n")
        .unwrap()
        .with_fact(
            "conversion.fp16_mx7.preferred_backend",
            serde_json::json!("vector_mx7"),
        )
        .unwrap()
        .with_fact(
            "conversion.fp16_mx7.vector_looped",
            serde_json::json!(false),
        )
        .unwrap();
    let src = "target ktpu.v9\nfn q.l2(x: f16[32,416] @dram, q: mx7b[32,416] @dram(gn=8,nk=1))\n    quant q <- x\n";
    let p = Program::parse(src, "<absolute>", None).unwrap();
    let build = || {
        compile_l1(&p, &m, &[], false, &Search::default())
            .unwrap()
            .l1_text
    };
    let text = build();
    let body = text.split("fn q.l1").nth(1).unwrap();
    assert!(!body.contains("r10") && !body.contains("r11") && !body.contains("goto"));
    assert!(body.contains("x + 0 + 64"), "{body}");
    assert!(body.contains("q + 0 + 1024"));
    assert_eq!(body.matches("wait vc[0]").count(), 14);
    assert!(text.contains("mx7_0_8_1"));
    assert!(!text.contains("mx7_0_7_1") && !text.contains("mx7_0_1_1"));
    #[cfg(feature = "mutants")]
    {
        let broken = ktpu_l2::mutant::scoped(
            ktpu_l2::mutant::State {
                active: Some("l2.packing/absolute-zero"),
                ..Default::default()
            },
            build,
        );
        assert!(!broken.contains("x + 0 + 64"));
        let no_wait = ktpu_l2::mutant::scoped(
            ktpu_l2::mutant::State {
                active: Some("l2.packing/no-tile-wait"),
                ..Default::default()
            },
            build,
        );
        assert_eq!(no_wait.matches("wait vc[0]").count(), 1);
    }
}
