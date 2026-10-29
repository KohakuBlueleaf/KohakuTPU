//! Shared quant-boundary selection, geometry and scheduling mutation witnesses.

use ktpu_l2::{compile_l1, Program, Search};
use ktpu_machine::Machine;

fn source(options: &str, nk: i64) -> String {
    format!("target ktpu.v9\n\nfn q.l2(x: f16[128, 64] @dram, q: mx7a[128, 64] @dram(gm=32,nk={nk}))\n    quant q <- x {options}\n")
}

fn lower_nk(options: &str, nk: i64) -> Result<String, ktpu_l2::Error> {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let p = Program::parse(&source(options, nk), "<packing>", None)?;
    compile_l1(&p, &m, &[], false, &Search::default()).map(|c| c.l1_text)
}

fn lower(options: &str) -> Result<String, ktpu_l2::Error> {
    lower_nk(options, 2)
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
    assert!(lower_nk("backend=mover_quant", 1).is_err());
    #[cfg(feature = "mutants")]
    {
        let text = ktpu_l2::mutant::scoped(
            ktpu_l2::mutant::State {
                active: Some("l2.packing/ignore-policy"),
                ..Default::default()
            },
            || lower_nk("backend=mover_quant", 1).unwrap(),
        );
        assert!(text.contains("quant w("));
    }
}

fn fused(nk: i64) -> String {
    format!(
        "target ktpu.v9\n\nfn zq.l2(x: f16[8, 64] @dram, q: mx7a[8, 64] @dram(gm=2, nk={nk}))\n    par c in 0..1 on vc[c]\n        for t in 0..2\n            xl = load x[t*4 : +4, 0 : +64] : f16[4, 64] @l1(gt4)\n            v = unpack xl : f32[4, 64] @reg(gt4)\n            p = pack v : mx7a[4, 64] @l1\n            store q[t*4 : +4, 0 : +64] <- p\n"
    )
}

fn lower_on(text: &str, mutant: Option<&'static str>) -> String {
    let m = Machine::named("multimesh_v9t6").unwrap();
    let p = Program::parse(text, "<packing>", None).unwrap();
    let state = ktpu_l2::mutant::State {
        active: mutant,
        ..Default::default()
    };
    ktpu_l2::mutant::scoped(state, || {
        compile_l1(&p, &m, &[], false, &Search::default())
            .unwrap()
            .l1_text
    })
}

/// NUM.MX7.008: a vector quant or pack into K pairs selects zeros to
/// 2^-24 (`seti s4`, `vsel`) before VPACK; into nk = 1 it does not.
#[test]
fn packs_into_pairs_lift_their_zeros() {
    let quant = |nk, mutant| lower_on(&source("finite=1", nk), mutant);
    let paired = quant(2, None);
    assert!(paired.contains("seti s4 = ") && paired.contains("vsel v0, v0, s4, v0"));
    assert!(!quant(1, None).contains("vsel"));
    let pack = |nk, mutant| lower_on(&fused(nk), mutant);
    assert!(pack(2, None).contains("vsel"));
    assert!(!pack(1, None).contains("vsel"));
    #[cfg(feature = "mutants")]
    {
        assert!(!quant(2, Some("l2.packing/unit-scale-zero")).contains("vsel"));
        assert!(!pack(2, Some("l2.vector/unit-scale-zero")).contains("vsel"));
    }
}

/// RUL.099: mover QUANT into an nk >= 2 destination is refused even where
/// the board's policy is ignored; `l2.packing/mover-paired` lifts it.
#[test]
fn mover_quant_into_pairs_refuses() {
    #[cfg(feature = "mutants")]
    {
        let on = |name: &'static str| ktpu_l2::mutant::State {
            active: Some(name),
            ..Default::default()
        };
        let refused = ktpu_l2::mutant::scoped(on("l2.packing/ignore-policy"), || {
            lower("backend=mover_quant").unwrap_err()
        });
        assert!(refused.message.contains("RUL.099"), "{}", refused.message);
        let m = Machine::named("vlt_v9t6s_1n").unwrap();
        let p = Program::parse(&source("backend=mover_quant", 2), "<packing>", None).unwrap();
        let build = || compile_l1(&p, &m, &[], false, &Search::default());
        assert!(build().is_err());
        let text = ktpu_l2::mutant::scoped(on("l2.packing/mover-paired"), build)
            .unwrap()
            .l1_text;
        assert!(text.contains("quant w("));
    }
}

/// L2.MOV.018: on a mover-preferring board a quant into nk = 2 lowers on the
/// vector cores and one into nk = 1 through mover QUANT.
#[test]
fn paired_destination_lowers_on_the_vector_cores() {
    let m = Machine::named("vlt_v9t6s_1n").unwrap();
    let text = |nk| {
        let p = Program::parse(&source("finite=1", nk), "<packing>", None).unwrap();
        compile_l1(&p, &m, &[], false, &Search::default())
            .unwrap()
            .l1_text
    };
    let (paired, single) = (text(2), text(1));
    assert!(paired.contains("vpack") && !paired.contains("quant w("));
    assert!(single.contains("quant w(") && !single.contains("vpack"));
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
